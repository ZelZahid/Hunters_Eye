"""Tests for counting catches: core/text_feed.py (which chat lines are new), wow.py's ChatReader (which
of those are loot, and what they say), routes/fish_tally.py (names, prices, the tally) and the
auto-pause timer in routes/fishing.py.

The chat frames are real: the owner's own screenshot of his chat box (2026-11-25), and crops of his
fourth fishing recording (2026-11-24 00-24-51) at 1920x1080, each exactly the box wow.py captures
by default - including the moment two loot lines arrive. Nothing here needs the game; the cards
that read text need Tesseract, like V-407.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import json
import tempfile
import time

import cv2 as cv
import numpy as np

import wow
from core import text_detection, text_feed
from routes import fish_tally, fishing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "wow"

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def load(name):
    path = FIXTURES / name
    image = cv.imread(str(path))
    if image is None:                    #a committed fixture that is missing FAILS, never skips
        print(f"FAIL missing committed fixture {path}")
        sys.exit(1)
    return image


def ocr_or_skip(card):
    """True when Tesseract is here. Without it these cards skip - except under CI, where a skip is
    a failure (docs/test_cards.md, "Skips are failures under CI")."""
    if text_detection.ocr_available():
        return True
    if os.environ.get("CI"):
        check(f"{card}: Tesseract must be installed on CI", False)
    else:
        print("  SKIPPED - no Tesseract on this machine")
    return False


SCREENSHOT = load("chat_zel_screenshot.png")      #436x147, 7 whole lines and a cut-off one on top
LOOT_FRAMES = [load(f"chat_loot_{t}s.png") for t in ("28_25", "28_5", "28_75")]   #406x178, 0.25 s apart
CRAB = load("chat_crab_behind.png")               #a monster walking behind the (empty) chat box
CONFIG = wow.load_chat_config()
PITCH = 18
SNAPPER_LINE = "You receive loot: [Raw Longjaw Mud Snapper]"


def lines_of(frame):
    return text_feed.split_lines(frame, PITCH)


print("\nV-416  Chat lines are cut exactly between rows, whatever is behind the box, and the pitch is measured")
check(f"the screenshot's line pitch is measured as 18 px (got {text_feed.estimate_pitch(SCREENSHOT)})",
      text_feed.estimate_pitch(SCREENSHOT) == 18)
check(f"...and the recording's, over grass and fog, too (got {text_feed.estimate_pitch(LOOT_FRAMES[0])})",
      text_feed.estimate_pitch(LOOT_FRAMES[0]) == 18)
tops = [line.top for line in lines_of(SCREENSHOT)]
check(f"7 whole lines, the cut-off one at the top left out (tops {tops})",
      tops == [13, 31, 49, 67, 85, 103, 121])
check("a box with nothing in it has no lines", lines_of(np.zeros((178, 406, 3), np.uint8)) == [])
check("a box shorter than one line has no lines, and no crash", lines_of(SCREENSHOT[:10]) == [])


print("\nV-417  A line matches itself in another frame - even fading, or over moving grass - and never a different line")
lines = lines_of(SCREENSHOT)
snapper_a, snapper_b = lines[2], lines[6]         #"You receive loot: [Raw Longjaw Mud Snapper]" twice
check("the two Snapper loot lines are the same line", text_feed.same_line(snapper_a, snapper_b))
different = [(i, j) for i in range(7) for j in range(7)
             if i != j and {i, j} != {2, 6} and text_feed.same_line(lines[i], lines[j])]
check(f"no two different lines match, not even two loot lines for different fish ({different})",
      not different)
faded = lines_of((SCREENSHOT.astype(np.float32) * 0.6).astype(np.uint8))
check("a line at 60% brightness (fading out) still matches itself",
      len(faded) == 7 and text_feed.same_line(lines[1], faded[1]))
first, later = lines_of(LOOT_FRAMES[0]), lines_of(LOOT_FRAMES[2])
check("the recording's old lines, 0.5 s apart over moving grass, match where they moved to",
      all(text_feed.same_line(a, b) for a, b in zip(first[2:], later[:len(first) - 2])))


print("\nV-418  New lines are found as they arrive on a real recording; an unchanged box finds none; a crab is 'cannot tell'")
feed = text_feed.TextFeed(PITCH)
check("the first look is 'cannot tell' (nothing to compare with yet)", feed.feed(LOOT_FRAMES[0]) is None)
check("the same picture again: nothing new", feed.feed(LOOT_FRAMES[0]) == [])
arrived = [feed.feed(frame) for frame in LOOT_FRAMES[1:]]
check(f"one new line in each of the next two frames ({[len(a) if a is not None else None for a in arrived]})",
      [len(a) if a is not None else None for a in arrived] == [1, 1])
check("not ambiguous: the box has other lines in it", not feed.ambiguous)
check("a monster walking behind the box is 'cannot tell' - never a box full of new lines",
      feed.feed(CRAB) is None)
check("a frame that is not there is 'cannot tell'", feed.feed(None) is None)


def box(rows, height=7):
    """A synthetic chat box from the screenshot's real line pictures, newest at the bottom."""
    pictures = [lines[i].image for i in rows[-height:]]
    return np.ascontiguousarray(np.vstack([SCREENSHOT[:13]] + pictures))


