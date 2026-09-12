"""Draw the boxes a monster detector trains on - the human half of the labelling loop.

Step 3 of docs/monster_detection_plan.txt. tools/autolabel.py drafts boxes automatically and gets
roughly four in five right; this is where a person fixes the fifth, and where frames that were
never auto-labelled get done by hand. Nothing trains until this has been run.

    python tools/label_monsters.py --import assets/zelScreenshots   # bring frames in, then label
    python tools/label_monsters.py                                  # label what is already in
    python tools/label_monsters.py --only-unlabelled                # skip finished frames
    python tools/label_monsters.py --export-crops                   # also write per-class crops

WHAT YOU DRAW IS THE LABEL, AND A CROP IS NOT A LABEL. This answers the open question left in
section 10 of the plan ("full frames / crops / alpha crops?"). YOLO trains on a WHOLE FRAME plus
box coordinates, not on cut-out pictures of the monster, and the difference is not a file-format
detail - it is most of what the model learns:

  - the frame's OTHER pixels are the negative examples. A torch, a statue, the mercenary and the
    player are in that frame being NOT-a-monster, and a crop throws every one of them away. The
    measured autolabel run put boxes on a torch and on the player, which is exactly the mistake a
    model makes when it has never been shown one labelled as background.
  - scale is information. A monster is ~69x105px in a 1920x1080 frame; a crop is 100% monster at
    whatever size it was saved, so the model loses any sense of how big the thing is on screen.
  - a crop's rectangular edge is itself a feature, and a model will happily learn the edge.

So the per-class folders under assets/monsters/ stay what the README calls them - a REGISTRATION
SURFACE and a visual record of what each name means - and --export-crops fills them as a
by-product of labelling. They are for a human flipping through to check that 'defiled_warrior'
means what they think it means. The trainer reads _dataset/, never them.

LABEL EVERY MONSTER IN THE FRAME, NOT ONLY THE ONE YOU CAME FOR. An unlabelled monster is not
neutral - YOLO reads unlabelled pixels as background, so leaving Pindleskin unboxed in a frame
full of Defiled Warriors actively teaches the model that a skeleton like that is scenery. That is
the same trap train_monster.py warns about for missing label files, one level finer. Give the
look-alike its own class and box it; a multi-class model is explicitly trained to tell two similar
classes apart, which is the one thing separate per-monster models can never learn.

AN EMPTY FRAME IS A REAL LABEL, which is why 'e' is a key of its own. A frame with no monsters in
it, saved as an empty label file, is a hard negative - it is how the model learns that a torch-lit
corridor of statues contains nothing to shoot at. A frame left with NO label file is not the same
thing: the trainer treats it as unlabelled and warns, because the two are indistinguishable to
YOLO and opposite in intent.

CLASS IDS ARE RESOLVED AT SAVE TIME, NOT AT SELECTION TIME. Boxes are held in memory by class
NAME and only turned into the integer classes.txt ids when a file is written, so classes.txt gains
a line the moment a class is first actually USED and never merely because a folder exists.
Selecting a class you then decide against leaves no trace. This matters because classes.txt is
append-only by contract - an id that shifts silently changes the meaning of every label file
already on disk.
"""
import argparse
import hashlib
import shutil
from pathlib import Path

import cv2 as cv

REPO_ROOT = Path(__file__).resolve().parent.parent
MONSTERS_DIR = REPO_ROOT / "assets" / "monsters"
DATASET_DIR = MONSTERS_DIR / "_dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
CLASSES_FILE = DATASET_DIR / "classes.txt"

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")

WINDOW = "Hunter's Eye - label monsters"

# Minimum drag, in FRAME pixels, before a drag counts as a box rather than a stray click. A
# Defiled Warrior is ~69x105px, so anything this small is a slip, not a monster.
MIN_BOX_PX = 6

# Magnifier shown while dragging. Fit-to-screen puts a 1920x1080 frame at ~0.8x on a 1080p
# display, and a box drawn 3px loose at that scale is 4px loose in the label - which is 5% of a
# monster's width. The loupe costs one resize per mouse move and buys that precision back.
LOUPE_SIZE = 180
LOUPE_ZOOM = 3

# Distinct, and none of them near the red of a Diablo II name plate or the green of an item
# label, so a box is never mistaken for something the game itself drew. BGR.
CLASS_COLORS = [
    (80, 220, 80), (80, 180, 255), (255, 180, 80), (200, 120, 255),
    (80, 255, 255), (255, 120, 180), (160, 255, 160), (255, 220, 120),
    (120, 160, 255), (200, 200, 200),
]


