"""
Transparent, click-through overlay: draws detection boxes directly on top of
whatever's on screen (the game), instead of showing a separate mirrored window.

Windows-only for now. It's built from a Tk window using two Windows-specific
tricks: Tk's "-transparentcolor" attribute (a real Windows Tk feature - makes a
chosen color see-through AND click-through) plus a Win32 WS_EX_TRANSPARENT
style bit (via pywin32) layered on top so even the drawn box outlines never
intercept a click - the game underneath always gets it. Neither trick exists
on macOS; that needs a different implementation later (e.g. an NSWindow via
pyobjc) - see CLAUDE.md.
"""
import platform
import time
import tkinter as tk
import tkinter.font as tkfont

#The colour key: every pixel of EXACTLY this colour is invisible and click-through.
#
#Near-black, not the traditional magenta, and the reason is antialiasing. Windows' subpixel font
#smoothing blends a glyph's edges into whatever is beneath, and beneath is this colour wherever
#the plate is stippled - so the key's hue is smeared around every piece of text on the overlay.
#With #ff00ff that produced a visible red/pink glow around each word (measured: the ring of
#mid-brightness pixels around a label averaged +37 red-and-blue over green). Blacking out the
#ring with an outline only moved the glow one pixel further out, because the outline's own edges
#then blended into the magenta. A key that is visually black makes the smear look like an
#ordinary drop shadow instead, which fixes it everywhere at once rather than per-element.
#
#NOT #000000, so that genuinely-black drawings (the text outline below) stay visible - only this
#exact value disappears. THE CONSTRAINT IS NOW INVERTED FROM WHAT IT USED TO BE: magenta is a
#perfectly usable box colour, and near-blacks are the ones that would silently vanish.
TRANSPARENT_COLOR = "#010203"

#Layout for the optional debug panel (see draw_panel). Deliberately positioned by a FRACTION of
#the window height rather than a pixel offset, so it lands in the same visual place at any
#resolution. Kept away from the very top/bottom edges, where games put their own HUD chrome.
PANEL_MARGIN_X = 24
PANEL_TOP_FRACTION = 0.30
PANEL_ROW_HEIGHT = 22
PANEL_TITLE_GAP = 26
PANEL_BAR_OFFSET = 90 #x offset of the bar from the label, in pixels
PANEL_BAR_WIDTH = 130
PANEL_VALUE_OFFSET = PANEL_BAR_OFFSET + PANEL_BAR_WIDTH + 12 #x offset of a row's value text
PANEL_BAR_HEIGHT = 10
PANEL_FONT = ("Consolas", 11, "bold")   #monospace, so a changing number doesn't shift the text
PANEL_TITLE_FONT = ("Consolas", 11, "bold")
PANEL_TITLE_COLOR = (170, 170, 170)
PANEL_LABEL_COLOR = (220, 220, 220)
PANEL_BAR_TRACK_COLOR = (90, 90, 90)
PANEL_WIDTH = 300 #MINIMUM plate width; it grows to fit anything wider - see draw_panel(). A
                   #minimum rather than a fixed size so the plate does not visibly resize as an
                   #ordinary reading goes "9%" -> "100%", while a long status message still fits.
