"""
On-screen text detector: OCRs the whole frame in a single Tesseract call and
fuzzy-matches recognized lines against a target item list.

Kept separate from main.py's image-template detector on purpose: this is the
"detector" seam described in CLAUDE.md - a second, differently-shaped way to
find things on screen, without the image-matching pipeline needing to know it exists.

Two OCR backends, tried in order:
1. tesserocr - talks to the Tesseract engine in-process via compiled bindings.
   Measured ~3.5-4.5x faster than pytesseract against real gameplay (~0.13s vs
   ~0.42-0.6s) by eliminating not just the subprocess-launch cost but also the
   temp-file I/O round-trip and per-call engine re-initialization that
   pytesseract's subprocess-per-call model requires. Needs a prebuilt wheel
   matching this exact Python version/platform (see requirements.txt) - there's
   no dev-toolchain build step, but if no matching wheel exists, it's unavailable.
2. pytesseract - launches a brand-new tesseract.exe subprocess on every call
   (~120-150ms of fixed overhead alone, before the temp-file round-trip, before
   the image's own processing cost). Slower, but works anywhere the Tesseract
   binary is installed, regardless of platform or Python version - the portable
   fallback if tesserocr isn't available.

Either way: exactly ONE OCR call per invocation, over the whole frame, using
Tesseract's own word/line grouping - never one call per candidate region.
Calling it once per region (tried early on) multiplies subprocess/call overhead
and can tank FPS by 10-20x. Callers (see main.py) should still throttle how
often this gets called at all - even the fast path costs far more than image matching.
"""
import difflib
import platform
import re
from pathlib import Path

import cv2 as cv
import numpy as np

LABEL_PAD_X = 10 #D2R (and most games) draw a semi-transparent background panel behind floating
LABEL_PAD_Y = 6  #item-name labels, sized a bit larger than the text - see _padded_box()


#--- Input preparation ---------------------------------------------------------------------------
#Tesseract expects a scanned page: dark text, light background, no texture. A game frame is the
#opposite - thin bright glyphs over dark, noisy, high-detail art - and handing it one raw is by
#far the biggest cause of "the text is right there and it isn't detected". Measured on a real
#Diablo II frame containing five legible rune labels, Tesseract read ZERO of them and returned 57
#junk fragments scraped off the terrain instead. Binarizing first read four of the five.
#
#It is also much FASTER, which is not the usual direction for an accuracy fix: Tesseract's cost
#scales with how much candidate "text" it thinks it can see, and thresholding deletes the texture
#that was generating all the false candidates. Same frame: 362ms -> 51ms, 83 words -> 7.
#
#NOT a game-specific hack, and deliberately not hardcoded on: "light text on a dark background"
#also describes subtitles, HUDs, dark-themed applications and night-time signage, while a document
#or a light-themed window is the exact opposite and must not be inverted. Hence the modes below,
#with AUTO deciding from the frame's own brightness so a caller that knows nothing about its
#input still gets the right one.
PREPROCESS_NONE = "none"                    #hand the frame to Tesseract untouched
PREPROCESS_LIGHT_ON_DARK = "light_on_dark"  #bright glyphs on dark: threshold, then invert
PREPROCESS_AUTO = "auto"                    #pick per frame from its median brightness

BRIGHT_TEXT_THRESHOLD = 150 #pixels brighter than this are treated as glyph, the rest as background.
                             #Tuned on real gameplay: 130-160 all worked, below 120 let terrain back
                             #in and above 170 dropped the fainter labels entirely.
DARK_FRAME_MEDIAN = 128 #a frame whose median pixel is darker than this is treated as light-on-dark


