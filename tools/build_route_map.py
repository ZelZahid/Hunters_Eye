"""Stitch screenshots taken while walking a route into a map, and record the route on it.

    python tools/build_route_map.py harrogath_to_nihlathak \\
        assets/zelScreenshots/Screenshot011.png ... assets/zelScreenshots/Screenshot018.png \\
        --point portal=Screenshot017.png:642,292 \\
        --keep-out waypoint=Screenshot014.png:665,320,955,455

Writes assets/routes/<name>.png (the map, greyscale, at --scale) and assets/routes/<name>.json (the
path, named points, keep-out zones, and what the runtime needs to locate a frame on the map). This
is a SETUP tool, run by hand once per route - the same "distinctly-named new file" case as
calibrate_meters.py, not a second pipeline.

HOW TO RECORD A ROUTE: walk it by hand, taking a screenshot every second or so. List them in the
order you walked. Consecutive shots must OVERLAP - roughly half a screen in common - because
overlap is the only thing that says where one screenshot sits relative to the next. The path the
runtime follows is the path you walked (where your character stood in each shot), which is what
makes it passable: nothing here plans a route, it replays one that is already known to work.

A named point or keep-out zone is given in the coordinates of ONE screenshot ("the portal is at
642,292 in Screenshot017"), and converted onto the map from there.

WHAT IT CHECKS RATHER THAN ASSUMES:
  - that every pair of overlapping screenshots differs by a pure TRANSLATION (no zoom, no rotation).
    The whole idea of a map rests on this - see core/localize.py - and a game whose camera tilts or
    zooms as it moves would produce a map that is quietly wrong everywhere.
  - that every screenshot is connected to the first through overlaps. A gap means two sets of
    screenshots with no known relation, and guessing it would put half the route in the wrong place.
  - that each screenshot, located back on the finished map, lands where the stitching put it.
"""
import argparse
import itertools
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2 as cv
import numpy as np

from core.localize import MapLocator

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "assets" / "routes"

#Defaults are Diablo II: Resurrected at 16:9. All are fractions of the frame, so they hold at any
#16:9 resolution; another game or aspect ratio passes its own.
#  viewport       - the part of the frame that shows the WORLD: below the top-left portrait and the
#                   clock, above the life/mana text and the orbs.
#  character_box  - the player's own character. It moves WITH the camera, so it is the one thing
#                   in every screenshot that is not part of the map; left in, it would pull every
#                   registration towards "nothing moved".
#  anchor         - where the character stands on screen. The camera is centred on the character,
#                   so this is also where a screenshot says "I was standing here".
#  query_view     - the crop of a LIVE frame that gets located on the map. Centred on the character
#                   rather than the whole viewport, and that was measured: a full-width query failed
#                   outright on a screenshot taken near the map's edge (it could not fit inside the
#                   map), while this crop located every one of 15 screenshots within 3.2px.
DEFAULT_VIEWPORT = (0.0, 0.11, 1.0, 0.69)
DEFAULT_CHARACTER_BOX = (0.4375, 0.30, 0.125, 0.22)
#  overlays       - things drawn at a fixed place on the SCREEN inside the viewport, which must be
#                   left out for the same reason as the character, only worse: they are pixel-for-
#                   pixel identical in every screenshot, so they match perfectly at ZERO shift.
#                   Diablo II's chat and system messages ("[Game] Zelgy says: ...") sit bottom-left.
#                   Measured: one chat line produced 474 agreeing feature matches between two
#                   screenshots whose cameras were really 438px apart, and the solve put them at the
#                   same place. Pass more with --ignore (the F5 debug panel, if it was up).
DEFAULT_OVERLAYS = ((0.0, 0.62, 0.30, 0.18),)
DEFAULT_ANCHOR = (0.5, 0.485)
DEFAULT_QUERY_VIEW = (0.2, 0.12, 0.6, 0.64)
#0.25 is the measured sweet spot: 0.2 / 0.25 / 0.33 all located every held-out screenshot within
#4px, at 8 / 13 / 29 ms per locate. Below 0.25 the error starts to grow; above, the cost doubles.
DEFAULT_SCALE = 0.25
#Local contrast normalisation - see core/localize.py for the false match it exists to stop. 6 map
#pixels was the measured best of 3 / 6 / 10.
DEFAULT_NORMALIZE_SIGMA = 6
#Normalised scores sit on a different scale from plain ones. Measured: true matches 0.67-0.88 with
#margins 0.59-0.82; the other area's frames at most 0.10. So 0.4 / 0.25 sit well clear of both.
DEFAULT_THRESHOLD = 0.4
DEFAULT_MIN_MARGIN = 0.25

