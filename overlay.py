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
import tkinter as tk

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
PANEL_BAR_HEIGHT = 10
PANEL_FONT = ("Consolas", 11, "bold")   #monospace, so a changing number doesn't shift the text
PANEL_TITLE_FONT = ("Consolas", 11, "bold")
PANEL_TITLE_COLOR = (170, 170, 170)
PANEL_LABEL_COLOR = (220, 220, 220)
PANEL_BAR_TRACK_COLOR = (90, 90, 90)
PANEL_WIDTH = 300
PANEL_PAD = 10
PANEL_BG_COLOR = (16, 16, 20)
PANEL_BORDER_COLOR = (70, 70, 80)
#The window's transparency is a colour KEY - a pixel is either fully invisible or fully opaque,
#so there is no alpha channel to set the plate to 50% with. A stipple gives the same result the
#way this has always been done on colour-keyed surfaces: paint every other pixel in a checker and
#leave the rest as the key colour, which shows the game through. "gray50" is a built-in Tk bitmap
#and costs nothing to draw. Set to None for a fully solid plate (more readable, hides more game).
PANEL_BG_STIPPLE = "gray50"
PANEL_TEXT_OUTLINE = True #ring each glyph in black - required whenever the plate is stippled,
                           #see _panel_text() for why. Pointless (but harmless) on a solid plate.
#WHY THE PANEL HAS A SOLID BACKGROUND, when the rest of the overlay deliberately doesn't:
#the window's transparency is a color KEY, not real alpha - a pixel is either exactly
#TRANSPARENT_COLOR and fully invisible, or fully opaque. Text drawn straight onto that
#background gets antialiased by Windows' subpixel font rendering AGAINST magenta, and on thin
#11px glyphs the magenta fringe dominates the stroke: measured on a real render, the glyph
#cores were the right color but the mean lit pixel came out (204, 140, 201) RGB - visibly pink.
#Drawing a dark plate first gives the text something sane to blend into. Detection boxes don't
#need this because a 2px rectangle outline isn't antialiased the same way, and because covering
#the game where an item is lying would defeat the point of them.


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
        if rectangles == self._last_rectangles:
            return
        self._last_rectangles = rectangles
        #delete("boxes"), not delete("all") - the debug panel shares this canvas and updates on a
        #completely different cadence, so the two must be able to redraw without erasing each other.
        self.canvas.delete("boxes")
        for (x, y, w, h, color) in rectangles:
            self.canvas.create_rectangle(x, y, x + w, y + h, outline=_hex(color), width=2, tags="boxes")

    def _panel_text(self, x, y, text, color, font):
        """One line of panel text, ringed in black before it is drawn.

        The ring is not decoration. With a stippled plate, half the pixels directly behind each
        glyph are the transparent key colour, and Windows' subpixel font smoothing blends the
        glyph's edges into whatever is under them - so the strokes pick up the key's magenta and
        the text renders visibly pink (measured: a grey (220,220,220) label came out averaging
        (208,159,207) RGB). Painting the eight neighbouring offsets in black first fills exactly
        the ring of pixels the antialiasing will use, so the edges blend into black instead."""
        if PANEL_TEXT_OUTLINE:
            for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
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
        self._last_panel = (title, rows, origin)
        self.canvas.delete("panel")
        if title is None:
            return

        if origin is None:
            x, top = PANEL_MARGIN_X, int(self._height * PANEL_TOP_FRACTION)
        else:
            x, top = int(origin[0]), int(origin[1])
        #Backing plate first, so everything else lands on top of it (Tk canvas draws in
        #insertion order). Sized from the row count so it always fits the content exactly.
        height = PANEL_TITLE_GAP + PANEL_ROW_HEIGHT * len(rows or [])
        self.canvas.create_rectangle(x - PANEL_PAD, top - PANEL_PAD,
                                      x + PANEL_WIDTH + PANEL_PAD, top + height + PANEL_PAD,
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
            self._panel_text(x + PANEL_BAR_OFFSET + PANEL_BAR_WIDTH + 12, y, value_text, color, PANEL_FONT)
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
