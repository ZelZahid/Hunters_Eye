"""Tests for the next_game building blocks: name increment, name validation, OCR field reading.

WHY THIS EXISTS: every failure here is silent. A wrong increment still produces a perfectly valid
game name, so nothing errors - the sequence just drifts off the naming scheme and you notice much
later. A misread name is also a valid name, and once it is typed in and created it becomes the
name that gets incremented from then on, so one bad OCR read poisons every game after it.

Frames come from tests/fixtures/, which is committed - NOT from assets/zelScreenshots/, which is
scratch space the owner clears and which makes a test skip rather than fail when it empties.
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

import main
from core import text_detection as td
from core import user_config

failures = 0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


print("1. The number on the end goes up by one")
for before, after in [("z25pin35", "z25pin36"), ("run1", "run2"), ("a0", "a1")]:
    check(f"{before!r} -> {after!r}", main.next_game_name(before) == after)

print("\n2. A name with no number gets a '0'")
#Stated by the project owner as the rule, and it has to be a rule rather than an error: the
#sequence must be able to start from whatever name is already in the box.
for before, after in [("mygame", "mygame0"), ("z25pin", "z25pin0")]:
    check(f"{before!r} -> {after!r}", main.next_game_name(before) == after)

print("\n3. Only a number at the very END counts")
#'z25pin' must not become 'z26pin'. The digits in the middle are part of the name the user chose.
check("'z25pin' -> 'z25pin0', not 'z26pin'", main.next_game_name("z25pin") == "z25pin0")
check("'a1b' -> 'a1b0'", main.next_game_name("a1b") == "a1b0")

print("\n4. Zero padding is kept at its width, and grows when it has to")
check("'z25pin09' -> 'z25pin10' (width kept)", main.next_game_name("z25pin09") == "z25pin10")
check("'z25pin9'  -> 'z25pin10' (no padding invented)", main.next_game_name("z25pin9") == "z25pin10")
check("'z25pin99' -> 'z25pin100' (width grows)", main.next_game_name("z25pin99") == "z25pin100")
check("'run007'   -> 'run008'", main.next_game_name("run007") == "run008")
check("'run099'   -> 'run100'", main.next_game_name("run099") == "run100")

print("\n5. Junk in, nothing out - it never invents a name")
check("empty -> empty", main.next_game_name("") == "")
check("None -> empty", main.next_game_name(None) == "")
check("surrounding spaces are trimmed", main.next_game_name("  z25pin35  ") == "z25pin36")

print("\n6. A name that does not look like a name is REJECTED, not cleaned up")
#The important half. A misread that gets 'fixed' into something plausible is typed in, created,
#and then incremented from there forever - so anything doubtful is refused instead.
check("a good name passes", main.clean_game_name("z25pin35") == "z25pin35")
check("case is preserved", main.clean_game_name("Z25pin35") == "Z25pin35")
check("a trailing cursor artifact is trimmed", main.clean_game_name("z25pin35|") == "z25pin35")
check("OCR fragments are rejected", main.clean_game_name("SAME: 2 | | l") is None)
check("punctuation is rejected", main.clean_game_name("bad!chars") is None)
check("empty is rejected", main.clean_game_name("") is None)
check("None is rejected", main.clean_game_name(None) is None)
check(f"longer than Diablo II allows ({main.MAX_GAME_NAME_LENGTH}) is rejected",
      main.clean_game_name("x" * (main.MAX_GAME_NAME_LENGTH + 1)) is None)
check("exactly at the limit is accepted",
      main.clean_game_name("x" * main.MAX_GAME_NAME_LENGTH) is not None)

print("\n7. Increment and validation compose the way the sequence uses them")
read = "z25pin35|"          # what OCR actually returned, cursor artifact and all
name = main.clean_game_name(read)
check(f"{read!r} -> clean {name!r} -> next {main.next_game_name(name)!r}",
      main.next_game_name(name) == "z25pin36")

print("\n8. The password / game-name settings load from user_config.txt")
cfg = user_config.load(os.path.join(REPO_ROOT, "user_config.txt"),
                       known_meters={"health", "mana"})
check("the file loads", cfg.loaded)
check("use_password is a bool", isinstance(cfg.use_password, bool))
check("password is a string", isinstance(cfg.password, str))
check("game_name is a string (blank = read it off the lobby)", isinstance(cfg.game_name, str))

written = user_config.load.__module__  # keep flake quiet; the real check is below
import tempfile, json
path = os.path.join(tempfile.mkdtemp(), "u.txt")
with open(path, "w", encoding="utf-8") as f:
    f.write("[settings]\nuse_password = yes\npassword =  s3cret \ngame_name = myrun07\n")
c2 = user_config.load(path, default_rules=cfg.rules)
check("use_password = yes is read", c2.use_password is True)
check("the password is taken verbatim (trimmed of surrounding space)", c2.password == "s3cret")
check("an override game name is read", c2.game_name == "myrun07")
with open(path, "w", encoding="utf-8") as f:
    f.write("[settings]\nuse_password = maybe\n")
c3 = user_config.load(path, default_rules=cfg.rules)
check("a nonsense use_password falls back to the default, not a crash",
      c3.use_password == user_config.DEFAULTS["use_password"])

shots = glob.glob(os.path.join(FIXTURES, "lobby_form_crop.png"))
if not shots:
    print("\n9. Reading the name off a real lobby - FIXTURES MISSING, this should not happen")
else:
    print("\n9. Reading the name off a real lobby field")
    #The measurement this whole approach rests on. The same string in Diablo II's map overlay
    #could not be read exactly by ANY combination of threshold, scale and segmentation mode
    #tried - the lobby's own text box can, which is why the name is read there.
    img = cv.imread(shots[0])
    raw = td.read_line(img[140:172, 130:500])
    #CHARACTERS are the contract here; CASE is not, and asserting it was this test being wrong
    #rather than the code. 'z' and 'Z' are the same glyph at different sizes and a text box gives
    #no x-height to judge against - measured, no threshold and no crop geometry produces the
    #lowercase reliably. Section 13 covers where the case actually comes from.
    check(f"OCR reads the characters exactly: {raw!r}", raw.lower() == "z25pin35")
    name = main.clean_game_name(raw)
    check("it survives validation", name is not None and name.lower() == "z25pin35")
    check("and increments correctly", main.next_game_name(name).lower() == "z25pin36")

    print("\n10. read_line does not disturb the shared OCR engine")
    #It switches the shared API's segmentation mode for the call. If it failed to put it back,
    #every item-label scan afterwards would silently get worse.
    before = len(td.read_lines(img))
    td.read_line(img[140:172, 130:500])
    after = len(td.read_lines(img))
    check(f"item-label scanning still works afterwards ({before} lines, then {after})",
          after > 0 and abs(after - before) <= 1)

print("\n11. Locating the Create Game form on a real lobby")
lobbies = sorted(glob.glob(os.path.join(FIXTURES, "lobby.png")))
dialogs = sorted(glob.glob(os.path.join(FIXTURES, "lobby_name_clash.png")))
if not lobbies:
    print("  FIXTURES MISSING - this should not happen")
else:
    lobby = cv.cvtColor(cv.imread(lobbies[0]), cv.COLOR_BGR2BGRA)
    H, W = lobby.shape[:2]
    form = main._lobby_form(lobby)
    check("the form is found", form is not None)
    if form:
        #Row spacing is MEASURED from two of the form's own labels rather than stored, which is
        #what lets this work at any resolution. On this frame the labels sit 69px apart.
        check(f"row spacing measured as {form['row_spacing']:.0f}px", 60 < form["row_spacing"] < 80)
        nx, ny = form["name_field"]
        px, py = form["password_field"]
        #The measured boxes on this screenshot: name y 153..180, password y 222..250, both
        #spanning x 1275..1650. A click has to land inside, not merely near.
        check(f"the name click ({nx}, {ny}) is inside the Game Name box",
              1275 < nx < 1650 and 153 < ny < 180)
        check(f"the password click ({px}, {py}) is inside the Password box",
              1275 < px < 1650 and 222 < py < 250)
        check("the password row is exactly one row below the name row",
              abs((py - ny) - form["row_spacing"]) < 2)

    print("\n12. Reading the name Diablo II pre-filled, and incrementing it")
    read = main._read_lobby_game_name(lobby, form)
    check(f"a name is read: {read!r}", read is not None)
    check("it is the right name, ignoring case", (read or "").lower() == "z25pin38")
    check("it survives validation", main.clean_game_name(read) is not None)
    check(f"and increments to {main.next_game_name(read)!r}",
          main.next_game_name(read).lower() == "z25pin39")

    print("\n13. Case comes from memory, because OCR cannot supply it")
    #'z' and 'Z' are the same glyph at different sizes and a text box gives no x-height to judge
    #against; every threshold from 120 to 150 read this field as 'Z25pin38'. So the reading picks
    #WHICH name, and the remembered spelling supplies the case.
    saved = main._last_created_name
    try:
        main._last_created_name = None
        check("with no memory, OCR's case is used as-is", main._preferred_case("Z25pin38") == "Z25pin38")
        main._last_created_name = "z25pin38"
        check("with a matching memory, the typed case wins",
              main._preferred_case("Z25pin38") == "z25pin38")
        #Self-correcting: renaming the game by hand must take effect at once, not be overridden.
        check("a different name ignores the memory", main._preferred_case("other9") == "other9")
        check("nothing read stays nothing", main._preferred_case(None) is None)
    finally:
        main._last_created_name = saved

print("\n14. The name-clash dialog is detected, and only when it is there")
if not lobbies or not dialogs:
    print("  SKIPPED (needs the screenshots)")
else:
    with_dialog = cv.cvtColor(cv.imread(dialogs[0]), cv.COLOR_BGR2BGRA)
    without = cv.cvtColor(cv.imread(lobbies[0]), cv.COLOR_BGR2BGRA)
    check("detected when 'A Game Already Exists With That Name' is up",
          main._name_clash_showing(with_dialog) is True)
    #A false positive here would make the sequence bump the name and retry forever.
    check("not detected on a normal lobby", main._name_clash_showing(without) is False)
    import numpy as np
    check("not detected on a blank frame",
          main._name_clash_showing(np.zeros((1080, 1920, 4), np.uint8)) is False)

print("\n15. next_game refuses to start if it cannot leave the current game")
#The failure that compounds: a next_game that believes it quit types a game name into a live game.
saved_quit = main.quit_game
try:
    main.quit_game = lambda: False
    check("returns False when quit_game fails", main.next_game() is False)
finally:
    main.quit_game = saved_quit

print("\n16. A name no single threshold reads correctly is still resolved, per character")
#Regression, 2026-09-03. Live failure: the lobby held 'zze9' and next_game refused to read it.
#The four thresholds gave zzeQ / zze9 / zze9 / zz09 - '9' misreads as 'Q' at one end of the
#range and 'e' as '0' at the other - so no WHOLE string ever reached the 3-of-4 bar, even though
#every character was decidable (z 4/4, z 4/4, e 3/4, 9 3/4). Whole-string voting discards where
#the readings disagree, which is exactly where the evidence lives.
zze9 = os.path.join(FIXTURES, "lobby_zze9.png")
if not os.path.exists(zze9):
    print("  FIXTURE MISSING - this should not happen")
    failures += 1
else:
    import cv2 as _cv
    _frame = _cv.cvtColor(_cv.imread(zze9), _cv.COLOR_BGR2BGRA)
    _form = main._lobby_form(_frame)
    check("form located on the zze9 lobby", _form is not None)
    if _form is not None:
        check("'zze9' read despite no single threshold winning outright",
              main._read_lobby_game_name(_frame, _form) == "zze9")

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
