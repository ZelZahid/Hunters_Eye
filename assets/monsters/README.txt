assets/monsters - training images for the monster detector
==========================================================

Drop example images of a monster into the folder named after it. That is the
whole registration process - no code change, no config edit.

See docs/monster_detection_plan.txt for the full design and the reasoning
behind it. This file is the short version.


HOW TO ADD A MONSTER
--------------------
  1. Make a folder here named after it.
  2. Label some frames containing it:  python tools/label_monsters.py
     (the folder you made shows up there as a class you can assign boxes to)
  3. Re-run:  python tools/train_monster.py

Step 2 is the real work, and "put images in the folder" is NOT it - see below.

THE FOLDER NAME IS THE CLASS NAME.
  - lowercase_with_underscores, no spaces:  defiled_warrior, not "Defiled
    Warrior". The name is what gets reported and drawn on the overlay.
  - An empty folder is skipped by the trainer, not an error. The folders here
    can sit empty for as long as you like.
  - Deleting a folder removes that class on the next training run.


ONE MODEL, MANY MONSTERS
------------------------
All of these folders train into a SINGLE model (monsters.onnx) that knows every
class at once - not one model per monster. That is what keeps the frame cost
flat as the list grows: the network runs once per frame whether it knows 3
monsters or 300. Adding a monster costs training images, paid once, offline -
it does not cost FPS.

Section 3 of docs/monster_detection_plan.txt explains why in detail.


WHAT TO PUT IN THEM - AND WHAT ACTUALLY TRAINS
----------------------------------------------
THESE FOLDERS DO NOT TRAIN ANYTHING. Decided 2026-09-11; section 7 of the plan
has the reasoning. They are the registration surface and a human-readable record
of what each class name means - crops you can flip through to check that
'defiled_warrior' means what you think it means.

WHAT TRAINS is a whole gameplay frame plus the coordinates of a box drawn on it,
in _dataset/. You produce those with:

    python tools/label_monsters.py --import assets/zelScreenshots   # frames in
    python tools/label_monsters.py                                  # draw boxes
    python tools/label_monsters.py --export-crops   # also fill these folders

A crop cannot train a detector, because a crop throws away the very things the
model needs: the rest of the frame is the NEGATIVE set (the torch, the statue,
the mercenary, the player - all of them being not-a-monster), and the frame is
also what tells the model how BIG a monster looks on screen.


HOW MANY IMAGES - COUNT BOXES, NOT SCREENSHOTS
----------------------------------------------
One frame of a Pindle pack gives 8-9 Defiled Warriors; one frame of Mephisto
gives one. So the unit that matters is boxes per class:

    ~50 boxes       enough to prove the loop works, not to trust it
    ~300-500        the practical target per class
    ~1000+          diminishing returns, unless the monster has many variants

VARIETY BEATS COUNT. Different animation poses, facings, zoom levels, lighting
and backgrounds are worth far more than fifty near-identical screenshots.

AND LABEL EVERY MONSTER IN A FRAME, not only the one you came for. An unlabelled
monster teaches the model that it is scenery. If a look-alike keeps appearing
(Pindleskin standing among Defiled Warriors), give it its own folder and box it -
that is how the model learns to tell them apart.

Frames with NO monsters are worth collecting too: torches, statues, empty
corridors, saved with the 'e' key as empty labels. Those are what stop the
detector shooting at scenery. About one in five frames is a good ratio.


WHAT IS NOT COMMITTED
---------------------
The folders are tracked (via .gitkeep) but the images in them are NOT, and
neither are the built model files (monsters.onnx / monsters.json). Same
reasoning as assets/zelScreenshots/: they are large binaries that only grow, and
the model is reproducible from the images anyway. See .gitignore.


BUILT ARTIFACTS THAT WILL APPEAR HERE
-------------------------------------
  monsters.onnx    the trained model - written by tools/train_monster.py
  monsters.json    class names + per-class confidence thresholds

Both are generated. Do not hand-edit them; change the folders and retrain.