def _preprocess(frame, mode):
    """Returns the image to hand Tesseract - either the frame itself, or a binarized version.

    Never rescales, so every box coordinate Tesseract reports still refers to the caller's
    original pixel grid and needs no translation."""
    if mode == PREPROCESS_NONE:
        return frame

    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    if mode == PREPROCESS_AUTO:
        #Subsampled: the median only has to be roughly right to pick a branch, and every 8th
        #pixel gets that for ~1/64th of the cost on a full-screen frame.
        if np.median(gray[::8, ::8]) >= DARK_FRAME_MEDIAN:
            return frame #light background already - this is what Tesseract wants
    return 255 - cv.threshold(gray, BRIGHT_TEXT_THRESHOLD, 255, cv.THRESH_BINARY)[1]


def _find_tessdata_dir():
    if platform.system() == "Windows":
        default = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
        if default.exists():
            return str(default)
    return None #TODO: add default tessdata paths for macOS (varies by Homebrew install location)
                #when that platform is actually being worked on - falls back to pytesseract's
                #PATH-based lookup in the meantime, which already works cross-platform.


_TESSDATA_DIR = _find_tessdata_dir()

_tesserocr_api = None
try:
    import tesserocr
    from PIL import Image
    if _TESSDATA_DIR:
        _tesserocr_api = tesserocr.PyTessBaseAPI(path=_TESSDATA_DIR, psm=tesserocr.PSM.SPARSE_TEXT)
except Exception:
    pass #no matching wheel, or init failed for some other reason - fall through to pytesseract below

_pytesseract_available = False
if _tesserocr_api is None:
    import pytesseract

    # On Windows, the official Tesseract installer doesn't add itself to PATH by default.
    if platform.system() == "Windows" and _TESSDATA_DIR:
        pytesseract.pytesseract.tesseract_cmd = str(Path(_TESSDATA_DIR).parent / "tesseract.exe")

    # Tesseract is a separate program pytesseract shells out to - if it isn't installed,
    # every OCR call would throw and (left unguarded) take down the whole detection thread.
    # Check once at import time and just disable text matching instead of crashing.
    try:
        pytesseract.get_tesseract_version()
        _pytesseract_available = True
    except Exception:
        print(
            "WARNING: Tesseract OCR engine not found - text detection is disabled. "
            "Install it (see requirements.txt) to enable item-name matching."
        )

if _tesserocr_api is not None:
    print("text_detection: using tesserocr (in-process, fast path)")
elif _pytesseract_available:
    print("text_detection: using pytesseract (subprocess fallback, slower - "
          "see requirements.txt for the faster tesserocr wheel)")


DEFAULT_BOX_COLOR = (0, 255, 0) #(r, g, b) - the green boxes have always used, kept as the fallback
                                 #for any item with no [color] tag, or an unrecognized one