print("\nV-419  A box of nothing but the same line: no line is invented, but it says it cannot be sure")
feed = text_feed.TextFeed(PITCH)
feed.feed(box([0, 1, 3, 4, 5, 2, 2]))
new = feed.feed(box([0, 1, 3, 4, 5, 2, 2, 2]))
check("a repeat that scrolls something else off the top is seen (one new line)",
      new is not None and len(new) == 1 and not feed.ambiguous)
feed = text_feed.TextFeed(PITCH)
feed.feed(box([2] * 7))
new = feed.feed(box([2] * 8))
check("seven Snappers then eight: no new line can be seen...", new == [])
check("...and it says so: ambiguous", feed.ambiguous)
feed = text_feed.TextFeed(PITCH)
feed.feed(box([0, 1, 3, 4, 5, 6, 2], height=7)[:13 + 18 * 3])     #only three lines showing
new = feed.feed(box([0, 1, 3, 4, 5, 6, 2], height=7)[:13 + 18 * 4])
check("a box that is not full yet grows downward: the extra line is new",
      new is not None and len(new) == 1)
feed = text_feed.TextFeed(PITCH)
feed.feed(box([3, 4, 2]))
emptied = feed.feed(np.ascontiguousarray(np.vstack([SCREENSHOT[:13]] + [np.zeros_like(lines[0].image)] * 3)))
back = feed.feed(box([3, 4, 2]))
check("hidden and shown again (all lines vanish at once, then return): nothing counted twice",
      emptied == [] and back == [])


print("\nV-319  Loot lines are read off real chat: the screenshot's three, and the recording's two as they arrive")
if ocr_or_skip("V-319"):
    reader = wow.ChatReader(CONFIG, PITCH)
    loot = [reader.read(line.image)[0] for line in lines if reader.is_loot(line.image)]
    check(f"the screenshot: Catfish, Snapper, Snapper ({loot})",
          [entry[:2] for entry in loot] == [("Raw Bristle Whisker Catfish", 1),
                                           ("Raw Longjaw Mud Snapper", 1), ("Raw Longjaw Mud Snapper", 1)])
    check("only green loot lines are read: the NPC and skill lines are not", len(loot) == 3)
    reader = wow.ChatReader(CONFIG, PITCH)
    events = [reader.look(frame, when) for when, frame in zip((0.0, 0.25, 0.5), LOOT_FRAMES)]
    check(f"the recording: nothing on the first look, then Gaffer Jack, then the Albacore ({events})",
          [[e[0] for e in batch] for batch in events] == [[], ["Gaffer Jack"], ["Raw Rainbow Fin Albacore"]])
    check("...each counted once: looking again finds nothing", reader.look(LOOT_FRAMES[2], 0.75) == [])


print("\nV-420  Chat lines 16 px apart (a smaller font) are measured and read at 16, so every catch is counted")
#The owner's chat on 2026-11-25, 16:04: three Snappers, each after a skill-up line, lines 16 px
#apart. wow.py read it at the 18 px chat.json says, the box stopped lining up with itself every
#few seconds, and two of the three catches were never counted.
PITCH16 = load("chat_pitch16_loot.png")
check(f"the pitch is measured as 16 (got {text_feed.estimate_pitch(PITCH16)})",
      text_feed.estimate_pitch(PITCH16) == 16)
check("...and as 18 on the screenshot and recording read at 18 before - nothing changes there",
      text_feed.estimate_pitch(SCREENSHOT) == 18 and text_feed.estimate_pitch(LOOT_FRAMES[2]) == 18)


def earlier(frame, lines_back, pitch=16):
    """The box as it was `lines_back` lines ago: everything that many lines lower, the empty water
    from its own top filling the space above (WoW's chat grows at the bottom and scrolls up)."""
    if lines_back == 0:
        return frame.copy()
    water = np.vstack([frame[:pitch]] * lines_back)
    return np.ascontiguousarray(np.vstack([water, frame[:frame.shape[0] - lines_back * pitch]]))