#A pair needs this many agreeing feature matches before its offset is believed. Repeating
#textures (stone walls, cobbles) produce 25-45 "matches" between screenshots that do not overlap at
#all; genuinely overlapping pairs measured 170-2400. Letting the weak ones in put errors of over a
#thousand pixels into the solve.
MIN_INLIERS = 150
#Once a pair's shift is estimated, the overlapping PIXELS must actually agree - not just the feature
#matches. Every match can come from one thing that is not the scene (the chat line above), and
#RANSAC will happily call that a consensus. A real overlap makes the whole picture line up.
#Measured on 38 pairs: the chat-line pair correlated 0.142; every genuine pair 0.539-0.939, the
#lowest being mid-fight screenshots full of spell flashes.
EDGE_MIN_CORRELATION = 0.4
MAX_SCALE_ERROR = 0.01   #measured 0.0015 worst on real pairs
MAX_ROTATION_DEG = 0.5   #measured 0.07 worst
EDGE_RESIDUAL_LIMIT = 15.0  #px; measured 0.3 worst once the weak pairs are out


def fractions(text, count):
    values = tuple(float(v) for v in text.split(","))
    if len(values) != count:
        raise argparse.ArgumentTypeError(f"expected {count} comma-separated numbers, got {text!r}")
    return values


def world_mask(shape, viewport, character_box, overlays=DEFAULT_OVERLAYS):
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    vx, vy, vw, vh = viewport
    mask[int(vy * h):int((vy + vh) * h), int(vx * w):int((vx + vw) * w)] = 255
    for bx, by, bw, bh in (character_box, *overlays):
        mask[int(by * h):int((by + bh) * h), int(bx * w):int((bx + bw) * w)] = 0
    return mask