#Small named-color palette for targets.txt's optional trailing "[color]" tag. Deliberately
#spelled-out names ("darkorange"), not single letters ("o") - a single letter is ambiguous
#(does 'o' mean orange or olive?) and a typo in one is indistinguishable from a different valid
#letter, whereas a misspelled word just fails the dict lookup and falls back to green with a
#warning instead of silently picking the wrong color. The palette must never contain
#overlay.TRANSPARENT_COLOR (near-black, "#010203") - a box drawn in exactly that color would
#silently vanish, since that is the value the overlay treats as see-through. Magenta used to be
#the excluded one and is now fine; near-blacks are the ones to keep out.
NAMED_COLORS = {
    "green": (0, 255, 0),
    "red": (255, 0, 0),
    "blue": (0, 100, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "purple": (160, 32, 240),
    "orange": (255, 165, 0),
    "darkorange": (255, 140, 0),
    "pink": (255, 105, 180),
    "gold": (255, 215, 0),
    "white": (255, 255, 255),
}

_COLOR_TAG_RE = re.compile(r"\[([A-Za-z]+)\]\s*$")


def load_target_items(path):
    """Returns {item_name: {"to_collect": bool, "ignore": bool, "color": (r, g, b)}}.

    A trailing '*' marks an item "to collect" (see main.py's auto-collect thread) and a
    trailing "[color]" tag (e.g. "[purple]", checked against NAMED_COLORS) sets its detection
    box color, defaulting to DEFAULT_BOX_COLOR if omitted or unrecognized. Both are stripped
    before matching - OCR output never contains '*', '[', or ']' (see _clean_text), so leaving
    either in the name would mean that item could never fuzzy-match.

    A LEADING '-' marks an item "ignore": it takes part in matching but is never reported.
    That sounds pointless and is in fact the mechanism that makes fuzzy matching safe. Fuzzy
    matching always returns the CLOSEST listed name, so anything on screen that resembles a
    target but isn't listed gets silently attributed to whichever target it happens to resemble
    most - "Ral Rune" was reported as "Jah Rune" and clicked on, because no closer name existed.
    Listing the look-alike fixes that at the root: it now wins the match on its own merits and
    is then dropped. The '-' just spares the user a box drawn around every look-alike.

    This is general, not a Diablo II detail: any vocabulary with a shared suffix and a short
    distinguishing part (product codes, license plates, station names) needs the same trick."""
    items = {}
    seen_on_line = {}  # NAME -> the line number it was first defined on, for the duplicate warning
    with open(path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            ignore = line.startswith("-")
            if ignore:
                line = line[1:].strip()

            color = DEFAULT_BOX_COLOR
            color_match = _COLOR_TAG_RE.search(line)
            if color_match:
                color_name = color_match.group(1).lower()
                if color_name in NAMED_COLORS:
                    color = NAMED_COLORS[color_name]
                else:
                    print(f"WARNING: targets.txt: unrecognized color '{color_name}' "
                          f"(line: {line!r}) - using default green")
                line = line[:color_match.start()].strip()

            to_collect = line.endswith("*")
            name = line[:-1].strip() if to_collect else line
            key = name.upper()

            # A DUPLICATE NAME IS ALWAYS A MISTAKE, AND SILENTLY LETTING THE LAST ONE WIN HID A
            # REAL BUG FOR AN ENTIRE VERSION. "Rejuvenation Potion" was listed twice - once as a
            # normal target near the top and once as a "-" look-alike further down - so the
            # look-alike overwrote the target, and the item was matched and then discarded on
            # every single frame. From outside that is indistinguishable from "OCR cannot read
            # it", which is a completely different problem to go hunting for. It also silently
            # dropped the "[purple]" box colour from the first line.
            # This file is hand-edited, it is long, and the two halves are far apart on screen -
            # exactly the conditions where a plain dict assignment quietly discards what someone
            # wrote. Last-one-wins is kept (predictable, and the warning says what happened)
            # rather than guessing which line was meant.
            if key in seen_on_line:
                print(f"WARNING: targets.txt: '{name}' is listed twice "
                      f"(line {seen_on_line[key]} and line {line_number}); "
                      f"line {line_number} wins and the earlier one is ignored. "
                      f"Delete one of them.")
            seen_on_line[key] = line_number

            items[key] = {"to_collect": to_collect, "ignore": ignore, "color": color}
    return items


def _clean_text(raw_text):
    text = raw_text.upper()
    text = re.sub(r"[^A-Z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _get_words_tesserocr(frame):
    """Returns [(text, left, top, width, height, block_num, par_num, line_num), ...]."""
    #_preprocess may hand us a single-channel binarized image; PIL takes that directly.
    prepared = frame if frame.ndim == 2 else cv.cvtColor(frame, cv.COLOR_BGR2RGB)
    _tesserocr_api.SetImage(Image.fromarray(prepared))
    tsv = _tesserocr_api.GetTSVText(0) #same column layout as pytesseract's image_to_data DICT

    words = []
    for line in tsv.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) != 12:
            continue
        level, _page, block_num, par_num, line_num, _word_num, left, top, width, height, _conf, text = parts
        if level != "5" or not text.strip(): #level 5 = word; other levels are page/block/par/line placeholders
            continue
        words.append((text, int(left), int(top), int(width), int(height),
                       int(block_num), int(par_num), int(line_num)))
    return words


def _get_words_pytesseract(frame):
    """Returns [(text, left, top, width, height, block_num, par_num, line_num), ...]."""
    ocr_data = pytesseract.image_to_data(frame, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, word in enumerate(ocr_data["text"]):
        word = word.strip()
        if not word:
            continue
        words.append((word, ocr_data["left"][i], ocr_data["top"][i], ocr_data["width"][i], ocr_data["height"][i],
                       ocr_data["block_num"][i], ocr_data["par_num"][i], ocr_data["line_num"][i]))
    return words


def _group_words_by_line(words):
    """Groups per-word OCR output by Tesseract's own line/paragraph assignment - each group is
    [(word, left, top, width, height), ...] in reading order for one recognized line."""
    lines = {}
    for (word, left, top, width, height, block_num, par_num, line_num) in words:
        key = (block_num, par_num, line_num)
        lines.setdefault(key, []).append((word, left, top, width, height))
    return list(lines.values())


def _padded_box(words):
    """Bounding box of just these words, padded to approximate the game's label background
    panel (see LABEL_PAD_X/Y above)."""
    xs = [w[1] for w in words]
    ys = [w[2] for w in words]
    x2s = [w[1] + w[3] for w in words]
    y2s = [w[2] + w[4] for w in words]
    x, y = min(xs) - LABEL_PAD_X, min(ys) - LABEL_PAD_Y
    w, h = (max(x2s) - min(xs)) + 2 * LABEL_PAD_X, (max(y2s) - min(ys)) + 2 * LABEL_PAD_Y
    return max(x, 0), max(y, 0), w, h


def _required_cutoff(word, base_cutoff):
    """How closely one WORD of a target name has to match to count - see _match_ratio().

    Short words are especially prone to false-positive fuzzy matches. difflib's ratio is
    2*M/(lenA+lenB) - for a long word like "REJUVENATION", one stray mislettered character
    barely moves the ratio, so a fixed cutoff works well; for a 2-3 letter word, a single
    shared letter with ANY unrelated short OCR misread can already clear 0.75. So scale the
    required cutoff up as the word gets shorter, up to exact-match at length <= 3.

    Every word tolerates roughly one misread character, which for a 2- or 3-letter word is a very
    loose ratio (0.5 / 0.65) taken in isolation. It is not loose in practice, because it is only
    ever one of three checks: EVERY word of the name must pass, and the winner must additionally
    be unambiguous (see _match_ratio and the ambiguity rejection in find_text_matches). Observed
    on a real frame: Tesseract read "Ko Rune" as "Ke RUNE", which an exact rule silently dropped.

    THIS DEPENDS ON THE TARGET FILE BEING COMPLETE and is the reason "ignore" entries exist.
    Two 3-letter words differing by one character score 0.667 whether that character is an OCR
    misread ("GUL" read as "GU1") or a genuinely different item ("RAL" vs "MAL"). No cutoff can
    tell those apart, so tolerance here is only safe when the genuinely-different item is ALSO
    in the target list: a correctly-read "RAL" then scores 1.0 against its own entry and wins
    outright over 0.667 against "MAL", because the caller keeps the highest-scoring match. List
    the neighbours (as ignore entries if they shouldn't be reported) and the ambiguity resolves
    itself; leave them out and the nearest listed name wins by default, which is exactly the
    bug this whole mechanism exists to prevent. Tolerance matters because real game fonts are
    stylized display faces Tesseract was never trained on - Diablo II's is Exocet-derived - so
    a systematic one-character misread is the normal case, not an unlucky one.

    Words of 4+ characters get the plain base cutoff, NOT a stricter one. An earlier version
    demanded 0.9 up to length 6, which was carried over from when this ladder was applied to
    whole NAMES rather than to single words - applied per word it meant "RUNE" and "POTION" had
    to be read perfectly too, and it measurably cost real detections. It bought nothing, because
    protection against a wrong item comes entirely from the word that identifies it: once "GUL"
    must be exact, nothing else on the line can turn a Gul Rune into a different rune."""
    n = len(word)
    if n <= 2:
        return 0.5  #one misread character in a 2-letter word ("KO" read as "KE")
    if n == 3:
        return 0.65 #one misread character in a 3-letter word ("GUL" read as "GU1")
    return base_cutoff


#Set True for one session to find out WHY an item on screen isn't being detected. Prints every
#OCR'd line that came close to a target but was rejected, with the per-word breakdown showing
#which word failed and by how much - the only way to tell "Tesseract misread it" from "the
#cutoff is wrong" from "Tesseract never saw the text at all". Off by default: it prints a few
#lines per OCR pass, which is far too noisy to leave on during normal play.
MATCH_DEBUG = False
MATCH_DEBUG_FLOOR = 0.5 #don't report a rejection that wasn't even close


def _explain(text, name, base_cutoff):
    """Per-word score breakdown for one candidate/target pair, for MATCH_DEBUG output."""
    text_words, name_words = text.split(), name.split()
    if len(text_words) != len(name_words):
        despaced = difflib.SequenceMatcher(None, text.replace(" ", ""), name.replace(" ", "")).ratio()
        needed = max(_required_cutoff(w, base_cutoff) for w in name_words)
        return (f"word count {len(text_words)} vs {len(name_words)} -> despaced compare "
                f"{despaced:.2f} (needed {needed:.2f})")
    parts = []
    for text_word, name_word in zip(text_words, name_words):
        ratio = _word_ratio(text_word, name_word)
        needed = _required_cutoff(name_word, base_cutoff)
        parts.append(f"{text_word!r} vs {name_word!r} {ratio:.2f}"
                     f"{'' if ratio >= needed else f' FAILED (needed {needed:.2f})'}")
    return "; ".join(parts)


SHORT_WORD_LENGTH = 3 #at or below this, compare position-by-position - see _word_ratio()
#Floor for the differing-word-count path in _match_ratio(). High on purpose: that path only
#legitimately fires for a merge/split, which preserves every character and so scores near 1.0.
MERGED_WORD_CUTOFF = 0.9


def _word_ratio(text_word, name_word):
    """Similarity of one OCR'd word to one word of a target name.

    For SHORT, equal-length words this compares character POSITIONS rather than using difflib.
    difflib's ratio counts how many characters two strings have in common, not where they are,
    and on 2-3 letter words that loses the only information there is. Measured on a real frame:
    Tesseract read "Ko Rune" as "Ke RUNE", and difflib scored "KE" 0.5 against BOTH "KO" (a
    genuine one-character misread) and "EL" (nothing alike - it just also contains an E). Those
    tie, so the match was thrown out as ambiguous and a Ko Rune went uncollected. Comparing
    positions gives "KO" 0.5 and "EL" 0.0, which is the real answer.

    This is the better model of what OCR actually does: it SUBSTITUTES a character in place
    ("KO"->"KE", "GUL"->"GU1"), it does not transpose or shift them. difflib's order-preserving
    subsequence matching is built for edits that move text around, which is the wrong tool here.
    Longer words keep difflib, where having many characters in common really is good evidence
    and where a dropped or doubled letter (which does shift the rest) is common enough to matter."""
    if len(text_word) == len(name_word) and len(name_word) <= SHORT_WORD_LENGTH:
        return sum(1 for a, b in zip(text_word, name_word) if a == b) / len(name_word)
    return difflib.SequenceMatcher(None, text_word, name_word).ratio()


def _match_ratio(text, name, base_cutoff):
    """Returns the overall similarity ratio if `text` matches target `name`, else None.

    Matching is per WORD, not over the whole string, because a word shared between many target
    names otherwise drags a completely wrong one over the cutoff. Measured: "RAL RUNE" scores
    exactly 0.75 against "JAH RUNE", "LEM RUNE" and "GUL RUNE" - the shared " RUNE" is 5 of the
    8 characters, so it alone clears a 0.75 whole-string cutoff no matter what the other word
    says. (Same reason "KO RUNE" matched "LO RUNE" at 0.857.) No whole-string cutoff can fix
    this: "RAL RUNE" and "JAH RUNE" really are 75% identical as strings - the information that
    separates them is *which word* differs, and that is only visible word by word.

    So every word of the name must clear its own length-scaled cutoff (_required_cutoff): the
    3-letter word that actually identifies the item has to be right, and can no longer ride in
    on the generic one. The returned ratio is still the whole-string one, purely so callers can
    rank several passing candidates against each other the same way they did before."""
    text_words = text.split()
    name_words = name.split()

    if len(text_words) == len(name_words):
        for text_word, name_word in zip(text_words, name_words):
            if _word_ratio(text_word, name_word) < _required_cutoff(name_word, base_cutoff):
                return None
        return difflib.SequenceMatcher(None, text, name).ratio()

    # Differing word counts are not automatically a miss: Tesseract sometimes merges or splits
    # words ("JAHRUNE", "REJUVEN ATION"), which would lose a real detection if treated as one.
    # There is no word alignment to check in that case, so compare the whole string with spaces
    # removed (so a merge/split isn't itself counted as a difference).
    #
    # This path must be STRICTER than the per-word one, not looser, because it has no structure
    # left to verify - hence the MERGED_WORD_CUTOFF floor. A merge or split preserves all the
    # CHARACTERS, so a genuine one scores near 1.0 ("GULRUNE" vs "GULRUNE"). What the floor keeps
    # out is a candidate that is merely a PREFIX of the name: the caller tries every contiguous
    # run of words on a line, so the single word "FLAWLESS" off a Flawless Diamond gets compared
    # against "FLAWLESSRUBY" and scores 0.8 - enough to clear a per-word cutoff and report a gem
    # that is not on the list, boxing just the word "Flawless". A whole missing word is not a
    # transcription artifact and must not be forgiven like one.
    strictest = max(MERGED_WORD_CUTOFF, *(_required_cutoff(w, base_cutoff) for w in name_words))
    despaced = difflib.SequenceMatcher(None, text.replace(" ", ""), name.replace(" ", "")).ratio()
    if despaced < strictest:
        return None
    return difflib.SequenceMatcher(None, text, name).ratio()


def find_text_matches(frame, target_items, match_cutoff=0.75, preprocess=PREPROCESS_AUTO):
    """Returns a list of (x, y, w, h, matched_name, to_collect, color) for on-screen text
    matching target_items (the {item_name: {"to_collect", "color"}} dict from
    load_target_items()).

    Runs exactly one OCR call - see the module docstring for why and which backend. The frame is
    binarized first when it looks like light-text-on-dark (see _preprocess), which on real game
    frames is the difference between reading the labels and reading nothing at all."""
    frame = _preprocess(frame, preprocess)

    if _tesserocr_api is not None:
        try:
            words = _get_words_tesserocr(frame)
        except Exception as e:
            print(f"WARNING: OCR call failed, skipping this scan ({e})")
            return []
    elif _pytesseract_available:
        try:
            words = _get_words_pytesseract(frame)
        except pytesseract.TesseractError as e:
            # Tesseract is an external subprocess we don't control - an occasional transient
            # failure there shouldn't be allowed to take down the whole detection thread.
            print(f"WARNING: OCR call failed, skipping this scan ({e})")
            return []
    else:
        return []

    matches = []
    for line_words in _group_words_by_line(words):
        # Try every contiguous run of words in this line, not just the whole line - matching
        # (and boxing) only the words that actually make up the item name. Games often render
        # something else on the same OCR-perceived line right next to/below an item's name
        # (e.g. D2R's "Rune" suffix under a rune name), and Tesseract's line/paragraph grouping
        # doesn't consistently include or exclude it between passes. Matching whole-line text
        # made both detection AND the click point depend on that inconsistent grouping - a short
        # name like "GUL" would fail to match "GUL RUNE" outright (see _required_cutoff), and
        # when it did match, the box (and therefore where auto-collect clicks) would jump in
        # size depending on whatever else got swept into the line that pass.
        best = None #(name, to_collect, color, x, y, w, h)
        #LONGEST MATCH WINS, with the ratio only breaking ties - key is (words matched, ratio),
        #in that order. Ratio-first is wrong here, and both failure modes were observed:
        #  - "Full Rejuvenation Potion" read cleanly scores 1.000, and so does its own tail
        #    "Rejuvenation Potion" against the look-alike entry. Ratio-first called that a tie
        #    and suppressed the item entirely.
        #  - read as "Fvll Rejuvenation Potion" the full span scores 0.958 while the tail still
        #    scores 1.000, so ratio-first picked the look-alike outright and reported nothing.
        #Both are the same mistake: a shorter span matched against less text is not better
        #evidence just because it scores higher on what little it covers. A long sloppy span
        #cannot win by accident, because a span still has to clear every per-word cutoff (and
        #MERGED_WORD_CUTOFF when the word counts differ) before it is eligible at all.
        #Note how the first form hid itself: a slightly MISREAD label worked while a perfectly
        #read one failed, so improving OCR quality made detection worse.
        best_key = (0, 0.0)
        best_names = set() #targets tied at best_key - more than one genuinely means "can't tell"
        near_miss = None #(ratio, text, name) - best REJECTED pair on this line, for MATCH_DEBUG
        n = len(line_words)
        for start in range(n):
            for end in range(start + 1, n + 1):
                window = line_words[start:end]
                text = _clean_text(" ".join(w[0] for w in window))
                if not text:
                    continue
                for name in target_items:
                    # Not difflib.get_close_matches() and not a bare whole-string ratio - see
                    # _match_ratio() for why matching has to happen word by word.
                    ratio = _match_ratio(text, name, match_cutoff)
                    key = (len(window), ratio) if ratio is not None else None
                    if key is not None and key > best_key:
                        x, y, w, h = _padded_box(window)
                        info = target_items[name]
                        best = (name, info["to_collect"], info["color"], x, y, w, h)
                        best_key = key
                        best_names = {name}
                    elif key is not None and key == best_key and best is not None:
                        best_names.add(name)
                    elif MATCH_DEBUG and ratio is None:
                        raw = difflib.SequenceMatcher(None, text, name).ratio()
                        if raw >= MATCH_DEBUG_FLOOR and (near_miss is None or raw > near_miss[0]):
                            near_miss = (raw, text, name)
        if best and len(best_names) > 1:
            #Two or more different targets fit this text equally well, so there is no honest way
            #to say which it is - report nothing rather than pick one. This is what makes the very
            #loose short-word cutoffs safe: "LE" is one character off both "LO" and "EL", and
            #guessing would eventually send the mouse to the wrong item. Distinguishing genuinely
            #requires information this module does not have.
            if MATCH_DEBUG:
                print(f"[ocr ambiguous] matched {sorted(best_names)} equally well "
                      f"({best_key[1]:.3f} over {best_key[0]} word(s)) - reporting nothing")
        elif best:
            name, to_collect, color, x, y, w, h = best
            #An ignored target still had to WIN the match to get here - that is the whole point.
            #It absorbed a line that would otherwise have been misattributed to a real target.
            if not target_items[name].get("ignore"):
                matches.append((x, y, w, h, name, to_collect, color))
        elif MATCH_DEBUG and near_miss is not None:
            _, text, name = near_miss
            print(f"[ocr near-miss] read {text!r} -> closest target {name!r}: "
                  f"{_explain(text, name, match_cutoff)}")
    return matches