#Lines 0-7: skill 55, LOOT, online, skill 56, LOOT, skill 57, LOOT, online. Each step is one arrival.
ARRIVALS = [6, 6, 6, 5, 5, 3, 3, 1, 1, 0, 0]
notes = []
reader = wow.ChatReader(CONFIG, 18, ocr=lambda image, threshold: SNAPPER_LINE, log=notes.append)
found = [len(reader.look(earlier(PITCH16, n), 0.25 * i)) for i, n in enumerate(ARRIVALS)]
check(f"started at chat.json's 18, it switches to 16 within the first two looks ({notes[:1]})",
      reader.pitch == 16 and any("16 px apart" in note for note in notes))
check(f"...then the two Snappers that arrive are each counted once, when they arrive ({found})",
      found == [0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0])
reader = wow.ChatReader(CONFIG, 16, ocr=lambda image, threshold: SNAPPER_LINE, log=notes.append)
found = [len(reader.look(earlier(PITCH16, n), 0.25 * i)) for i, n in enumerate([8, 8] + ARRIVALS)]
check(f"from an empty box: all three, one per arrival ({found})", sum(found) == 3
      and found == [0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0])
reader = wow.ChatReader(CONFIG, 16, ocr=lambda image, threshold: SNAPPER_LINE, log=notes.append)
[reader.look(PITCH16, t) for t in (0.0, 0.25, 0.5)]
check("a box at the right pitch is never switched away from it", reader.pitch == 16)


print("\nV-421  Guild chat is green too, but not the loot green - it is never read as a loot line")
GUILD = load("chat_guild_pitch16.png")
reader = wow.ChatReader(CONFIG, 16)
guild_lines = text_feed.split_lines(GUILD, 16)
check(f"9 lines, 6 of them green guild chat: none is taken for loot "
      f"({[reader.is_loot(line.image) for line in guild_lines]})",
      len(guild_lines) == 9 and not any(reader.is_loot(line.image) for line in guild_lines))
check("...while every loot line still is, on the owner's chat and on the recording",
      [reader.is_loot(l.image) for l in text_feed.split_lines(PITCH16, 16)].count(True) == 3
      and wow.ChatReader(CONFIG, 18).is_loot(lines_of(LOOT_FRAMES[2])[-1].image))
check("a green line that does not read as loot is not reported as an unread loot line",
      not wow.looks_like_loot("[Guild] [Belle Rogl: long weekend to level to 60 an tm")
      and wow.looks_like_loot("You recieve lool: [Raw Longjaw") and wow.looks_like_loot("Vou rcceive"))


print("\nV-422  A line fading out, or a streak behind the empty top of the box, does not lose the box")
#A fading line stays bright enough to be a line but too faint to match itself for a moment
#(measured on the recording over grass at 35-45% brightness); before, that made the whole box
#"cannot tell", and a loot line arriving in that moment was never counted.
rows = [0, 1, 3, 4, 5, 6, 2]
before = box(rows)
after = box(rows + [2])
top = 13                                     #the screenshot's first whole line starts here
junk = after.copy()
junk[top:top + PITCH] = (junk[top:top + PITCH].astype(np.float32) * 0.6).astype(np.uint8)
junk[top:top + PITCH, :, :] = np.roll(junk[top:top + PITCH], 120, axis=1)   #no longer matches itself
feed = text_feed.TextFeed(PITCH)
feed.feed(before)
new = feed.feed(junk)
check(f"the top line no longer matching while a Snapper arrives: the Snapper is still found "
      f"({None if new is None else len(new)} new)", new is not None and len(new) == 1)
emptied = before.copy()
emptied[top:top + PITCH] = SCREENSHOT[:1].mean(axis=0, keepdims=True).astype(np.uint8)  #slot left empty
streak = emptied.copy()
for x in range(30, 400, 9):                  #a dashed glint behind the empty slot
    cv.line(streak, (x, top + 8), (x + 5, top + 8), (255, 255, 255), 2)
feed = text_feed.TextFeed(PITCH)
feed.feed(emptied)
check("a streak appearing in the empty top slot is set aside: nothing new, not 'cannot tell'",
      feed.feed(streak) == [])
feed = text_feed.TextFeed(PITCH)
feed.feed(before)
check("but a line that was seen before is never set aside as junk: a box whose top line was seen elsewhere stays 'cannot tell'",
      feed.feed(box([4, 1, 3, 4, 5, 6, 2])) is None)
