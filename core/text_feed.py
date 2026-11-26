"""Answering "which lines of this scrolling text box are NEW since I last looked?"

The question: a box of text that grows at the bottom and scrolls up - a game's chat window, a log
pane, a terminal, a list of subtitles. It is handed a picture of the box each time and answers with
the lines that arrived since the previous picture, as images, ready for OCR. Only the new lines are
ever read, so OCR (about 0.1 s a line) is paid once per message, not once per look.

WHY ITS OWN DETECTOR: none of the others answers this. text_detection.py reads what text says, but
reading the whole box every time cannot tell an old "You receive loot: [Snapper]" from a new one -
the same words, one line higher. What tells them apart is MOVEMENT: the old lines slid up. This
module matches each line of the new picture against the lines of the old one (by their pixels) and
reports whatever is left over at the bottom.

IT KNOWS NOTHING ABOUT ANY GAME. It needs one number, the line pitch in pixels (WoW's chat at
1920x1080 with the default font: 18). Pointed at a terminal window or a subtitle strip it works the
same with a different pitch; estimate_pitch() measures one from a picture.

HOW, in order (each step measured on a real WoW chat box, see docs/wow_fishing.md):
  1. Ink: the brightest channel (coloured text is bright in its OWN channel - EH#48: WoW's loot
     green is 0,160,0, which a grey conversion turns to 94), then a morphological top-hat, which
     keeps thin bright strokes and drops smooth backgrounds. The box is see-through, and behind it
     can be dark water or bright fog; a fixed brightness threshold works on one and not the other.
  2. Lines: rows of text sit at a fixed pitch, with a near-empty pixel row between them. The phase
     is the offset where those gap rows hold the least ink, so lines are cut exactly between
     glyphs even when descenders almost touch the next line. A line cut off by the box's top or
     bottom edge is ignored until it is whole.
  3. Matching: two lines are "the same" when their ink correlates (TM_CCOEFF_NORMED, a couple of
     pixels of slack). Correlation ignores brightness, so a line that is FADING OUT (WoW fades old
     chat) still matches itself.
  4. Alignment: the new picture should be the old one with its top n lines gone (scrolled off or
     faded) and new lines below. The smallest n that makes every remaining old line match is
     taken, and the lines below them are new.
  5. Junk on top: a line that is FADING OUT can still be bright enough to count as a line but too
     faint to match itself (measured on the recording over grass: at 35-45% of its brightness),
     and the empty top of a box can show a line-like streak (a nameplate, a glint on water). Both
     sit ABOVE the lines that still match. So when nothing lines up, up to `max_unaligned` top
     lines are set aside - but only lines that match no old line at all (a line seen before is
     never junk: setting it aside could hide a real repeat) - and the alignment is tried again.

THE PITCH MUST BE RIGHT. Lines cut at the wrong pitch drift a little further off on every line, and
the box stops lining up with itself (measured on the owner's chat, 16 px apart, read at 18: the box
was "cannot tell" every few seconds and two catches of three were missed). estimate_pitch()
measures it; a caller that was given the pitch should check it against a picture now and then.

LINES ALIKE BUT FOR A WORD can be taken for each other: correlation is over the whole line, so two
lines sharing most of their letters ("door 3 opened" / "door 1 opened" in a log) can score above
the threshold. WoW's chat measured safe (different lines at most 0.58, loot lines for different
fish 0.47), but a log of near-identical messages would need a finer comparison.

THE ONE THING PIXELS CANNOT TELL, said plainly: if every visible line is the same line (a box full of
"You receive loot: [Raw Longjaw Mud Snapper]"), one more of it arriving changes nothing on screen.
feed() still answers "nothing new" there (it cannot invent a line), but sets `ambiguous` = True: "a
repeat of the bottom line may have arrived and I could not see it". A caller that knows something
else - wow.py knows it just reeled in a fish - can use that. Every other case is unambiguous.

THREE ANSWERS (CLAUDE.md, "None is a third answer"): feed() returns a list of new line images (empty
= looked, nothing new), or None = "cannot tell" - the first look (nothing to compare with yet), or
a picture that does not line up with the last one at all (the box was scrolled by hand, a window
covered it, it reappeared after being hidden). After None it starts over from the current picture;
it never guesses that a whole box of lines is new.
"""
from __future__ import annotations

import cv2 as cv
import numpy as np

INK_THRESHOLD = 60        #top-hat value of a glyph stroke; the gaps between lines measured 0-15
MATCH_THRESHOLD = 0.75    #same line, two frames apart, over moving grass: 0.90-1.00 measured;
                          #different lines, even sharing "Cerellean Whiteclaw says:": at most 0.58