def class_color(index):
    return CLASS_COLORS[index % len(CLASS_COLORS)]


def load_classes():
    """The append-only class-id mapping. Index in this list IS the id written to a label file."""
    if not CLASSES_FILE.exists():
        return []
    return [line.strip() for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def registered_folders():
    """Class names offered in the UI: 'make a folder' stays the registration gesture.

    Folders are OFFERED, not committed - see the module docstring on save-time id resolution.
    """
    if not MONSTERS_DIR.exists():
        return []
    return sorted(p.name for p in MONSTERS_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))


def ensure_class_ids(names):
    """Append any newly-used class to classes.txt, preserving every existing id. Returns the map."""
    classes = load_classes()
    added = [n for n in names if n not in classes]
    if added:
        classes.extend(added)
        CLASSES_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLASSES_FILE.write_text("\n".join(classes) + "\n", encoding="utf-8")
        print("classes.txt: added %s" % ", ".join(added))
    return {name: i for i, name in enumerate(classes)}


def label_path(image_path):
    return LABELS_DIR / (image_path.stem + ".txt")


def read_labels(image_path, shape, classes):
    """YOLO file -> [(name, x0, y0, x1, y1)] in frame pixels, or None if never labelled."""
    path = label_path(image_path)
    if not path.exists():
        return None                    # distinct from [] - "never labelled" vs "nothing here"
    height, width = shape[:2]
    boxes = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split()
        if len(parts) != 5:
            if line.strip():
                print("  %s line %d: skipped, expected 5 fields" % (path.name, number))
            continue
        try:
            class_id = int(parts[0])
            cx, cy, w, h = (float(v) for v in parts[1:])
        except ValueError:
            print("  %s line %d: skipped, not numeric" % (path.name, number))
            continue
        name = classes[class_id] if 0 <= class_id < len(classes) else "class_%d" % class_id
        boxes.append((name,
                      int(round((cx - w / 2) * width)), int(round((cy - h / 2) * height)),
                      int(round((cx + w / 2) * width)), int(round((cy + h / 2) * height))))
    return boxes