feed = text_feed.TextFeed(PITCH)
feed.feed(box(rows))
check("the same box again, nothing faded: nothing new (the junk step changes no plain answer)",
      feed.feed(box(rows)) == [] and not feed.ambiguous)


print("\nL-712  Loot lines are parsed from what OCR really returns - misread brackets, stacks, junk after")
cases = {
    "You receive loot: [Raw Bristle Whisker Catfish] ia": ("Raw Bristle Whisker Catfish", 1, True),
    "You receive loot: (Gaffer Jack]": ("Gaffer Jack", 1, True),
    "You receive loot: [Raw Rainbow Fin Albacore]. ,..": ("Raw Rainbow Fin Albacore", 1, True),
    "You receive loot: [Raw Brilliant Smallfish]x2": ("Raw Brilliant Smallfish", 2, True),
    "You receive loot: [Raw Longjaw Mud Snapp": ("Raw Longjaw Mud Snapp", 1, False),
    "Your skill in Fishing has increased to 113.": None,
    "Adam says: Shhh! You're scaring the fish away.": None,
    "": None,
}
for text, expected in cases.items():
    got = fish_tally.parse_loot_line(text)
    check(f"{text!r} -> {got}", got == expected)


print("\nL-713  A name snaps to the fish it means; numbers must agree; an unknown fish is new; junk is refused")
catalog = fish_tally.Catalog(wow.PRICES)
check("a misread letter snaps to the real fish",
      catalog.resolve("Raw Longiaw Mud Snapper") == "Raw Longjaw Mud Snapper")
check("case does not matter", catalog.resolve("raw brilliant smallfish") == "Raw Brilliant Smallfish")
check("17 Pound Catfish is NOT filed as the known 19 Pound Catfish (0.94 alike, different fish)",
      catalog.resolve("17 Pound Catfish") == "17 Pound Catfish")
check("a fish never seen is a new name, as read", catalog.resolve("Gaffer Jack") == "Gaffer Jack")
check("an unclosed reading is only taken when it snaps to a known fish",
      catalog.resolve("Raw Longjaw Mud Snapp", closed=False) == "Raw Longjaw Mud Snapper"
      and catalog.resolve("Gaffer Ja", closed=False) is None)
check("symbols are not a fish", catalog.resolve("~=| ;") is None and catalog.resolve("") is None)
check("there is no built-in list of fish: the seed file is just data",
      not hasattr(fish_tally, "FISH") and len(catalog.names()) >= 8)


print("\nL-714  Prices: copper, silver and gold; the user's own file wins; new fish, typed prices and merges are kept")
check("money is shown the way the game shows it",
      [fish_tally.format_money(c) for c in (7, 1900, 12345, None)] == ["7c", "19s 0c", "1g 23s 45c", "?"])
check("prices can be written 40, '2s 40c', '1g', '19 s'",
      [fish_tally.parse_money(v) for v in (40, "2s 40c", "1g", "19 s")] == [40, 240, 10000, 1900])
for bad in ("lots", "-5", "2x"):
    try:
        fish_tally.parse_money(bad)
        check(f"{bad!r} is refused", False)
    except ValueError:
        check(f"{bad!r} is refused", True)
check("the seed prices are the tooltips' Sell Price (Snapper 20c, 19 Pound Catfish 19s)",
      catalog.price("Raw Longjaw Mud Snapper") == 20 and catalog.price("19 Pound Catfish") == 1900)
with tempfile.TemporaryDirectory() as folder:
    user = Path(folder) / "fish_prices.json"
    mine = fish_tally.Catalog(wow.PRICES, user)
    check("a new fish is added with no price, and written to the user's file",
          mine.add("Gaffer Jack") and mine.price("Gaffer Jack") is None
          and "Gaffer Jack" in json.loads(user.read_text())["items"])
    mine.set_price("Gaffer Jack", "1s 5c")
    mine.set_price("Raw Longjaw Mud Snapper", 25)
    again = fish_tally.Catalog(wow.PRICES, user)
    check("typed prices survive a restart, and the user's price beats the repo's",
          again.price("Gaffer Jack") == 105 and again.price("Raw Longjaw Mud Snapper") == 25)
    again.add("Raw Longjaw Mud Snapperr")
    again.merge("Raw Longjaw Mud Snapperr", "Raw Longjaw Mud Snapper")
    third = fish_tally.Catalog(wow.PRICES, user)
    check("a merged misreading is gone from the list, and counts as the right fish from then on",
          "Raw Longjaw Mud Snapperr" not in third.names()
          and third.resolve("Raw Longjaw Mud Snapperr") == "Raw Longjaw Mud Snapper")
    user.write_text("{ not json")
    broken = fish_tally.Catalog(wow.PRICES, user)
    check("a damaged user file is reported, and the repo's prices still work",
          broken.problems and broken.price("Oily Blackmouth") == 24)