SLACK_PX = 2              #how far a line may sit from where it was and still match itself
MAX_UNALIGNED_LINES = 3   #a picture that matches nothing old may still be new lines arriving in an
                          #emptied box - but only a few (one message, wrapped). More is "cannot tell".


class Line:
    """One line of the box: its ink (for matching) and its picture (for reading)."""

    __slots__ = ("top", "ink", "image")

    def __init__(self, top, ink, image):
        self.top = top          #y of the line's top in the box, in pixels
        self.ink = ink          #float32 ink, SLACK_PX taller on each side than the line
        self.image = image      #the line as it looked (a view of the frame; copy to keep)


def ink_image(frame, pitch, dark_text=False):
    """The text strokes of a BGR (or grey) picture as a 0-255 uint8 image: brightest channel, then a
    top-hat about half a line tall, so smooth backgrounds of any brightness drop out.
    dark_text: for dark writing on a light page (a document, a log window) - the picture is
    turned negative first (its darkest channel), so the same steps apply."""
    if dark_text:
        gray = 255 - (frame.min(axis=2) if frame.ndim == 3 else frame)
    else:
        gray = frame.max(axis=2) if frame.ndim == 3 else frame
    size = max(3, int(round(pitch * 0.4)) | 1)
    kernel = cv.getStructuringElement(cv.MORPH_RECT, (size, size))
    return cv.morphologyEx(np.ascontiguousarray(gray), cv.MORPH_TOPHAT, kernel)