def write_labels(image_path, shape, boxes):
    """Frame-pixel boxes -> YOLO file. Writes an EMPTY file for an empty list, deliberately."""
    height, width = shape[:2]
    ids = ensure_class_ids(sorted({name for name, *_ in boxes}))
    lines = []
    for name, x0, y0, x1, y1 in boxes:
        # Clamp the CORNERS, then derive the centre and size from what is left. A box dragged
        # past the frame edge is a real monster half off screen, and the honest label is the
        # visible part. Clamping the derived cx/cy/w/h instead would keep the full width around a
        # moved centre, i.e. silently shift AND resize the box - out-of-range values are fatal to
        # the trainer, but a plausible wrong box is worse, because nothing rejects it.
        x0, x1 = sorted((min(max(x0, 0), width), min(max(x1, 0), width)))
        y0, y1 = sorted((min(max(y0, 0), height), min(max(y1, 0), height)))
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue                   # entirely off frame; a zero-sized box is trainer-fatal
        lines.append("%d %.6f %.6f %.6f %.6f"
                     % (ids[name], ((x0 + x1) / 2) / width, ((y0 + y1) / 2) / height,
                        (x1 - x0) / width, (y1 - y0) / height))
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    label_path(image_path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def export_crops(image, image_path, boxes):
    """Write each box into assets/monsters/<class>/ as the human-readable record of that name.

    NOT training data - see the module docstring. Named after the frame and the box index, so
    re-exporting the same frame overwrites rather than accumulating near-duplicates.
    """
    written = 0
    height, width = image.shape[:2]
    for index, (name, x0, y0, x1, y1) in enumerate(boxes):
        x0, x1 = sorted((max(0, x0), min(width, x1)))
        y0, y1 = sorted((max(0, y0), min(height, y1)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        folder = MONSTERS_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        cv.imwrite(str(folder / ("%s_%02d.png" % (image_path.stem, index))), image[y0:y1, x0:x1])
        written += 1
    return written


def import_frames(source_dir):
    """Copy images into the dataset, skipping ones already there.

    THE POINT IS TO GET THEM OFF VOLATILE GROUND. assets/zelScreenshots/ is gitignored scratch
    that gets cleared whenever it suits its owner - documented in CLAUDE.md, and it already
    destroyed an experiment's inputs mid-run (monster_detection_plan.txt, section 9). Anything
    being labelled has to live somewhere that is not cleared at will.

    Deduplicated by CONTENT HASH, not by name, so re-importing the same folder twice is a no-op
    and two screenshots that happen to share a filename both survive.
    """
    source = Path(source_dir)
    if not source.is_dir():
        print("Not a directory: %s" % source)
        return 0
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    def digest(path):
        return hashlib.sha1(path.read_bytes()).hexdigest()

    existing = {digest(p) for p in IMAGES_DIR.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES}
    # Continue the frame_NNNN numbering capture_frames.py and autolabel.py both assume.
    used = [int(p.stem[6:]) for p in IMAGES_DIR.glob("frame_*") if p.stem[6:].isdigit()]
    next_number = max(used) + 1 if used else 1

    imported = skipped = 0
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if digest(path) in existing:
            skipped += 1
            continue
        target = IMAGES_DIR / ("frame_%04d%s" % (next_number, path.suffix.lower()))
        shutil.copy2(path, target)
        existing.add(digest(target))
        next_number += 1
        imported += 1
    print("Imported %d frame(s) into %s%s"
          % (imported, IMAGES_DIR, (", skipped %d already present" % skipped) if skipped else ""))
    return imported


class Labeller:
    """The interactive window. Holds one frame's boxes; everything else is read from disk."""

    def __init__(self, frames, classes, export_crops_on_save=False):
        self.frames = frames
        self.index = 0
        self.classes = classes                 # names offered, in selection order
        self.active = 0
        self.export_on_save = export_crops_on_save
        self.image = None
        self.boxes = []
        self.undo_stack = []
        self.dirty = False
        self.never_labelled = True
        self.scale = 1.0
        self.drag_start = None
        self.cursor = (0, 0)
        self.show_help = True
        self.message = ""
        self.quit = False

    # ---- coordinate mapping -------------------------------------------------------------
    def to_frame(self, x, y):
        return int(round(x / self.scale)), int(round(y / self.scale))

    def to_display(self, x, y):
        return int(round(x * self.scale)), int(round(y * self.scale))

    # ---- frame lifecycle ----------------------------------------------------------------
    def load(self):
        path = self.frames[self.index]
        self.image = cv.imread(str(path))
        self.undo_stack = []
        self.dirty = False
        self.message = ""
        if self.image is None:
            self.message = "could not read %s" % path.name
            self.boxes, self.never_labelled = [], True
            return
        existing = read_labels(path, self.image.shape, load_classes())
        self.never_labelled = existing is None
        self.boxes = list(existing or [])

    def save(self):
        if self.image is None:
            return
        path = self.frames[self.index]
        write_labels(path, self.image.shape, self.boxes)
        self.never_labelled = False
        self.dirty = False
        if self.export_on_save and self.boxes:
            export_crops(self.image, path, self.boxes)

    def go(self, delta):
        if self.dirty:
            self.save()
        self.index = (self.index + delta) % len(self.frames)
        self.load()

    def next_unlabelled(self):
        if self.dirty:
            self.save()
        for step in range(1, len(self.frames) + 1):
            candidate = (self.index + step) % len(self.frames)
            if not label_path(self.frames[candidate]).exists():
                self.index = candidate
                self.load()
                return
        self.message = "every frame has a label file"

    def push_undo(self):
        self.undo_stack.append(list(self.boxes))
        del self.undo_stack[:-50]

    # ---- mouse --------------------------------------------------------------------------
    def on_mouse(self, event, x, y, flags, _param):
        self.cursor = (x, y)
        if event == cv.EVENT_LBUTTONDOWN:
            self.drag_start = self.to_frame(x, y)
        elif event == cv.EVENT_LBUTTONUP and self.drag_start is not None:
            x0, y0 = self.drag_start
            x1, y1 = self.to_frame(x, y)
            self.drag_start = None
            if abs(x1 - x0) >= MIN_BOX_PX and abs(y1 - y0) >= MIN_BOX_PX:
                self.push_undo()
                self.boxes.append((self.classes[self.active],
                                   min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
                self.dirty = True
        elif event == cv.EVENT_RBUTTONDOWN:
            self.delete_at(*self.to_frame(x, y))

    def delete_at(self, fx, fy):
        """Delete the SMALLEST box containing the point.

        Overlapping monsters are the norm in a pack, and the smallest box under the cursor is the
        one whose edge you were actually aiming at.
        """
        hits = [(i, (b[3] - b[1]) * (b[4] - b[2])) for i, b in enumerate(self.boxes)
                if b[1] <= fx <= b[3] and b[2] <= fy <= b[4]]
        if not hits:
            return
        index = min(hits, key=lambda hit: hit[1])[0]
        self.push_undo()
        self.boxes.pop(index)
        self.dirty = True

    # ---- drawing ------------------------------------------------------------------------
    def render(self):
        if self.scale != 1.0:
            display = cv.resize(self.image, None, fx=self.scale, fy=self.scale,
                                interpolation=cv.INTER_AREA)
        else:
            display = self.image.copy()

        for name, x0, y0, x1, y1 in self.boxes:
            color = class_color(self.classes.index(name) if name in self.classes else 9)
            p0, p1 = self.to_display(x0, y0), self.to_display(x1, y1)
            cv.rectangle(display, p0, p1, color, 2)
            label_y = max(12, p0[1] - 4)
            cv.putText(display, name, (p0[0], label_y), cv.FONT_HERSHEY_SIMPLEX, 0.45,
                       (0, 0, 0), 3, cv.LINE_AA)
            cv.putText(display, name, (p0[0], label_y), cv.FONT_HERSHEY_SIMPLEX, 0.45,
                       color, 1, cv.LINE_AA)

        if self.drag_start is not None:
            cv.rectangle(display, self.to_display(*self.drag_start), self.cursor,
                         class_color(self.active), 1)
            self.draw_loupe(display)

        self.draw_status(display)
        if self.show_help:
            self.draw_help(display)
        return display

    def draw_loupe(self, display):
        """Magnified view of the pixels under the cursor, in the corner furthest from it."""
        fx, fy = self.to_frame(*self.cursor)
        half = LOUPE_SIZE // (2 * LOUPE_ZOOM)
        height, width = self.image.shape[:2]
        x0, y0 = max(0, fx - half), max(0, fy - half)
        patch = self.image[y0:min(height, y0 + 2 * half), x0:min(width, x0 + 2 * half)]
        if patch.size == 0:
            return
        patch = cv.resize(patch, (LOUPE_SIZE, LOUPE_SIZE), interpolation=cv.INTER_NEAREST)
        centre = LOUPE_SIZE // 2
        cv.line(patch, (centre, 0), (centre, LOUPE_SIZE), (0, 255, 255), 1)
        cv.line(patch, (0, centre), (LOUPE_SIZE, centre), (0, 255, 255), 1)
        dheight, dwidth = display.shape[:2]
        px = dwidth - LOUPE_SIZE - 10 if self.cursor[0] < dwidth // 2 else 10
        py = dheight - LOUPE_SIZE - 10
        display[py:py + LOUPE_SIZE, px:px + LOUPE_SIZE] = patch
        cv.rectangle(display, (px, py), (px + LOUPE_SIZE, py + LOUPE_SIZE), (255, 255, 255), 1)

    def draw_status(self, display):
        done = sum(1 for f in self.frames if label_path(f).exists())
        state = "unsaved" if self.dirty else ("NEW" if self.never_labelled else "saved")
        lines = [
            "frame %d/%d  %s   [%s]   boxes: %d   labelled frames: %d/%d"
            % (self.index + 1, len(self.frames), self.frames[self.index].name, state,
               len(self.boxes), done, len(self.frames)),
            "active class: %s   (%s)" % (self.classes[self.active], self.message or "h for help"),
        ]
        overlay = display.copy()
        cv.rectangle(overlay, (0, 0), (display.shape[1], 46), (0, 0, 0), -1)
        cv.addWeighted(overlay, 0.6, display, 0.4, 0, display)
        for i, text in enumerate(lines):
            color = class_color(self.active) if i == 1 else (235, 235, 235)
            cv.putText(display, text, (10, 18 + i * 20), cv.FONT_HERSHEY_SIMPLEX, 0.5,
                       color, 1, cv.LINE_AA)

    def draw_help(self, display):
        rows = ["drag         draw a box (active class)",
                "right-click  delete the box under the cursor",
                "1-9 / tab    choose class",
                "a / d        previous / next frame (autosaves)",
                "n            next frame with no label file",
                "e            mark EMPTY (no monsters) and save - a real label",
                "u  undo      c  clear frame      s  save now",
                "k            export crops of this frame to the class folders",
                "h  hide this            q / ESC  save and quit",
                "",
                "classes: " + "  ".join("%d:%s" % (i + 1, n)
                                        for i, n in enumerate(self.classes[:9]))]
        width, height = 440, 20 * len(rows) + 16
        x0, y0 = display.shape[1] - width - 10, 56
        overlay = display.copy()
        cv.rectangle(overlay, (x0, y0), (x0 + width, y0 + height), (0, 0, 0), -1)
        cv.addWeighted(overlay, 0.65, display, 0.35, 0, display)
        for i, text in enumerate(rows):
            cv.putText(display, text, (x0 + 10, y0 + 22 + i * 20), cv.FONT_HERSHEY_SIMPLEX,
                       0.42, (225, 225, 225), 1, cv.LINE_AA)

    # ---- keys ---------------------------------------------------------------------------
    def on_key(self, key):
        if key in (ord("q"), 27):
            if self.dirty:
                self.save()
            self.quit = True
        elif key in (ord("d"), 83):
            self.go(1)
        elif key in (ord("a"), 81):
            self.go(-1)
        elif key == ord("n"):
            self.next_unlabelled()
        elif key == ord("\t"):
            self.active = (self.active + 1) % len(self.classes)
        elif ord("1") <= key <= ord("9"):
            wanted = key - ord("1")
            if wanted < len(self.classes):
                self.active = wanted
            else:
                self.message = "no class %d - make a folder in assets/monsters/" % (wanted + 1)
        elif key == ord("u"):
            if self.undo_stack:
                self.boxes = self.undo_stack.pop()
                self.dirty = True
            else:
                self.message = "nothing to undo"
        elif key == ord("c"):
            if self.boxes:
                self.push_undo()
                self.boxes = []
                self.dirty = True
        elif key == ord("e"):
            self.push_undo()
            self.boxes = []
            self.save()
            self.message = "marked empty (hard negative)"
            self.go(1)
        elif key == ord("s"):
            self.save()
            self.message = "saved"
        elif key == ord("k"):
            count = export_crops(self.image, self.frames[self.index], self.boxes)
            self.message = "exported %d crop(s) to assets/monsters/<class>/" % count
        elif key == ord("h"):
            self.show_help = not self.show_help

    def run(self, max_width, max_height):
        self.load()
        if self.image is None:
            print("Could not read the first frame.")
            return
        height, width = self.image.shape[:2]
        self.scale = min(1.0, max_width / width, max_height / height)
        cv.namedWindow(WINDOW, cv.WINDOW_AUTOSIZE)
        cv.setMouseCallback(WINDOW, self.on_mouse)
        while not self.quit:
            if self.image is not None:
                cv.imshow(WINDOW, self.render())
            key = cv.waitKey(20) & 0xFF
            if key != 255:
                self.on_key(key)
            if cv.getWindowProperty(WINDOW, cv.WND_PROP_VISIBLE) < 1:
                if self.dirty:
                    self.save()
                break
        cv.destroyAllWindows()


def screen_size():
    """Best effort, so a 1920x1080 frame is not shown larger than the display showing it."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        size = (root.winfo_screenwidth(), root.winfo_screenheight())
        root.destroy()
        return size
    except Exception:
        return (1600, 900)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--import", dest="import_dir",
                    help="copy images from this folder into the dataset first, then label")
    ap.add_argument("--only-unlabelled", action="store_true",
                    help="show only frames with no label file yet")
    ap.add_argument("--export-crops", action="store_true",
                    help="also write each box into assets/monsters/<class>/ on every save")
    ap.add_argument("--import-only", action="store_true", help="import and exit, do not label")
    args = ap.parse_args()

    if args.import_dir:
        import_frames(args.import_dir)
        if args.import_only:
            return

    frames = sorted(p for p in IMAGES_DIR.glob("*")
                    if p.suffix.lower() in IMAGE_SUFFIXES) if IMAGES_DIR.exists() else []
    if args.only_unlabelled:
        frames = [p for p in frames if not label_path(p).exists()]
    if not frames:
        print("No frames to label in %s." % IMAGES_DIR)
        print("Get some in with:  python tools/label_monsters.py --import assets/zelScreenshots")
        print("or record them live with:  python tools/capture_frames.py")
        return

    classes = load_classes()
    for name in registered_folders():
        if name not in classes:
            classes.append(name)     # offered only; ensure_class_ids() commits it when first used
    if not classes:
        print("No classes. Make a folder per monster under %s and re-run." % MONSTERS_DIR)
        return

    print("%d frame(s), %d already labelled."
          % (len(frames), sum(1 for f in frames if label_path(f).exists())))
    print("Classes: %s" % ", ".join("%d:%s" % (i + 1, n) for i, n in enumerate(classes)))
    width, height = screen_size()
    Labeller(frames, classes, args.export_crops).run(int(width * 0.92), int(height * 0.88))


if __name__ == "__main__":
    main()