print("\nL-715  The tally: counts, what they are worth (unpriced fish counted apart), and per hour")
tally = fish_tally.Tally()
tally.add("Raw Longjaw Mud Snapper", 1, 0.0)
tally.add("Raw Longjaw Mud Snapper", 2, 10.0)
tally.add("Gaffer Jack", 1, 20.0)
tally.add("Oily Blackmouth", 1, 30.0, how="inferred")
copper, unpriced = tally.value(catalog)
check(f"5 caught, worth 3 x 20c + 24c = 84c, 1 with no price ({copper}c, {unpriced})",
      tally.total == 5 and copper == 84 and unpriced == 1)
check("an inferred catch is counted, and counted as inferred", tally.inferred == 1)
check("per hour needs at least a minute of fishing", fish_tally.per_hour(5, 30) is None)
check("5 in 10 minutes is 30 an hour", fish_tally.per_hour(5, 600) == 30.0)
tally.merge("Gaffer Jack", "Oily Blackmouth")
check("merging moves the count", tally.counts["Oily Blackmouth"] == 2 and "Gaffer Jack" not in tally.counts)
check("durations read like a clock",
      [fish_tally.format_duration(s) for s in (12, 249, 3720)] == ["12s", "4m 09s", "1h 02m"])
with tempfile.TemporaryDirectory() as folder:
    history = Path(folder) / "catches.csv"
    fish_tally.append_history(history, "2026-11-25 01:00:00", "s1", "Gaffer Jack", 1, "read")
    fish_tally.append_history(history, "2026-11-25 01:01:00", "s2", "Gaffer Jack", 2, "read")
    with open(history, "a", encoding="utf-8") as handle:
        handle.write("garbage,row\n")
    counts, sessions = fish_tally.read_history(history)
    check("the history file adds up across sessions, and a damaged row is skipped",
          counts == {"Gaffer Jack": 3} and sessions == 2)


print("\nL-716  Auto-pause: after N minutes with no F5, never while paused, and every F5 starts it over")
timer = fishing.AutoPause(30, now=0.0)
check("not due before 30 minutes", not timer.due(1799.0))
check("due at 30 minutes", timer.due(1800.0))
check("never 'due' while already paused", not timer.due(5000.0, paused=True))
timer.restart(1000.0)                       #F5 pressed at 1000 s
check("an F5 press starts the 30 minutes over", not timer.due(2700.0) and timer.due(2800.0))
check("the time left counts down, and shows the full time while paused",
      timer.left(1900.0) == 900.0 and timer.left(1900.0, paused=True) == 1800.0)
timer.set_minutes(10, 3000.0)
check("changing the minutes counts from the change", timer.left(3000.0) == 600.0)
try:
    fishing.AutoPause(0, 0.0)
    check("0 minutes is refused (that would be no safety at all)", False)
except ValueError:
    check("0 minutes is refused (that would be no safety at all)", True)
check(f"wow.py's default is 30 minutes, and the GUI's choices include it ({wow.AUTO_PAUSE_CHOICES})",
      wow.AUTO_PAUSE_MINUTES == 30 and 30 in wow.AUTO_PAUSE_CHOICES)


print("\nL-717  The one case pixels cannot settle: a reeled-in bite and a box of repeats counts one more; a fish that got away does not")
SNAPPER = "You receive loot: [Raw Longjaw Mud Snapper]"
fake_ocr = lambda image, threshold: SNAPPER        #the box below holds only Snapper lines
quiet = lambda message: None
full = box([2] * 7)
reader = wow.ChatReader(CONFIG, PITCH, ocr=fake_ocr, log=quiet)
reader.look(full, 0.0)
reader.look(full, 1.0)
check("no reel-in, a box of repeats: nothing counted", reader.look(full, 10.0) == [])
reader.expect(20.0)
early = reader.look(full, 21.0)
late = reader.look(full, 25.5)
check(f"reeled in at 20 s, no new line by 25 s, box all repeats: one Snapper, inferred ({late})",
      early == [] and [(e[0], e[3]) for e in late] == [("Raw Longjaw Mud Snapper", "inferred")])
