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

TRANSPARENT_COLOR = "#ff00ff" #arbitrary color never used elsewhere in our drawings, so nothing is accidentally invisible


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
        self._last_rectangles = None #see draw_rectangles()

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
        self.canvas.delete("all")
        for (x, y, w, h, color) in rectangles:
            self.canvas.create_rectangle(x, y, x + w, y + h, outline="#%02x%02x%02x" % color, width=2)

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
