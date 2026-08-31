"""Tests for presence.py and the in-play gate, using the real screenshots when they are present.

WHY THIS EXISTS: this check is the only thing standing between a lobby screen and a potion
sequence being typed into a text field, and it is the ONLY guard whose evidence is independent of
colour. The colour-based guards cannot help here by construction - other players' names render
red exactly where the health orb belongs, and red text and red liquid are the same colour - so if
this check silently stops discriminating, nothing else catches it.

The screenshot-backed section is skipped when assets/zelScreenshots/ is absent (it is gitignored),
so this file still runs anywhere. The synthetic sections always run.
"""
import glob
import os
import sys

import cv2 as cv
import numpy as np

import presence

failures = 0
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


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
check("the template is single channel (matched on one channel, like the needle)",
      p.template.ndim == 2)
check("a threshold is set", 0.0 < p.threshold < 1.0)
check("a search region is set", len(p.search_region) == 4)

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
big = p._template_at(1.0)
small = p._template_at(0.5)
check("half scale gives a smaller template", small is not None and small.shape[0] < big.shape[0])
check("the resize is cached, not redone every frame", p._template_at(0.5) is small)
check("an absurd scale is refused rather than producing a 1px template",
      p._template_at(0.0001) is None)

shots = sorted(glob.glob(os.path.join(ASSETS, "zelScreenshots", "*.png")))
if not shots:
    print("\n6. Real-screenshot separation - SKIPPED (assets/zelScreenshots/ is gitignored)")
else:
    print("\n6. Real screenshots: in-play and lobby separate by a wide margin")
    #The numbers that justify the threshold. If a future change narrows this gap, the guard is
    #being eroded and this is where it shows up.
    EXPECT = {"221353": ("actual gameplay", True),
              "210653": ("ESC menu, HUD still drawn", True),
              "210700": ("lobby A", False),
              "222238": ("lobby B", False)}
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
        check(f"the threshold {p.threshold} sits between them",
              scores[False] < p.threshold < scores[True])

print("\n7. The in-play gate actually stops the potion layer in a lobby")
import main
if not shots:
    print("  SKIPPED (needs the screenshots)")
else:
    import game_state as gs
    profiles = gs.load_profiles(os.path.join(ASSETS, "meters.json"))
    for path in shots:
        if "222238" not in path and "210700" not in path:
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

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