check("...and only once", reader.look(full, 30.0) == [])
reader.expect(40.0)
reader.cancel()                              #"Your fish got away!" appeared
check("the fish got away: nothing inferred", reader.look(full, 46.0) == [])
mixed = box([0, 1, 3, 4, 5, 6, 2])
reader = wow.ChatReader(CONFIG, PITCH, ocr=fake_ocr, log=quiet)
reader.look(mixed, 0.0)
reader.expect(1.0)
check("a box with other lines in it is never ambiguous, so nothing is inferred from it",
      reader.look(mixed, 2.0) == [] and reader.look(mixed, 7.0) == [])


#The owner's four chat screenshots of 2026-11-25 (16 px lines, a smaller chat font), each with the
#chat frame's scroll arrows and the portrait's level badge down the right edge and two glowing
#buttons below the last line. wow.py counted 9 of 25+ catches that session.
ICONS = load("chat_icons_pitch16.png")
SHOTS = [ICONS, load("chat_guild_smallfish_pitch16.png"), load("chat_catfish_smallfish_pitch16.png"),
         load("chat_mana_potion_pitch16.png")]
SNAPPER_NAME, SMALLFISH, CATFISH = ("Raw Longjaw Mud Snapper", "Raw Brilliant Smallfish",
                                    "Raw Bristle Whisker Catfish")
catalog = fish_tally.Catalog(wow.PRICES)          #the repo's price list only


print("\nV-423  Icons and scroll arrows beside the chat are not text: each line is only as wide as its words")
icon_lines = text_feed.split_lines(ICONS, 16)
check(f"11 lines of text; the glowing buttons under the last line are not a line "
      f"(tops {[line.top for line in icon_lines]})", len(icon_lines) == 11 and icon_lines[-1].top < 170)
loot_lines = [line for line in icon_lines if wow.ChatReader(CONFIG, 16).is_loot(line.text)]
check(f"the five loot lines stop where their words do, short of the scroll arrows at x 418+ "
      f"({[line.text.shape[1] for line in loot_lines]})",
      len(loot_lines) == 5 and all(line.text.shape[1] < 320 for line in loot_lines))
check("...while the whole row is still there for anyone who wants it",
      all(line.image.shape[1] == ICONS.shape[1] for line in icon_lines))
#A long line ending right at the scroll arrows: in one frame 415 px of words, in the next its ink
#runs into the arrows (443). It is the same line, compared over the width both have.
long_line = max(icon_lines, key=lambda line: line.text.shape[1])
joined = ICONS.copy()
end = long_line.text.shape[1]
cv.rectangle(joined, (end + 6, long_line.top + 7), (ICONS.shape[1] - 1, long_line.top + 8), (255, 255, 255), -1)
moved = text_feed.split_lines(joined, 16)[icon_lines.index(long_line)]
check(f"a line that runs into the box's edge in one frame still matches itself "
      f"({long_line.text.shape[1]} px vs {moved.text.shape[1]} px)",
      moved.at_edge and text_feed.same_line(long_line, moved) and text_feed.same_line(moved, long_line))
check("...and still no two different lines of the screenshot match",
      not [(i, j) for i, a in enumerate(icon_lines) for j, b in enumerate(icon_lines)
           if i < j and text_feed.same_line(a, b) and a.text.shape[1] != b.text.shape[1]])


print("\nV-424  Lines are cut between their descenders and the next line's ascenders, at the same place in every frame")
#The cut used to be "the emptiest row", and on the 16 px font four rows nearly tie (0.7-1.5 px of
#ink): it moved 3 px between frames and sometimes sliced the descenders off ("Lonaiaw Snanperl").
check(f"the 16 px screenshot's Snappers are cut at rows 65, 113, 145, descenders and all "
      f"({[line.top for line in text_feed.split_lines(PITCH16, 16) if wow.ChatReader(CONFIG, 16).is_loot(line.text)]})",
      [line.top for line in text_feed.split_lines(PITCH16, 16)
       if wow.ChatReader(CONFIG, 16).is_loot(line.text)] == [65, 113, 145])
phases = sorted({line.top % 16 for n in range(0, 8) for line in text_feed.split_lines(earlier(PITCH16, n), 16)})
check(f"the same box scrolled up a line at a time is always cut at the same offset ({phases})",
      len(phases) == 1)
check("WoW's default font (18 px) is cut exactly where it was before (rows 13, 31, ...)",
      [line.top for line in lines_of(SCREENSHOT)] == [13, 31, 49, 67, 85, 103, 121])


