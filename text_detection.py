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

LABEL_PAD_X = 10 #D2R (and most games) draw a semi-transparent background panel behind floating
LABEL_PAD_Y = 6  #item-name labels, sized a bit larger than the text - see _padded_box()


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


def load_target_items(path):
    """Returns {item_name: to_collect}. A trailing '*' on a line marks that item as
    "to collect" (see main.py's auto-collect thread) and is stripped before matching -
    OCR output never contains '*' (see _clean_text), so leaving it in the name would mean
    that item could never fuzzy-match."""
    items = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            to_collect = line.endswith("*")
            name = line[:-1].strip() if to_collect else line
            items[name.upper()] = to_collect
    return items


def _clean_text(raw_text):
    text = raw_text.upper()
    text = re.sub(r"[^A-Z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _get_words_tesserocr(frame):
    """Returns [(text, left, top, width, height, block_num, par_num, line_num), ...]."""
    rgb = cv.cvtColor(frame, cv.COLOR_BGR2RGB)
    _tesserocr_api.SetImage(Image.fromarray(rgb))
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


def _required_cutoff(item_length, base_cutoff):
    """Very short target names (e.g. 2-4 letter Diablo II rune names like 'Lo', 'Um', 'Ko')
    are especially prone to false-positive fuzzy matches. difflib's ratio is 2*M/(lenA+lenB) -
    for a long name like "FULL REJUVENATION POTION", one stray mismatched letter barely moves
    the ratio, so a fixed cutoff works well; for a 2-3 letter name, a single shared letter with
    ANY unrelated short OCR misread (stray HUD text, chat, icons) can already clear 0.75. Scale
    the required cutoff up as the name gets shorter - effectively exact-match at length <= 3,
    since fuzzy tolerance there causes far more false positives than it saves real misreads.
    This matters more than usual now that a match can drive real mouse clicks (auto-collect)."""
    if item_length <= 3:
        return 1.0
    if item_length <= 6:
        return max(base_cutoff, 0.9)
    return base_cutoff


def find_text_matches(frame, target_items, match_cutoff=0.75):
    """Returns a list of (x, y, w, h, matched_name, to_collect) for on-screen text matching
    target_items (an {item_name: to_collect} dict from load_target_items()).

    Runs exactly one OCR call - see the module docstring for why and which backend."""
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
        best = None #(name, to_collect, x, y, w, h)
        best_ratio = 0.0
        n = len(line_words)
        for start in range(n):
            for end in range(start + 1, n + 1):
                window = line_words[start:end]
                text = _clean_text(" ".join(w[0] for w in window))
                if not text:
                    continue
                # Not difflib.get_close_matches() - it only takes one cutoff for every
                # candidate, and a cutoff loose enough for long names is far too loose for
                # short ones (see _required_cutoff). Each target name needs its own,
                # length-scaled cutoff instead.
                for name in target_items:
                    ratio = difflib.SequenceMatcher(None, text, name).ratio()
                    if ratio > best_ratio and ratio >= _required_cutoff(len(name), match_cutoff):
                        x, y, w, h = _padded_box(window)
                        best = (name, target_items[name], x, y, w, h)
                        best_ratio = ratio
        if best:
            name, to_collect, x, y, w, h = best
            matches.append((x, y, w, h, name, to_collect))
    return matches
