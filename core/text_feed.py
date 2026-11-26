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
  2. Lines: rows of text sit at a fixed pitch. Each line's BODY (its small letters, half a line
     tall) holds most of the ink; lines are cut half a line away from the body's middle, so each
     keeps its own ascenders and descenders and the cut does not move between frames (line_phase()
     says why not "the emptiest row"). A line cut off by the box's top or
     bottom edge is ignored until it is whole. Each line is only as wide as its text (the leftmost
     run of ink): a scroll bar or a button over the box's right side stays put while the text
     scrolls past it, and is cut off rather than compared (text_end()).
  3. Matching: two lines are "the same" when their ink correlates (TM_CCOEFF_NORMED, a quarter
     of a line of slack up and down, a couple of pixels sideways). Correlation ignores brightness, so a line that is FADING OUT (WoW fades old
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
GAP_PITCHES = 2           #a gap this many line pitches wide ends a line's text: WoW's widest gap
                          #between words is about 5 px at a 16 px pitch; anything past 32 px of
                          #nothing is not the same line (see text_end())
LINE_START_FRACTION = 0.5 #a row whose ink starts in the right half of the box is not a line of text
MAX_UNALIGNED_LINES = 3   #a picture that matches nothing old may still be new lines arriving in an
                          #emptied box - but only a few (one message, wrapped). More is "cannot tell".


class Line:
    """One line of the box: its ink (for matching) and its picture (for reading)."""

    __slots__ = ("top", "ink", "image", "text", "slack", "at_edge")

    def __init__(self, top, ink, image, end=None, slack=SLACK_PX, at_edge=False):
        self.top = top          #y of the line's top in the box, in pixels
        self.at_edge = at_edge  #its ink runs into the box's right edge (see same_line)
        self.slack = slack      #rows of ink above and below the line, for matching (line_slack)
        self.ink = ink          #float32 ink of the text only, `slack` taller on each side
        self.image = image      #the whole row of the box as it looked (a view; copy to keep)
        self.text = image[:, :end] if end is not None else image   #...only as far as the text


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
    """The offset (0..pitch-1) to cut lines at: half a line away from the middle of the text.

    Every line has a BODY - the rows of its small letters, half a line tall, holding most of the
    ink - and between two bodies a band of thin rows: the descenders of the line above (g, j, p),
    the gap, the ascenders of the line below. The body is found as the half-line window of rows
    (pitch apart) with the most ink, and the cut goes opposite its middle, so each line keeps its
    own ascenders and descenders, whatever its font.

    NOT "the emptiest row", which is what this did first: the thin rows hold a pixel or two each,
    nearly tied (0.7 / 1.0 / 1.5 / 1.0 pixels a row on the owner's 16 px chat), so the emptiest one
    changed from frame to frame. The cut jumped 3 px - lines stopped matching themselves - and
    landed above a line's descenders as often as below them, slicing them off ('Raw Lonaiaw Mud
    Snanperl', measured). The body is many times inkier than anything around it, so its place does
    not change between frames. On WoW's default font (18 px) it gives the same cut as before.
    """
    cost = np.array([float(profile[p::pitch].mean()) if profile[p::pitch].size else 0.0
                     for p in range(pitch)])
    if not cost.any():
        return 0
    width = max(1, pitch // 2)
    ring = np.concatenate([cost, cost[:width - 1]])
    sums = np.convolve(ring, np.ones(width), mode="valid")[:pitch]
    start = int(np.argmax(sums))
    return (start + width // 2 + pitch // 2) % pitch


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
    #at least half as well is the real pitch. Half, not more: 16 scored 0.55 of 48 on a box of the
    #owner's chat lines (lines of very different lengths), and was read at 48 for seconds. A real
    #pitch scores far below zero at a half or a third of itself (a line's middle against a gap).
    for divisor in (3, 2):
        smaller = int(round(best / divisor))
        if smaller in scores and scores[smaller] >= 0.5 * scores[best]:
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
    slack = line_slack(pitch)
    padded = cv.copyMakeBorder(ink, slack, slack, 0, 0, cv.BORDER_CONSTANT, value=0)
    lines = []
    for top in range(phase, height - pitch + 1, pitch):
        end = text_end(strokes[top:top + pitch], pitch)
        if end is None or strokes[top:top + pitch, :end].sum() < min_ink:
            continue
        band = padded[top:top + pitch + 2 * slack, :end]
        #Faint ink is zeroed before matching: it is the background (grass, waves) showing through,
        #and it changes as the camera moves. Measured on a short line over grass, same text in
        #two frames: 0.69 with it, 0.90 without; different lines stay under 0.58 either way.
        lines.append(Line(top, np.where(band > INK_THRESHOLD, band, 0).astype(np.float32),
                          frame[top:top + pitch], end, slack, end > width - pitch))
    return lines


def line_slack(pitch):
    """How many pixels up or down a line may sit from where it was and still match itself: a
    quarter of a line (4 px at 16), at least SLACK_PX. A quarter, because the cut between lines can
    move by up to 3 px between two frames on a small font (see line_phase), and a line one whole
    pitch away is a different line."""
    return max(SLACK_PX, pitch // 4)


def text_end(strokes, pitch):
    """Where a line's text ends (a column index, exclusive), or None if this row holds no text.

    A line's text is its LEFTMOST run of ink: a gap of more than GAP_PITCHES line pitches ends it,
    and anything further right is not part of it - a scroll bar, a button or an icon drawn over the
    box. Those stay put while the text scrolls past them, so left in, the same line no longer
    matched itself one line higher, and an icon on a row of its own was a "line" that never moved
    (the owner's chat, 2026-11-25: a glowing button below the last line, and the box's scroll
    arrows beside every line). A row whose first ink starts past LINE_START_FRACTION of the width
    is not text either: that is the same kind of thing with no text to its left.
    """
    columns = np.flatnonzero(strokes.any(axis=0))
    if columns.size == 0 or columns[0] > strokes.shape[1] * LINE_START_FRACTION:
        return None
    gaps = np.flatnonzero(np.diff(columns) > GAP_PITCHES * pitch)
    last = columns[gaps[0]] if gaps.size else columns[-1]
    return int(min(strokes.shape[1], last + 1 + SLACK_PX))


def same_line(a, b, threshold=MATCH_THRESHOLD):
    """Do two Lines show the same text? Correlation of their ink, with a few pixels of slack each
    way (line_slack() up and down, SLACK_PX sideways)."""
    s, v = SLACK_PX, b.slack
    a_ink, b_ink = a.ink, b.ink
    if a.at_edge or b.at_edge:
        #A long line ends right where a scroll bar or a frame is drawn at the box's edge, with too
        #small a gap to tell them apart, so what is joined onto its end changes as it scrolls past
        #(415 px of text in one frame, 443 with the scroll arrows in the next - it no longer
        #matched itself). Compared over the width both have; a line this long that agrees over
        #all of it is the same line.
        width = min(a_ink.shape[1], b_ink.shape[1])
        a_ink, b_ink = a_ink[:, :width], b_ink[:, :width]
    template = b_ink[v:b_ink.shape[0] - v, s:b_ink.shape[1] - s]
    if template.size == 0 or not template.any() or not a_ink.any():
        return False
    searched = a_ink
    if template.shape[1] > searched.shape[1]:
        #Lines are only as wide as their text, so a longer line is padded with nothing to compare:
        #its extra letters then simply fail to match (a different line scores low, as it should).
        searched = cv.copyMakeBorder(searched, 0, 0, 0, template.shape[1] - searched.shape[1],
                                     cv.BORDER_CONSTANT, value=0)
    score = cv.matchTemplate(searched, template, cv.TM_CCOEFF_NORMED)
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
            if len(current) <= self.max_unaligned and len(previous) <= self.max_unaligned:
                #Nothing old is left: the box was empty (or fully faded) and these just arrived.
                #Only from a box that was (nearly) empty already: WoW fades its lines one at a
                #time, so a full box does not vanish at once while a line arrives - a full box
                #replaced by a few lines that match nothing is something passing behind it.
                self.ambiguous = False
                self.lines = current
                return [line.text for line in current]
            self.lines = current
            return None
        dropped, junk = aligned
        rest = current[junk:]
        kept = len(previous) - dropped
        new = rest[kept:]
        self.ambiguous = self._fits(previous, rest, dropped + 1)
        self.lines = current
        return [line.text for line in new]

    def _fits(self, previous, current, dropped):
        """Does "the top `dropped` old lines are gone, the rest are still here" fit the picture?

        The lines must match AND have moved the right distance: with n new lines below them, a
        box that grows at the bottom (a game chat) moves every kept line up by n lines; one that
        fills from the top (a terminal not yet full) does not move them at all. Anything else is
        a coincidence of look-alike lines - in a chat full of the same loot line, the old bottom
        line "matches" the new TOP line, and without this check most of the box was reported as
        new (three Snappers counted at once, 2026-11-25)."""
        kept = len(previous) - dropped
        if kept < 0:
            return False
        if kept == 0:
            return len(current) <= self.max_unaligned
        if len(current) < kept:
            return False
        arrived = len(current) - kept
        moved = previous[dropped].top - current[0].top
        slack = line_slack(self.pitch) + 1
        if abs(moved) > slack and abs(moved - arrived * self.pitch) > slack:
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