def chat_session(seed, catches=30, ocr=None, known=()):
    """A fishing session made of the owner's real chat lines: each step, 0-2 other lines then one
    loot line arrive at the bottom of a box with his chat frame's scroll arrows and buttons drawn
    over its right edge, as in his screenshots; the box is looked at three times per step (4 a
    second). Returns (loot line ids that arrived, events reported, pitch at the end, looks lost)."""
    right = 418                                  #the scroll arrows and the level badge start here
    strips = [(line.image[:, :right].copy(), wow.ChatReader(CONFIG, 16).is_loot(line.text))
              for shot in SHOTS for line in text_feed.split_lines(shot, 16)]
    loot = [i for i, (_, is_loot) in enumerate(strips) if is_loot]
    other = [i for i, (_, is_loot) in enumerate(strips) if not is_loot]
    slots = list(range(2, 163, 16))
    background = ICONS[:1, :right].mean(axis=(0, 1)).astype(np.uint8)

    def picture(stream):
        frame = ICONS.copy()                     #its right edge and the buttons stay put
        frame[:178, :right] = background
        shown = stream[-len(slots):]
        for top, i in zip(slots[len(slots) - len(shown):], shown):
            frame[top:top + 16, :right] = strips[i][0]
        return frame

    rng = np.random.default_rng(seed)
    stream = [int(i) for i in rng.choice(other, len(slots))]
    reader = wow.ChatReader(CONFIG, 18, ocr=ocr or (lambda image, threshold: SNAPPER),
                            log=lambda message: None, known=lambda: known)
    arrived, events, lost, when = [], [], 0, 0.0
    for _ in range(3):
        events += reader.look(picture(stream), when)
        when += 0.25
    for _ in range(catches):
        stream += [int(i) for i in rng.choice(other, int(rng.integers(0, 3)))]
        stream.append(int(rng.choice(loot)))
        arrived.append(stream[-1])
        for _ in range(3):
            events += reader.look(picture(stream), when)
            lost += reader.lost
            when += 0.25
    return arrived, events, reader.pitch, lost


print("\nV-425  A fishing session of the owner's real chat lines, scroll arrows and all: every catch counted, once")
#Before this fix the same sessions counted 0-6 catches of 30 - the scroll arrows and buttons rode
#along on every line and the box never lined up with itself. That is the "9 of 25" of 2026-11-25.
for seed in (1, 2, 3):
    arrived, events, pitch, lost = chat_session(seed)
    check(f"session {seed}: {len(events)} of {len(arrived)} catches counted, read at {pitch} px, "
          f"{lost} look(s) lost", len(events) == len(arrived) and pitch == 16)


print("\nV-426  The owner's loot lines are read exactly: enlarged 3x, cut to their words")
if ocr_or_skip("V-426"):
    expected = [CATFISH, SNAPPER_NAME, "Lesser Healing Potion", SNAPPER_NAME, SNAPPER_NAME,
                SMALLFISH, SNAPPER_NAME,
                CATFISH, SMALLFISH, SMALLFISH, SNAPPER_NAME,
                SNAPPER_NAME, SNAPPER_NAME, "Lesser Mana Potion"]
    reader = wow.ChatReader(CONFIG, 16)
    names = []
    for shot in SHOTS:
        for line in text_feed.split_lines(shot, 16):
            if reader.is_loot(line.text):
                parsed, text = reader.read(line.text)
                names.append(catalog.resolve(parsed[0], parsed[2]) if parsed else None)
    check(f"all 14 loot lines of the four screenshots name the right item ({names})", names == expected)
    arrived, events, _, _ = chat_session(4, catches=12, ocr=wow.ocr_line, known=tuple(catalog.names()))
    read = [catalog.resolve(raw, closed) for raw, count, closed, how in events]
    check(f"a session read with real OCR: 12 catches, each with a name the price list knows ({read})",
          len(read) == 12 and all(name in catalog.prices or name.startswith("Lesser") for name in read))