PANEL_PAD = 10
PANEL_BG_COLOR = (16, 16, 20)
PANEL_BORDER_COLOR = (70, 70, 80)
#The window's transparency is a colour KEY - a pixel is either fully invisible or fully opaque,
#so there is no alpha channel to set the plate to 50% with. A stipple gives the same result the
#way this has always been done on colour-keyed surfaces: paint every other pixel in a checker and
#leave the rest as the key colour, which shows the game through. "gray50" is a built-in Tk bitmap
#and costs nothing to draw. Set to None for a fully solid plate (more readable, hides more game).
PANEL_BG_STIPPLE = "gray50"
#Ring each glyph in black by drawing it once per offset before the real glyph. NOW EMPTY - i.e.
#off - and that is a measured change, not a cleanup. It was added to fight MAGENTA fringing: the
#stipple leaves key-coloured holes behind every glyph and Windows' subpixel smoothing blends the
#glyph edges into them, so with a magenta key the text rendered visibly pink and the ring of black
#pixels was what stopped it. TRANSPARENT_COLOR is near-black now, so the smear is already dark -
#the outline was painting black next to black. Re-measured on real renders over both a dark and a
#bright backdrop: 8 offsets vs 4 vs none gave glyph contrast 183.7 / 183.6 / 183.4 on dark and
#181.3 / 181.0 / 180.4 on bright, with an identical count of lit pixels. No visible benefit left.
#It was not free: it drew every label NINE times, making the panel 1.66ms per repaint against
#0.605ms without it, and the panel is the overlay's most expensive layer.
#IF TRANSPARENT_COLOR IS EVER MOVED BACK TO A SATURATED COLOUR, PUT THESE BACK - the eight
#neighbours are ((-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)).
PANEL_TEXT_OUTLINE_OFFSETS = ()
#WHY THE PANEL HAS A SOLID BACKGROUND, when the rest of the overlay deliberately doesn't:
#the window's transparency is a color KEY, not real alpha - a pixel is either exactly
#TRANSPARENT_COLOR and fully invisible, or fully opaque. Text drawn straight onto that
#background gets antialiased by Windows' subpixel font rendering AGAINST magenta, and on thin
#11px glyphs the magenta fringe dominates the stroke: measured on a real render, the glyph
#cores were the right color but the mean lit pixel came out (204, 140, 201) RGB - visibly pink.
#Drawing a dark plate first gives the text something sane to blend into. Detection boxes don't
#need this because a 2px rectangle outline isn't antialiased the same way, and because covering
#the game where an item is lying would defeat the point of them.


#HOW OFTEN THIS WINDOW IS ALLOWED TO REPAINT, which is deliberately NOT how often it is asked to.
#
#This is a fullscreen, always-on-top, layered (WS_EX_LAYERED) window, so every repaint makes the
#desktop compositor redo the whole screen - it is the most expensive kind of window there is to
#redraw, and the cost lands on the GAME underneath, not on us. The caller redraws once per
#detection frame, so making detection faster silently made this repaint more often: measured, box
#repaints went from 34/s to 51/s purely as a side effect of an FPS optimisation, with the compositor
#work rising in step. That shows up as cursor flicker and as the game feeling a touch less crisp.
#
#Nothing is gained by repainting faster. These are DISPLAY rates: nothing reads the screen to make
#a decision (auto-collect reads shared_text_tracks directly), so a box arriving up to 33ms later
#changes only how smooth it looks. The panel gets a much lower cap because it is a numeric read-out
#nobody can read at 30Hz anyway, and because it is by far the more expensive of the two layers:
#measured 1.17ms per panel repaint against 0.35ms for the boxes, since every label is drawn nine
#times over to ring it in black (see _panel_text).
#
#Measured over the worst case (boxes moving AND a health value ticking), against a ~51Hz caller:
#  repaint work   73.3 ms/s -> 34.3 ms/s
#  box repaints   47.6/s    -> 24.6/s
#  panel repaints 32.0/s    -> 10.0/s
#
#BE HONEST ABOUT THE ONE TRADE: because the caller runs at ~51Hz, a 30Hz cap cannot land on 30 -
#it takes every second call, so boxes actually update ~25/s, slightly LESS often than the ~34/s
#they managed before the pipeline got faster. A rate cap can only ever land on caller_rate/N. At
#25Hz a moving box is still perfectly fluid, and it buys back more than half the compositor work,
#which is the thing that was actually hurting. If boxes ever look choppy this is the knob - but
#note that raising it to anything above caller_rate/2 jumps straight back to every frame.
#
#Both are pure smoothness knobs. Neither can affect detection, tracking, or auto-collect.
BOX_REDRAW_HZ = 30
PANEL_REDRAW_HZ = 10