def overlap_correlation(gray_a, gray_b, mask_a, mask_b, offset, scale=0.25):
    """How well two screenshots' overlapping pixels agree once b is shifted by `offset` onto a
    (a point at p in a is at p + offset in b). Pearson correlation of the masked overlap at
    `scale`, or None when the overlap is too small or too flat to say."""
    small = [cv.resize(g, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA).astype(np.float32)
             for g in (gray_a, gray_b)]
    masks = [cv.resize(m, (small[0].shape[1], small[0].shape[0]),
                       interpolation=cv.INTER_NEAREST) > 0 for m in (mask_a, mask_b)]
    dx, dy = (int(round(v * scale)) for v in offset)
    h, w = small[0].shape
    x0, x1 = max(0, -dx), min(w, w - dx)
    y0, y1 = max(0, -dy), min(h, h - dy)
    if x1 - x0 < 20 or y1 - y0 < 20:
        return None
    keep = masks[0][y0:y1, x0:x1] & masks[1][y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    va = small[0][y0:y1, x0:x1][keep]
    vb = small[1][y0 + dy:y1 + dy, x0 + dx:x1 + dx][keep]
    if va.size < 400 or va.std() < 1 or vb.std() < 1:
        return None
    return float(np.corrcoef(va, vb)[0, 1])


def register(grays, masks, names):
    """Camera offset of every screenshot relative to the first: world = screen + offset."""
    orb = cv.ORB_create(6000, fastThreshold=10)
    features = [orb.detectAndCompute(g, m) for g, m in zip(grays, masks)]
    matcher = cv.BFMatcher(cv.NORM_HAMMING)

    edges = []
    for a, b in itertools.combinations(range(len(grays)), 2):
        (ka, da), (kb, db) = features[a], features[b]
        if da is None or db is None:
            continue
        pairs = matcher.knnMatch(da, db, k=2)
        good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < MIN_INLIERS:
            continue
        pa = np.float32([ka[m.queryIdx].pt for m in good])
        pb = np.float32([kb[m.trainIdx].pt for m in good])
        M, inliers = cv.estimateAffinePartial2D(pa, pb, method=cv.RANSAC, ransacReprojThreshold=3.0,
                                                maxIters=5000, confidence=0.999)
        if M is None or int(inliers.sum()) < MIN_INLIERS:
            continue
        count = int(inliers.sum())
        scale = float(np.hypot(M[0, 0], M[1, 0]))
        rotation = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        if abs(scale - 1) > MAX_SCALE_ERROR or abs(rotation) > MAX_ROTATION_DEG:
            print(f"  WARNING: {names[a]} -> {names[b]} is not a pure translation "
                  f"(scale {scale:.4f}, rotation {rotation:+.2f} deg) - pair ignored. If this is "
                  f"common, the camera zooms or tilts and a flat map will not work.")
            continue
        keep = inliers.ravel().astype(bool)
        offset = np.median(pb[keep] - pa[keep], axis=0)
        agreement = overlap_correlation(grays[a], grays[b], masks[a], masks[b], offset)
        if agreement is None or agreement < EDGE_MIN_CORRELATION:
            shown = "too little overlap to check" if agreement is None else f"{agreement:.2f}"
            print(f"  WARNING: {names[a]} -> {names[b]}: {count} features agree on a shift of "
                  f"({offset[0]:+.0f}, {offset[1]:+.0f}) but the pictures do not ({shown}) - "
                  f"pair ignored. Usually something drawn on the SCREEN rather than in the world "
                  f"(chat, a panel) - see --ignore.")
            continue
        edges.append((a, b, offset, count))

    def solve(edges):
        rows, rhs, weights = [], [], []
        for a, b, offset, count in edges:
            row = np.zeros(len(grays))
            row[b], row[a] = 1, -1
            rows.append(row)
            rhs.append(-offset)
            weights.append(np.sqrt(count))
        row = np.zeros(len(grays))
        row[0] = 1
        rows.append(row)
        rhs.append(np.zeros(2))
        weights.append(1000.0)
        weights = np.array(weights)[:, None]
        return np.linalg.lstsq(np.array(rows) * weights, np.array(rhs) * weights, rcond=None)[0]

    #Solve, then throw out the single worst-fitting pair and solve again, until every pair agrees.
    #One false pair (repeating texture that slipped past MIN_INLIERS) would otherwise drag every
    #screenshot it touches.
    while True:
        cams = solve(edges)
        errors = [np.linalg.norm(-(cams[b] - cams[a]) - offset) for a, b, offset, _ in edges]
        worst = int(np.argmax(errors)) if errors else None
        if worst is None or errors[worst] <= EDGE_RESIDUAL_LIMIT:
            break
        a, b = edges[worst][:2]
        print(f"  dropping {names[a]} -> {names[b]}: disagrees with the rest by {errors[worst]:.0f}px")
        edges.pop(worst)

    #Every screenshot has to be connected to the first through overlapping pairs.
    reached, frontier = {0}, [0]
    while frontier:
        node = frontier.pop()
        for a, b, _, _ in edges:
            for x, y in ((a, b), (b, a)):
                if x == node and y not in reached:
                    reached.add(y)
                    frontier.append(y)
    missing = [names[i] for i in range(len(grays)) if i not in reached]
    if missing:
        sys.exit(f"ERROR: these screenshots do not overlap enough with the rest to be placed: "
                 f"{missing}. Take screenshots closer together along the route.")

    residual = max(errors) if errors else 0.0
    return cams, len(edges), residual


def stitch(frames, masks, cams, scale):
    """The map: the per-pixel MEDIAN of every screenshot covering that spot.

    Median rather than "last one wins" because the things that are NOT the map - an NPC walking
    past, a torch mid-flicker - are in a different place in each screenshot, and a median drops
    them as outliers the same way game_state's Smoother drops a one-frame glitch.
    """
    origin = cams.min(axis=0)
    h, w = frames[0].shape[:2]
    size = np.ceil((cams.max(axis=0) - origin + [w, h]) * scale).astype(int) + 1
    stack = np.full((len(frames), size[1], size[0]), np.nan, np.float32)
    for k, (frame, mask, cam) in enumerate(zip(frames, masks, cams)):
        gray = cv.cvtColor(cv.resize(frame, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA),
                           cv.COLOR_BGR2GRAY).astype(np.float32)
        small_mask = cv.resize(mask, (gray.shape[1], gray.shape[0]),
                               interpolation=cv.INTER_NEAREST) > 0
        gray[~small_mask] = np.nan
        x, y = np.round((cam - origin) * scale).astype(int)
        stack[k, y:y + gray.shape[0], x:x + gray.shape[1]] = gray
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   #"All-NaN slice": spots nothing covers
        merged = np.nanmedian(stack, axis=0)
    covered = ~np.isnan(merged)
    #0 MEANS "NEVER SEEN", so nothing that was seen may be 0. core/localize.py tells the mapped
    #area apart by exactly that, and a genuinely black pixel of a dark scene - most of Nihlathak's
    #Temple's arrival point, for instance - would otherwise be thrown away as unmapped.
    merged = np.where(covered, np.clip(np.round(merged), 1, 255), 0)
    return merged.astype(np.uint8), origin, float(covered.mean())


def parse_named(spec, count):
    """'portal=Screenshot017.png:642,292' -> ('portal', 'Screenshot017.png', (642, 292))."""
    try:
        name, rest = spec.split("=", 1)
        shot, coords = rest.rsplit(":", 1)
        values = tuple(float(v) for v in coords.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected name=SCREENSHOT:x,y..., got {spec!r}")
    if len(values) != count:
        raise argparse.ArgumentTypeError(f"{spec!r}: expected {count} numbers")
    return name, shot, values


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("name", help="route name - becomes assets/routes/<name>.png/.json")
    parser.add_argument("screenshots", nargs="+", help="screenshots, in the order walked")
    parser.add_argument("--point", action="append", default=[],
                        type=lambda s: parse_named(s, 2), help="name=SCREENSHOT:x,y")
    parser.add_argument("--keep-out", action="append", default=[],
                        type=lambda s: parse_named(s, 4), help="name=SCREENSHOT:x0,y0,x1,y1")
    parser.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    parser.add_argument("--viewport", type=lambda s: fractions(s, 4), default=DEFAULT_VIEWPORT)
    parser.add_argument("--character-box", type=lambda s: fractions(s, 4),
                        default=DEFAULT_CHARACTER_BOX)
    parser.add_argument("--anchor", type=lambda s: fractions(s, 2), default=DEFAULT_ANCHOR)
    parser.add_argument("--query-view", type=lambda s: fractions(s, 4), default=DEFAULT_QUERY_VIEW)
    parser.add_argument("--ignore", action="append", type=lambda s: fractions(s, 4),
                        help="x,y,w,h fractions of a screen-fixed overlay to leave out; replaces "
                             "the default (the chat area) - repeat for several")
    args = parser.parse_args()
    overlays = tuple(args.ignore) if args.ignore else DEFAULT_OVERLAYS

    paths = [Path(p) for p in args.screenshots]
    names = [p.name for p in paths]
    frames = [cv.imread(str(p)) for p in paths]
    for path, frame in zip(paths, frames):
        if frame is None:
            sys.exit(f"ERROR: could not read {path}")
    if len({f.shape for f in frames}) != 1:
        sys.exit("ERROR: the screenshots are not all the same size - take them at one resolution.")
    h, w = frames[0].shape[:2]

    print(f"Registering {len(frames)} screenshots ({w}x{h})...")
    masks = [world_mask(f.shape, args.viewport, args.character_box, overlays) for f in frames]
    grays = [cv.cvtColor(f, cv.COLOR_BGR2GRAY) for f in frames]
    cams, n_pairs, residual = register(grays, masks, names)
    print(f"  {n_pairs} overlapping pairs, all agreeing to within {residual:.1f}px")

    print(f"Stitching at scale {args.scale}...")
    map_gray, origin, coverage = stitch(frames, masks, cams, args.scale)
    cams_on_map = cams - origin
    print(f"  map {map_gray.shape[1]}x{map_gray.shape[0]}, {coverage:.0%} of it covered")

    #Each screenshot, located back on the finished map. In-sample, so this is a sanity check of
    #the stitching, not evidence the map generalises - tests/test_route.py does that with
    #screenshots the map was NOT built from.
    print("Locating each screenshot back on the map:")
    locator = MapLocator(map_gray, DEFAULT_THRESHOLD, DEFAULT_MIN_MARGIN, DEFAULT_NORMALIZE_SIGMA)
    vx, vy, vw, vh = args.query_view
    for name, frame, cam in zip(names, frames, cams_on_map):
        small = cv.cvtColor(cv.resize(frame, None, fx=args.scale, fy=args.scale,
                                      interpolation=cv.INTER_AREA), cv.COLOR_BGR2GRAY)
        sh, sw = small.shape
        x0, y0 = int(vx * sw), int(vy * sh)
        fix = locator.locate(small, (x0, y0, int(vw * sw), int(vh * sh)))
        if fix.found:
            error = np.linalg.norm(np.array([(fix.x - x0), (fix.y - y0)]) / args.scale - cam)
            print(f"  {name}: score {fix.score:.3f}, margin {fix.margin:.3f}, off by {error:.1f}px")
        else:
            print(f"  {name}: NOT located (score {fix.score}, margin {fix.margin}) - check this one")

    anchor = np.array(args.anchor) * [w, h]
    index = {n: i for i, n in enumerate(names)}

    def on_map(shot, x, y):
        if shot not in index:
            sys.exit(f"ERROR: {shot!r} is not one of the screenshots given")
        return (cams_on_map[index[shot]] + [x, y]).round(1).tolist()

    points = {name: on_map(shot, *xy) for name, shot, xy in args.point}
    keep_out = {}
    for name, shot, (x0, y0, x1, y1) in args.keep_out:
        keep_out[name] = on_map(shot, x0, y0) + on_map(shot, x1, y1)

    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    map_file = ROUTES_DIR / f"{args.name}.png"
    cv.imwrite(str(map_file), map_gray)
    meta = {
        "_comment": [
            "Generated by tools/build_route_map.py - re-run it rather than editing positions by hand.",
            "All positions are in SOURCE pixels (the screenshots' own resolution) measured from the",
            "map's top-left; the map image itself is stored at map_scale. 'path' is where the",
            "character stood in each screenshot, in the order walked. 'frames' is where each",
            "screenshot's top-left sits on the map, kept for checking and tests.",
            "Built with: " + " ".join(Path(a).name if a.endswith(".png") else a
                                      for a in sys.argv[1:]),
        ],
        "map": map_file.name,
        "map_scale": args.scale,
        "source_size": [w, h],
        "query_view": list(args.query_view),
        "character_anchor": list(args.anchor),
        "normalize_sigma": DEFAULT_NORMALIZE_SIGMA,
        "threshold": DEFAULT_THRESHOLD,
        "min_margin": DEFAULT_MIN_MARGIN,
        "path": [(cam + anchor).round(1).tolist() for cam in cams_on_map],
        "points": points,
        "keep_out": keep_out,
        "frames": {n: cam.round(1).tolist() for n, cam in zip(names, cams_on_map)},
    }
    with open(ROUTES_DIR / f"{args.name}.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
    print(f"Wrote {map_file.relative_to(REPO_ROOT)} and {args.name}.json")


if __name__ == "__main__":
    main()