print("\nL-719  The line pitch is never switched to twice or three times the one in use")
#The owner's log: "the lines are 32 px apart, not 16 - reading at 32" and back, three times in a
#minute. At 32 two chat lines are read as one ('Raw Lonoiah WISSHRASEH AY').
real_estimate = text_feed.estimate_pitch
try:
    reader = wow.ChatReader(CONFIG, 16, ocr=lambda image, threshold: SNAPPER, log=lambda m: None)
    reader.look(PITCH16, 0.0)
    for measured in (32, 48):
        text_feed.estimate_pitch = lambda frame, value=measured: value
        for step in range(10):
            reader.look(earlier(PITCH16, step % 3), 0.25 * step)
        check(f"measured {measured} again and again: still read at 16 ({reader.pitch})", reader.pitch == 16)
    text_feed.estimate_pitch = lambda frame: 14
    reader.pitch_confirmed = True
    switched_at = None
    for step in range(10):
        reader.look(earlier(PITCH16, step % 2), 0.25 * step)
        if reader.pitch == 14 and switched_at is None:
            switched_at = step
    check(f"a real change, once the pitch was confirmed, takes {wow.ChatReader.PITCH_VOTES_CONFIRMED} "
          f"measurements in a row, not 2 (switched at look {switched_at})",
          switched_at is not None and switched_at >= wow.ChatReader.PITCH_VOTES_CONFIRMED - 1)
finally:
    text_feed.estimate_pitch = real_estimate
check("16 is measured on a box of the owner's lines that scored 48 best before (lines of very "
      "different lengths)", chat_session(6, catches=1)[2] == 16)


print("\nL-720  A loot line whose name cannot be read is still a catch, never dropped")
garbled = "You receiv: Raw Brigit Svan beatNer>"            #from the owner's log, 2026-11-25 17:03:45
reader = wow.ChatReader(CONFIG, 16, ocr=lambda image, threshold: garbled, log=lambda m: None)
reader.look(earlier(PITCH16, 2), 0.0)
events = reader.look(PITCH16, 0.25)                  #a Snapper line and an "online" line arrive
check(f"a green loot line OCR'd as {garbled!r}: one catch, name unknown ({events})",
      events == [("", 1, False, "read")])
with tempfile.TemporaryDirectory() as folder:
    saved_history = wow.HISTORY
    wow.HISTORY = Path(folder) / "catches.csv"
    try:
        books = wow.Bookkeeper(fish_tally.Catalog(wow.PRICES, Path(folder) / "mine.json"), "test", False)
        books.caught("", 1, False, "read", True)
        books.caught("Raw Lonoiah WISSHRASEH AY", 1, False, "read", True)
        books.caught("Raw Longjaw Mud Snapper", 1, True, "read", True)
        check(f"the Bookkeeper counts all three, two as '{fish_tally.UNREADABLE}' ({dict(books.tally.counts)})",
              books.tally.total == 3 and books.tally.counts[fish_tally.UNREADABLE] == 2)
        check("...and does not add the unreadable name to the price list",
              fish_tally.UNREADABLE not in fish_tally.Catalog(wow.PRICES, Path(folder) / "mine.json").prices)
    finally:
        wow.HISTORY = saved_history


print("\nL-721  A name read wrong at one threshold and right at another is taken from the right one")
answers = {110: "You receive loot: [Raw Lonoiah WISSHRASEH AY]", 140: "You receive loot: [Raw Longjaw Mud Snapper]",
           85: "You receive loot: [Raw Longjaw Mud Snapper]"}
asked = []
reader = wow.ChatReader(CONFIG, 16, ocr=lambda image, threshold: asked.append(threshold) or answers[threshold],
                        known=lambda: catalog.names())
parsed, text = reader.read(PITCH16[64:80])
check(f"the known name, from the second threshold ({parsed}, thresholds tried {asked})",
      parsed == (SNAPPER_NAME, 1, True) and asked == [110, 140])
asked.clear()
answers[110] = "You receive loot: [Raw Longjaw Mud Snapper]"
reader.read(PITCH16[64:80])
check(f"a known name read right the first time costs one OCR call ({asked})", asked == [110])
answers.update({threshold: "You receive loot: [Glowing Starfish]" for threshold in answers})
asked.clear()
parsed, _ = reader.read(PITCH16[64:80])
check(f"a new fish (no threshold reads a known name) is still taken, as read ({parsed})",
      parsed == ("Glowing Starfish", 1, True))


print("\nL-722  The log's times are on a 12-hour clock, like the panel's Latest catches")
noon = time.mktime((2026, 11, 25, 12, 5, 9, 0, 0, -1))
check(f"12:05:09 -> {wow.clock_text(noon)!r}", wow.clock_text(noon) == "12:05:09 PM")
check(f"00:05:09 -> {wow.clock_text(noon - 12 * 3600)!r}", wow.clock_text(noon - 12 * 3600) == "12:05:09 AM")
check(f"16:56:47 -> {wow.clock_text(noon + 4 * 3600 + 51 * 60 + 38)!r}",
      wow.clock_text(noon + 4 * 3600 + 51 * 60 + 38) == "4:56:47 PM")


print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