#How far a box must move before it is worth repainting for. Detection runs at CAPTURE_SCALE (0.3),
#so coordinates are scaled back up by 3.33x on the way here - one pixel of matchTemplate jitter on
#a completely STATIONARY item becomes a 3-4px jump on screen, which is enough to fail an equality
#check and force a full repaint of a fullscreen layered window, every frame, forever. That is both
#wasted compositor work and a visibly restless box.
#Compared against the last DRAWN position rather than the last one offered, so slow genuine drift
#accumulates until it crosses the threshold instead of being ignored forever. Verified: a box
#creeping 2px per frame stays within 4px of truth indefinitely rather than falling steadily behind.
#
#THE TRADE, stated because the test caught it: the box does NOT snap to the exact position when
#movement stops - it settles wherever it last drew, up to this many pixels out, and stays there.
#That is a cosmetic offset of a few pixels on a box a couple of hundred pixels wide, and it is
#display-only: auto-collect clicks coordinates from shared_text_tracks, never from what is drawn,
#so nothing aims at this box. If it ever needs to settle exactly, the fix is a one-shot exact
#redraw after the content has been unchanged for a beat, not a smaller deadband - jitter at this
#capture scale is 3.3px by construction (1px at CAPTURE_SCALE 0.3, scaled back up), so anything
#below 4 stops suppressing it at all.
BOX_MOVE_DEADBAND_PX = 4


def _boxes_look_the_same(new, old):
    """True if every box is within BOX_MOVE_DEADBAND_PX of the one already drawn - same count,
    same colours, same order. Anything else (a box appearing, disappearing, or changing colour)
    is a real change and must be drawn."""
    if old is None or len(new) != len(old):
        return False
    for (x1, y1, w1, h1, c1), (x2, y2, w2, h2, c2) in zip(new, old):
        if c1 != c2:
            return False
        if max(abs(x1 - x2), abs(y1 - y2), abs(w1 - w2), abs(h1 - h2)) > BOX_MOVE_DEADBAND_PX:
            return False
    return True


def _hex(color):
    return "#%02x%02x%02x" % color


