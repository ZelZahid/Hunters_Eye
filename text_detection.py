"""
On-screen text detector: OCRs the whole frame in a single Tesseract call and
fuzzy-matches recognized lines against a target item list.

Kept separate from main.py's image-template detector on purpose: this is the
"detector" seam described in CLAUDE.md - a second, differently-shaped way to
find things on screen, without the image-matching pipeline needing to know it exists.

Design note: pytesseract launches a brand-new tesseract.exe subprocess on every
call (~120-150ms of fixed overhead, independent of image size). Calling it once
per candidate region (e.g. once per on-screen icon/UI element) multiplies that
overhead and can tank FPS by 10-20x. So this always does exactly ONE OCR call
per invocation, over the whole frame, using Tesseract's own word/line grouping
(image_to_data) instead of pre-cropping candidate regions ourselves.
Callers (see main.py) should still throttle how often they call this at all -
one ~150ms call per frame is still far more expensive than image matching.
"""
import difflib
import platform
import re
from pathlib import Path

import pytesseract

# D2R (and most games) draw a semi-transparent background panel behind floating item-name
# labels, sized a bit larger than the text itself for legibility. Tesseract's word boxes only
# cover the glyphs, not that panel, so the box would look visibly too small/tight without this
# padding. These are estimates (in native screen pixels, since OCR runs at OCR_CAPTURE_SCALE=1.0
# in main.py) - tune against real gameplay if the box still doesn't match the panel's edges.
LABEL_PAD_X = 10
LABEL_PAD_Y = 6

# On Windows, the official Tesseract installer doesn't add itself to PATH by
# default. Point pytesseract at the default install location if it exists and
# nothing is already on PATH - avoids every Windows user having to configure this by hand.
if platform.system() == "Windows":
    _default_windows_path = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if _default_windows_path.exists():
        pytesseract.pytesseract.tesseract_cmd = str(_default_windows_path)

# Tesseract is a separate program pytesseract shells out to - if it isn't installed,
# every OCR call would throw and (left unguarded) take down the whole detection thread.
# Check once at import time and just disable text matching instead of crashing.
try:
    pytesseract.get_tesseract_version()
    _TESSERACT_AVAILABLE = True
except Exception:
    _TESSERACT_AVAILABLE = False
    print(
        "WARNING: Tesseract OCR engine not found - text detection is disabled. "
        "Install it (see requirements.txt) to enable item-name matching."
    )


def load_target_items(path):
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def _clean_text(raw_text):
    text = raw_text.upper()
    text = re.sub(r"[^A-Z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _group_words_into_lines(ocr_data):
    """Groups pytesseract's per-word output back into per-line (text, x, y, w, h) tuples."""
    lines = {}
    for i, word in enumerate(ocr_data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (ocr_data["block_num"][i], ocr_data["par_num"][i], ocr_data["line_num"][i])
        lines.setdefault(key, []).append(
            (word, ocr_data["left"][i], ocr_data["top"][i], ocr_data["width"][i], ocr_data["height"][i])
        )

    results = []
    for words in lines.values():
        text = " ".join(w[0] for w in words)
        xs = [w[1] for w in words]
        ys = [w[2] for w in words]
        x2s = [w[1] + w[3] for w in words]
        y2s = [w[2] + w[4] for w in words]
        x, y = min(xs) - LABEL_PAD_X, min(ys) - LABEL_PAD_Y
        w, h = (max(x2s) - min(xs)) + 2 * LABEL_PAD_X, (max(y2s) - min(ys)) + 2 * LABEL_PAD_Y
        results.append((text, max(x, 0), max(y, 0), w, h))
    return results


def find_text_matches(frame, target_items, match_cutoff=0.75):
    """Returns a list of (x, y, w, h, matched_name) for on-screen text matching target_items.

    Runs exactly one Tesseract call - see the module docstring for why."""
    if not _TESSERACT_AVAILABLE:
        return []

    try:
        ocr_data = pytesseract.image_to_data(frame, config="--psm 11", output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError as e:
        # Tesseract is an external subprocess we don't control - an occasional transient
        # failure there shouldn't be allowed to take down the whole detection thread.
        print(f"WARNING: OCR call failed, skipping this scan ({e})")
        return []

    matches = []
    for (raw_text, x, y, w, h) in _group_words_into_lines(ocr_data):
        text = _clean_text(raw_text)
        if not text:
            continue
        close = difflib.get_close_matches(text, target_items, n=1, cutoff=match_cutoff)
        if close:
            matches.append((x, y, w, h, close[0]))
    return matches
