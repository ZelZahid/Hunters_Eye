assets/monsters - training images for the monster detector
==========================================================

Drop example images of a monster into the folder named after it. That is the
whole registration process - no code change, no config edit.

See docs/monster_detection_plan.txt for the full design and the reasoning
behind it. This file is the short version.


HOW TO ADD A MONSTER
--------------------
  1. Make a folder here named after it.
  2. Put images of it in that folder.
  3. Re-run:  python tools/train_monster.py     (not written yet)

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


WHAT TO PUT IN THEM
-------------------
NOT FIXED YET - deliberately. It may end up being full game frames, tight crops
of the monster, or crops with the background removed, and that gets decided by
the experiments in section 9 of the plan rather than guessed now.

For the moment: put in whatever is convenient, favour VARIETY over quantity.
Different animation poses, facings, zoom levels, lighting and backgrounds are
worth far more than fifty near-identical screenshots. The trainer will be
written to match whatever is actually here.


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