class Overlay:
    def __init__(self, width, height):
        if platform.system() != "Windows":
            raise NotImplementedError(
                "The transparent overlay is Windows-only right now (see overlay.py). "
                "Run without --overlay to use the plain debug window instead."
            )
        import win32api
        import win32con
        import win32gui

        self.root = tk.Tk()
        self.root.overrideredirect(True) #no title bar/border
        self.root.attributes("-topmost", True) #always drawn above the game
        self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.root.config(bg=TRANSPARENT_COLOR)
        self.root.geometry(f"{width}x{height}+0+0")

        self.canvas = tk.Canvas(self.root, width=width, height=height,
                                 bg=TRANSPARENT_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._height = height #panel position is a fraction of this - see draw_panel()
        self._last_rectangles = None #see draw_rectangles()
        self._last_panel = None      #see draw_panel()
        #Timestamps of the last ACTUAL repaint of each layer, for the rate caps above. Separate
        #per layer, since the two change on completely different cadences and have very
        #different costs.
        self._last_box_draw = 0.0
        self._last_panel_draw = 0.0
        self._fonts = {}  #font spec -> tkfont.Font, for measuring text (see _text_width)

        #WS_EX_TRANSPARENT makes the ENTIRE window click-through, not just the
        #transparent-colored background - guarantees clicks reach the game even
        #if they land exactly on a drawn box outline.
        self.root.update_idletasks() #forces the native window handle to actually exist
        #winfo_id() can return an inner Tk drawing window rather than the actual outer
        #OS-level frame that governs hit-testing/click-routing - walk up to that frame
        #if there is one, since styling the wrong (child) window silently does nothing.
        raw_hwnd = self.root.winfo_id()
        hwnd = win32gui.GetParent(raw_hwnd) or raw_hwnd

        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
        #SetWindowLong alone doesn't reliably make Windows re-evaluate hit-testing for the
        #new style - force it to by nudging the window via SetWindowPos with SWP_FRAMECHANGED.
        win32gui.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                               win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER |
                               win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED)

        #Changing GWL_EXSTYLE above resets the layered window's transparency data that
        #Tk's "-transparentcolor" had just set up, since Tk has no idea we touched its
        #window - without this, the window renders as solid black instead of see-through.
        r, g, b = int(TRANSPARENT_COLOR[1:3], 16), int(TRANSPARENT_COLOR[3:5], 16), int(TRANSPARENT_COLOR[5:7], 16)
        win32gui.SetLayeredWindowAttributes(hwnd, win32api.RGB(r, g, b), 0, win32con.LWA_COLORKEY)

    def resize(self, width, height):
        self.root.geometry(f"{width}x{height}+0+0")
        self.canvas.config(width=width, height=height)
        self._height = height
        self._last_panel = None #the panel is positioned relative to the height, so force a redraw

    def draw_rectangles(self, rectangles):
        """rectangles: [(x, y, w, h, color), ...] where color is an (r, g, b) tuple."""
        #This gets called every frame (~40-50x/sec) regardless of whether there's anything to
        #draw or whether it changed - redrawing this always-on-top layered window that often
        #for no visible change forces Windows to recomposite it constantly, which is a plausible
        #source of cursor flicker. Skipping the redraw when nothing changed (the common case:
        #no items on screen, or an item sitting still) avoids that churn for free.
        #Not just equality: a box that has only jittered by a pixel or two is not worth a
        #fullscreen repaint. See BOX_MOVE_DEADBAND_PX.
        if _boxes_look_the_same(rectangles, self._last_rectangles):
            return
        #Content changed, but cap how often that actually reaches the screen (see BOX_REDRAW_HZ).
        #Note what is NOT done here: _last_rectangles is deliberately left alone when a redraw is
        #skipped, so the next call still sees a difference and draws the newer content. Recording
        #it as drawn would strand the boxes at a stale position whenever an item stopped moving
        #right after a skipped frame.
        now = time.monotonic()
        if now - self._last_box_draw < 1.0 / BOX_REDRAW_HZ:
            return
        self._last_box_draw = now
        self._last_rectangles = rectangles
        #delete("boxes"), not delete("all") - the debug panel shares this canvas and updates on a
        #completely different cadence, so the two must be able to redraw without erasing each other.
        self.canvas.delete("boxes")
        for (x, y, w, h, color) in rectangles:
            self.canvas.create_rectangle(x, y, x + w, y + h, outline=_hex(color), width=2, tags="boxes")

    def _text_width(self, text, font):
        """Rendered width of `text` in `font`, in pixels.

        Tk font objects are cached per font spec: constructing one asks the font system for
        metrics, which is not something to redo for every label on every repaint of the panel -
        this is already the overlay's most expensive layer.
        """
        if not text:
            return 0
        measurer = self._fonts.get(font)
        if measurer is None:
            try:
                measurer = tkfont.Font(root=self.root, font=font)
            except tk.TclError:
                #Never let a font lookup take the overlay down; fall back to a monospace estimate.
                return int(len(text) * font[1] * 0.62)
            self._fonts[font] = measurer
        return measurer.measure(text)

    def _panel_text(self, x, y, text, color, font):
        """One line of panel text, ringed in black before it is drawn.

        The ring was not decoration when the key colour was magenta - see
        PANEL_TEXT_OUTLINE_OFFSETS for why it earned its place then, why it no longer does now
        that the key is near-black, and what to restore if the key colour ever changes back."""
        for dx, dy in PANEL_TEXT_OUTLINE_OFFSETS:
            self.canvas.create_text(x + dx, y + dy, text=text, fill="#000000", font=font,
                                     anchor="nw", tags="panel")
        self.canvas.create_text(x, y, text=text, fill=_hex(color), font=font, anchor="nw", tags="panel")

    def draw_panel(self, title, rows, origin=None):
        """Optional read-out panel on the left of the screen, independent of the detection boxes.

        title: heading string, or None to hide the panel entirely.
        rows:  [(label, value_text, fraction_or_None, (r, g, b)), ...]. `fraction` (0.0-1.0) draws
               a bar; None draws no bar, for a row that has no reading to show.

        origin: (x, y) top-left in screen pixels, or None for the default left-of-screen spot.
               A caller that knows where the watched window is should pass its corner, so the
               panel travels with the window instead of sitting on bare desktop beside it.

        Deliberately generic - it knows nothing about health, mana, or games, only about labels,
        bars and numbers, so the same panel can display anything a caller wants to watch."""
        if (title, rows, origin) == self._last_panel:
            return
        #Same rate cap and the same skip-without-recording as draw_rectangles, at a lower rate -
        #this is a numeric read-out, and it is the expensive layer. See PANEL_REDRAW_HZ.
        now = time.monotonic()
        if now - self._last_panel_draw < 1.0 / PANEL_REDRAW_HZ:
            return
        self._last_panel_draw = now
        self._last_panel = (title, rows, origin)
        self.canvas.delete("panel")
        if title is None:
            return

        if origin is None:
            x, top = PANEL_MARGIN_X, int(self._height * PANEL_TOP_FRACTION)
        else:
            x, top = int(origin[0]), int(origin[1])
        #Backing plate first, so everything else lands on top of it (Tk canvas draws in
        #insertion order). Sized from the row count AND from the measured width of what is about
        #to be drawn, so text can never spill outside the plate onto the bare game.
        #
        #It used to be a fixed PANEL_WIDTH, which was fine while every value was a percentage
        #("45%") but broke the moment a row carried a status message instead: the value column
        #starts at PANEL_VALUE_OFFSET (232px), so anything past about seven characters hung off
        #the right-hand edge. "not on screen (0.30)" did exactly that, in red, over the game.
        #Measuring is the fix rather than a bigger constant, because the next long string would
        #simply overflow the bigger constant too.
        height = PANEL_TITLE_GAP + PANEL_ROW_HEIGHT * len(rows or [])
        width = max(PANEL_WIDTH, self._text_width(title, PANEL_TITLE_FONT))
        for (label, value_text, _fraction, _color) in rows or []:
            width = max(width,
                        self._text_width(label, PANEL_FONT),
                        PANEL_VALUE_OFFSET + self._text_width(value_text, PANEL_FONT))
        self.canvas.create_rectangle(x - PANEL_PAD, top - PANEL_PAD,
                                      x + width + PANEL_PAD, top + height + PANEL_PAD,
                                      fill=_hex(PANEL_BG_COLOR), outline=_hex(PANEL_BORDER_COLOR),
                                      width=1, stipple=PANEL_BG_STIPPLE or "", tags="panel")

        y = top
        self._panel_text(x, y, title, PANEL_TITLE_COLOR, PANEL_TITLE_FONT)
        y += PANEL_TITLE_GAP

        for (label, value_text, fraction, color) in rows or []:
            self._panel_text(x, y, label, PANEL_LABEL_COLOR, PANEL_FONT)
            if fraction is not None:
                bar_x, bar_y = x + PANEL_BAR_OFFSET, y + 3
                self.canvas.create_rectangle(bar_x, bar_y, bar_x + PANEL_BAR_WIDTH, bar_y + PANEL_BAR_HEIGHT,
                                              outline=_hex(PANEL_BAR_TRACK_COLOR), width=1, tags="panel")
                filled = int(PANEL_BAR_WIDTH * max(0.0, min(1.0, fraction)))
                if filled > 0:
                    self.canvas.create_rectangle(bar_x, bar_y, bar_x + filled, bar_y + PANEL_BAR_HEIGHT,
                                                  fill=_hex(color), outline="", tags="panel")
            self._panel_text(x + PANEL_VALUE_OFFSET, y, value_text, color, PANEL_FONT)
            y += PANEL_ROW_HEIGHT

    def pump(self):
        """Processes pending Tk events. Call this regularly from the main thread -
        Tk's event loop must run on whichever thread created the window."""
        self.root.update_idletasks()
        self.root.update()

    def is_open(self):
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
