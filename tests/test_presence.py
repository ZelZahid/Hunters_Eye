"""Tests for presence.py and the in-play gate, using the real screenshots when they are present.

WHY THIS EXISTS: this check is the only thing standing between a lobby screen and a potion
sequence being typed into a text field, and it is the ONLY guard whose evidence is independent of
colour. The colour-based guards cannot help here by construction - other players' names render
red exactly where the health orb belongs, and red text and red liquid are the same colour - so if
this check silently stops discriminating, nothing else catches it.

Frames come from tests/fixtures/, which is COMMITTED. They deliberately do not come from
assets/zelScreenshots/ - that folder is scratch space the owner clears, and a test reading from it
SKIPS rather than fails when the file goes, which looks exactly like passing. It happened here:
this file's whole real-screenshot section silently stopped running for a while.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glob
import os
import sys

import cv2 as cv
import numpy as np

from core import presence

failures = 0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


print("1. The shipped in-play config loads")
p = presence.load(os.path.join(ASSETS, "in_play.json"))
check("assets/in_play.json loads", p is not None)
if p is None:
    print("\nCannot continue without the config.")
    sys.exit(1)
check("every template is single channel (matched on one channel, like the needle)",
      all(r.template.ndim == 2 for r in p.references))
check("every reference has a threshold", all(0.0 < r.threshold < 1.0 for r in p.references))
check("every reference has a search region", all(len(r.search_region) == 4 for r in p.references))
#TWO references on OPPOSITE corners. One was fragile to anything covering it: an item tooltip
#over the right-hand orb dropped the score to 0.48 and the pipeline decided it was not in a game.
check(f"there is more than one reference ({[r.name for r in p.references]})",
      len(p.references) >= 2)
lefts = [r.search_region[0] for r in p.references]
check(f"they look in different places (x fractions {lefts})", max(lefts) - min(lefts) > 0.3)

print("\n2. A missing or broken config is 'no check available', never a crash")
check("missing file -> None", presence.load(os.path.join(ASSETS, "nope-does-not-exist.json")) is None)
import json, tempfile
bad = os.path.join(tempfile.mkdtemp(), "bad.json")
with open(bad, "w", encoding="utf-8") as f:
    f.write("{ not json at all")
check("malformed JSON -> None, no exception", presence.load(bad) is None)
missing_tpl = os.path.join(tempfile.mkdtemp(), "m.json")
with open(missing_tpl, "w", encoding="utf-8") as f:
    json.dump({"template": "nothing.png", "source_size": [1920, 1080],
               "search_region": [0, 0, 1, 1]}, f)
check("template file missing -> None, no exception", presence.load(missing_tpl) is None)
#Written INTO the assets dir so the template resolves and the missing "source_size" is what
#actually fails - otherwise this passes for the wrong reason (template-not-found).
no_fields = os.path.join(ASSETS, "_test_no_fields.json")
try:
    with open(no_fields, "w", encoding="utf-8") as f:
        json.dump({"template": "in_play.png"}, f)
    check("required field missing -> None, no exception", presence.load(no_fields) is None)
finally:
    os.remove(no_fields)

print("\n3. An unanswerable question returns None, which is NOT False")
#None must never be collapsed into False by a caller: on None the pipeline has to behave exactly
#as it did before this check existed, or a setup without the config would be permanently disabled.
found, score = p.check(None, 500)
check("a None frame -> (None, None)", found is None and score is None)
found, score = p.check(np.zeros((0, 0), np.uint8), 500)
check("an empty frame -> (None, None)", found is None and score is None)
tiny = np.zeros((8, 8), np.uint8)
found, score = p.check(tiny, 1920)
check("a frame smaller than the template -> None, not False", found is None)
found, score = p.check(np.zeros((300, 300), np.uint8), 0)
check("a zero HUD width -> None, not a divide-by-zero", found is None)

print("\n4. A blank frame is a confident NO, not an error")
found, score = p.check(np.zeros((400, 700), np.uint8), 700, region=(0.0, 0.0, 1.0, 1.0))
check(f"flat black -> found={found} (score {score if score is None else round(score, 3)})",
      found is False)

print("\n5. The template is rescaled to the HUD's size, not assumed")
#Without this the check would silently start failing the moment the game ran at a resolution
#other than the one the reference was cut from.
first = p.references[0]
big = first.template_at(1.0)
small = first.template_at(0.5)
check("half scale gives a smaller template", small is not None and small.shape[0] < big.shape[0])
check("the resize is cached, not redone every frame", first.template_at(0.5) is small)
check("an absurd scale is refused rather than producing a 1px template",
      first.template_at(0.0001) is None)

shots = sorted(glob.glob(os.path.join(FIXTURES, "*.png")))
if not shots:
    print("\n6. Real-screenshot separation - FIXTURES MISSING, this should not happen")
else:
    print("\n6. Real screenshots: in-play and lobby separate by a wide margin")
    #The numbers that justify the threshold. If a future change narrows this gap, the guard is
    #being eroded and this is where it shows up.
    #Keyed by filename, so a fixture added here without an entry above is simply not scored
    #rather than being guessed at.
    EXPECT = {"in_game_tooltip": ("in game, tooltip over an orb", True),
              #A DIM area. The reference art used to include the game world showing above the
              #orb, so the score tracked how well-lit the room was: this frame scored 0.56 and
              #the pipeline announced NOT IN GAME while the HUD sat there in plain view.
              "in_game_dim": ("in game, an unlit catacomb", True),
              "lobby.png": ("lobby", False),
              "lobby_name_clash": ("lobby, name-clash dialog", False)}
    scores = {}
    for path in shots:
        key = next((k for k in EXPECT if k in path), None)
        if key is None:
            continue
        label, want = EXPECT[key]
        img = cv.imread(path)
        small = cv.resize(img, (0, 0), fx=0.3, fy=0.3)
        gray = cv.cvtColor(small, cv.COLOR_BGR2GRAY)
        found, score = p.check(gray, gray.shape[1])
        scores[want] = min(scores.get(True, 1.0), score) if want else max(scores.get(False, 0.0), score)
        check(f"{label:26} -> in_play={found} (score {score:.3f})", found is want)
    if True in scores and False in scores:
        margin = scores[True] - scores[False]
        check(f"worst in-play {scores[True]:.3f} is clear of best lobby {scores[False]:.3f} "
              f"(margin {margin:.3f})", margin > 0.2)
        #Every reference shares a threshold here; assert against all of them rather than
        #reaching for a single number that would quietly mean "the first one".
        check(f"every threshold sits between them "
              f"({sorted({r.threshold for r in p.references})})",
              all(scores[False] < r.threshold < scores[True] for r in p.references))

print("\n7. The in-play gate actually stops the potion layer in a lobby")
import main
if not shots:
    print("  SKIPPED (needs the screenshots)")
else:
    from core import game_state as gs
    profiles = gs.load_profiles(os.path.join(ASSETS, "meters.json"))
    for path in shots:
        if "lobby" not in os.path.basename(path):
            continue
        img = cv.imread(path)
        small = cv.resize(img, (0, 0), fx=0.3, fy=0.3)
        prof = gs.select_profile(profiles, (img.shape[1], img.shape[0]))
        if prof is None:
            continue
        readings = gs.read_all(small, prof.meters)
        gated = {k: None for k in readings}      # what main.py publishes when in_play is False
        rule = main._potion_due(gated, 10_000.0, {}, 0.0)
        check(f"{os.path.basename(path)[-10:-4]}: gated readings -> no potion", rule is None)

print("\n8. Occlusion: one covered reference must not read as 'not in a game'")
occluded = sorted(glob.glob(os.path.join(FIXTURES, "in_game_tooltip.png")))
if not occluded:
    print("  SKIPPED (needs the screenshots)")
else:
    #The reported bug: hovering an item draws a tooltip over the right-hand orb, the single
    #reference scored 0.48, and everything paused - while the left orb sat in plain view.
    img = cv.imread(occluded[0])
    small = cv.resize(img, (0, 0), fx=0.3, fy=0.3)
    g = cv.cvtColor(small, cv.COLOR_BGR2GRAY)
    found, score = p.check(g, g.shape[1])
    check(f"in play despite the tooltip (best score {score:.3f})", found is True)

    #Each reference on its own, to show WHY it needed a second one rather than a looser threshold.
    for reference in p.references:
        one = presence.Presence([reference])
        _f, s1 = one.check(g, g.shape[1])
        print(f"       {reference.name:18} alone: {s1:.3f}"
              f"{'  <- covered by the tooltip' if s1 < reference.threshold else ''}")
    #This fixture no longer covers either reference: the tooltip sat over the upper band and the
    #blue orb, and both were cut out of the templates when they turned out to be game world and
    #liquid rather than fixed art. That is a better outcome, but it leaves the fixture unable to
    #PROVE the two-reference design still works - "nothing was occluded" and "occlusion is handled"
    #look identical from the result. So occlusion is staged explicitly instead: black out one
    #reference's whole search region and the other must still carry the answer.
    for blinded in p.references:
        staged = g.copy()
        rx, ry, rw, rh = blinded.search_region
        h, w = staged.shape
        staged[int(ry * h):int((ry + rh) * h), int(rx * w):int((rx + rw) * w)] = 0
        blind_found, blind_score = p.check(staged, w)
        check(f"'{blinded.name}' fully blacked out -> still in play "
              f"(score {blind_score:.3f})", blind_found is True)
    check("a lobby still matches NO reference",
          all(presence.Presence([r]).check(
              cv.cvtColor(cv.resize(cv.imread(
                  os.path.join(FIXTURES, "lobby.png")),
                  (0, 0), fx=0.3, fy=0.3), cv.COLOR_BGR2GRAY), g.shape[1])[0] is False
              for r in p.references))

print("\n9. The negative is debounced, the positive is not")
import main as pipeline
import time as _time
#Named fixtures, not a glob for timestamps from a folder that no longer holds them. This section
#quietly skipped for several versions because "004909" and "000149" were scratch filenames that
#went away - a skip reads exactly like a pass, which is the whole reason fixtures are committed.
ingame = [os.path.join(FIXTURES, "in_game_tooltip.png")]
lobby = [os.path.join(FIXTURES, "lobby.png")]
if not all(os.path.exists(f) for f in ingame + lobby):
    print("  FIXTURES MISSING, this should not happen")
    failures += 1
else:
    def frame(path):
        return cv.cvtColor(cv.resize(cv.imread(path), (0, 0), fx=0.3, fy=0.3), cv.COLOR_BGR2GRAY)
    good, bad = frame(ingame[0]), frame(lobby[0])
    pipeline._in_play_missing_since = None
    pipeline._update_in_play(good)
    check("a good frame reads in play", pipeline.in_play() is True)
    pipeline._update_in_play(bad)
    #Seeing the HUD is proof; not seeing it might only mean something is covering it. So a miss
    #has to persist before it counts, while a hit counts at once.
    check("a brief miss does NOT flip it", pipeline.in_play() is True)
    _time.sleep(pipeline.IN_PLAY_MISS_GRACE_SECONDS + 0.05)
    pipeline._update_in_play(bad)
    check("a miss that persists past the grace DOES flip it", pipeline.in_play() is False)
    pipeline._update_in_play(good)
    check("and it comes back immediately, with no grace period", pipeline.in_play() is True)
    pipeline._in_play_missing_since = None

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
