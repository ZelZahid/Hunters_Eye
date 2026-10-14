# Git worktrees — running several agents at once

**What this file is for:** letting more than one agent (or you plus an agent)
work on Hunter's Eye at the same time without fighting over one folder.

## The idea in one paragraph

Normally a git repo is one folder on one branch. If an agent is editing
`core/slots.py` on a potion branch and you want to try something unrelated in
`routes/pindle.py`, you have to wait, or you both edit the same files and tangle
the two changes together. A **worktree** is a second folder, checked out to its
own branch, sharing the *same* `.git` history. Separate files on disk, one
history. Two agents in two worktrees cannot overwrite each other's edits,
and when both are done each branch merges into `main` separately.

## The layout

```
C:\Users\Spark\ws\
  Hunters_Eye\                     <- your main checkout, always on `main`. Never moves.
  Hunters_Eye-worktrees\
    belt-live-watch\               <- branch `belt-live-watch`
    potion-thresholds\             <- branch `potion-thresholds`
```

The main checkout stays where it is so nothing you have open ever breaks.
Worktrees live in the sibling folder, one per task.

## Add a worktree for a new task

From `C:\Users\Spark\ws\Hunters_Eye`:

```
git worktree add ../Hunters_Eye-worktrees/my-task -b my-task
```

That makes the folder and the branch in one step. Branch off the latest `main`
first if your `main` is behind: `git fetch origin main && git pull origin main`.

**Then run the junction command below.** A new worktree is not usable for
monster-detection work until you do.

## The one gotcha: ignored files do not come along

Git only puts **tracked** files into a new worktree. Everything in
`.gitignore` is absent. For this repo that means a fresh worktree has:

- `assets/monsters/` — the class folders and their `.gitkeep`, and nothing else.
  The 79 MB of training images, `monsters.onnx` and `monsters.json` are all
  ignored, so **monster detection silently fails to load** there.
- `yolo11n.pt` — ignored too, but harmless: it re-downloads on demand.
- `__pycache__/`, `venv/` — irrelevant, rebuilt as needed.

Fix it by pointing the worktree's `assets/monsters` at the real one instead of
giving it a copy. In PowerShell, with `my-task` as the worktree name:

```
$w = "C:\Users\Spark\ws\Hunters_Eye-worktrees\my-task\assets\monsters"
Remove-Item -Recurse -Force $w
cmd /c mklink /J "$w" "C:\Users\Spark\ws\Hunters_Eye\assets\monsters"
```

That is a **directory junction** — Windows' version of a shortcut that programs
follow as though it were a real folder. It needs no administrator rights. Two
benefits: the model is always the current one, and those 79 MB exist once on
disk rather than once per worktree (a worktree costs about 23 MB instead of
100 MB+).

Git still reports the worktree as clean afterwards, because the junction's
target holds the same tracked `README.txt` and `.gitkeep` files that git
expects to find there. Verify with `git status` — it should say nothing.

Everything else about paths is already safe: every path in this project
resolves from `__file__` (`main.py:42` for assets, `main.py:1371` for
`user_config.txt`, `routes/pindle.py:58` for routes,
`core/monster_detection.py:54` for the repo root), never from the directory you
happen to run the command in. So code inside a worktree reads *that worktree's*
config and assets, which is exactly what you want.

## Remove a worktree when the branch is merged

From anywhere in the repo:

```
git worktree remove ../Hunters_Eye-worktrees/my-task
git branch -d my-task
```

The first deletes the folder, the second deletes the now-merged branch. If
`remove` refuses because the worktree has uncommitted changes, that is the
safety catch working — look at them first, then add `--force` if you truly
want them gone.

Deleting the folder by hand instead leaves a stale entry behind. `git worktree
prune` cleans that up.

## The two commands you will actually use

```
git worktree list                                              # what exists, and on which branch
git worktree add ../Hunters_Eye-worktrees/my-task -b my-task    # start a new one
```

## Things worth knowing

- **Uncommitted work does not travel.** A new worktree branches off the last
  *commit* on `main`. Anything you have edited but not committed stays only in
  the folder you edited it in.
- **One branch, one worktree.** Git refuses to check the same branch out twice.
  That is the protection, not a limitation.
- **The stash is shared** across every worktree. If two agents both run
  `git stash`, one can pop the other's work. Prefer a throwaway WIP commit to
  set something aside.
- **`user_config.txt` is tracked**, so each worktree gets its own copy at the
  committed version. Tuning numbers in a worktree does not change the ones your
  main checkout uses, and vice versa.