def line_phase(profile, pitch):
    """The offset (0..pitch-1) of the gap rows: where the rows pitch apart hold the least ink.

    With wide gaps (a document's widely spaced lines) several offsets are equally empty; the MIDDLE
    of that run is taken, so a line is never cut at its very first or last pixel row and the cut
    lands in the same place on every frame.
    """
    cost = [float(profile[p::pitch].mean()) if profile[p::pitch].size else 0.0 for p in range(pitch)]
    lowest = min(cost)
    empty = [c <= lowest + 1e-9 for c in cost]
    if all(empty):
        return 0
    #The longest run of equally-empty offsets, going round the circle (offset pitch-1 is next to 0).
    best_start, best_length = 0, 0
    for start in range(pitch):
        if not empty[start] or empty[start - 1]:
            continue
        length = 0
        while length < pitch and empty[(start + length) % pitch]:
            length += 1
        if length > best_length:
            best_start, best_length = start, length
    return (best_start + (best_length - 1) // 2) % pitch


def estimate_pitch(frame, low=10, high=48):
    """The line pitch of a picture of text, in pixels, or None if it cannot tell (no lines, or one).

    For setting the box up once - the live reader takes the pitch as a number, because a guess made
    on every frame could change its mind between two frames. Measured: 18 on WoW's chat at
    1920x1080, both on dark water and on bright fog.
    """
    ink = ink_image(frame, 18)
    profile = (ink > INK_THRESHOLD).sum(axis=1).astype(np.float64)
    if profile.size < 2 * low or profile.max() <= 0:
        return None
    profile -= profile.mean()
    scores = {}
    for pitch in range(low, min(high, profile.size // 2) + 1):
        scores[pitch] = float(np.dot(profile[:-pitch], profile[pitch:])) / (profile.size - pitch)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return None
    #Twice the pitch lines up as well as the pitch itself, and can even score higher (measured: 36
    #beat 18 on a box of wrapped two-line messages). So a whole fraction of the best that scores
    #nearly as well is the real pitch.
    for divisor in (3, 2):
        smaller = int(round(best / divisor))
        if smaller in scores and scores[smaller] >= 0.6 * scores[best]:
            return smaller
    return best


def split_lines(frame, pitch, ink=None, dark_text=False):
    """The whole lines of text in a picture, top to bottom, as Line objects."""
    if frame is None or frame.size == 0 or frame.ndim not in (2, 3):
        return []
    if ink is None:
        ink = ink_image(frame, pitch, dark_text)
    height, width = ink.shape
    strokes = ink > INK_THRESHOLD
    profile = strokes.sum(axis=1)
    if height < pitch:
        return []
    phase = line_phase(profile, pitch)
    min_ink = 2 * pitch                  #a short real line ("apart...") has 150+; noise under 40
    padded = cv.copyMakeBorder(ink, SLACK_PX, SLACK_PX, 0, 0, cv.BORDER_CONSTANT, value=0)
    lines = []
    for top in range(phase, height - pitch + 1, pitch):
        if strokes[top:top + pitch].sum() < min_ink:
            continue
        band = padded[top:top + pitch + 2 * SLACK_PX]
        #Faint ink is zeroed before matching: it is the background (grass, waves) showing through,
        #and it changes as the camera moves. Measured on a short line over grass, same text in
        #two frames: 0.69 with it, 0.90 without; different lines stay under 0.58 either way.
        lines.append(Line(top, np.where(band > INK_THRESHOLD, band, 0).astype(np.float32),
                          frame[top:top + pitch]))
    return lines


def same_line(a, b, threshold=MATCH_THRESHOLD):
    """Do two Lines show the same text? Correlation of their ink, with SLACK_PX of slack each way."""
    s = SLACK_PX
    template = b.ink[s:-s, s:b.ink.shape[1] - s]
    if template.size == 0 or template.shape[1] > a.ink.shape[1]:
        return False
    if not template.any() or not a.ink.any():
        return False
    score = cv.matchTemplate(a.ink, template, cv.TM_CCOEFF_NORMED)
    return bool(np.nanmax(score) >= threshold)


class TextFeed:
    """Feed it the box's picture every so often; it returns the lines that are new since last time.

    pitch: the line pitch in pixels, at the size the pictures are handed in.
    dark_text: dark writing on a light background (a document, a log) instead of a game's light
        text over the world.
    After each feed(): `lines` holds the current picture's lines (top to bottom), and `ambiguous`
    is True when "one more copy of the bottom line arrived" would explain the picture equally well
    (see the module docstring - the one case pixels cannot settle).
    """

    def __init__(self, pitch, threshold=MATCH_THRESHOLD, max_unaligned=MAX_UNALIGNED_LINES,
                 dark_text=False):
        self.pitch = int(pitch)
        self.dark_text = dark_text
        if self.pitch < 4:
            raise ValueError(f"line pitch must be at least 4 pixels, got {pitch}")
        self.threshold = threshold
        self.max_unaligned = int(max_unaligned)
        self.lines = None        #None until the first picture
        self.ambiguous = False

    def reset(self):
        self.lines = None
        self.ambiguous = False

    def feed(self, frame):
        """New lines since the last call, oldest first, as images - [] if none, None if it cannot
        tell (first picture, or a picture that does not line up with the last; it starts over)."""
        self.ambiguous = False
        if frame is None or frame.size == 0:
            return None
        current = split_lines(frame, self.pitch, dark_text=self.dark_text)
        previous = self.lines
        if previous is None:
            self.lines = current
            return None
        if not current:
            #An empty box: everything faded, or the box was hidden. Keep the old lines when more
            #than one vanished at once (WoW fades lines one by one, oldest first; hiding the chat
            #takes them all), so they are recognised, not re-counted, when they come back.
            if len(previous) <= 1:
                self.lines = current
            return []
        aligned = self._align(previous, current)
        if aligned is None:
            if len(current) <= self.max_unaligned:
                #Nothing old is left: the box was empty (or fully faded) and these just arrived.
                self.ambiguous = False
                self.lines = current
                return [line.image for line in current]
            self.lines = current
            return None
        dropped, junk = aligned
        rest = current[junk:]
        kept = len(previous) - dropped
        new = rest[kept:]
        self.ambiguous = self._fits(previous, rest, dropped + 1)
        self.lines = current
        return [line.image for line in new]

    def _fits(self, previous, current, dropped):
        """Does "the top `dropped` old lines are gone, the rest are still here" fit the picture?"""
        kept = len(previous) - dropped
        if kept < 0:
            return False
        if kept == 0:
            return len(current) <= self.max_unaligned
        if len(current) < kept:
            return False
        return all(same_line(previous[dropped + i], current[i], self.threshold) for i in range(kept))

    def _align(self, previous, current):
        """(old lines gone from the top, junk lines set aside at the top of the new picture), or
        None. The fewest of both: with no junk this is exactly the plain alignment, so the junk
        step only ever turns a "cannot tell" into an answer, never changes an answer."""
        for junk in range(min(self.max_unaligned, len(current) - 1) + 1):
            if junk and self._seen(previous, current[junk - 1]):
                return None              #a line seen before is not junk - see the module docstring
            rest = current[junk:]
            for dropped in range(len(previous)):
                if self._fits(previous, rest, dropped):
                    return dropped, junk
        return None

    def _seen(self, previous, line):
        """Does this line match any line of the last picture?"""
        return any(same_line(old, line, self.threshold) for old in previous)
