# Who tests what

The goal: **a machine checks everything a machine can, and you only check what needs a person.**
Three layers, from "nobody does anything" to "you, in the game".

| layer | who runs it | when | what you do |
|---|---|---|---|
| 1. Test cards | GitHub, on every pull request | automatically | nothing - a red run blocks the merge |
| 2. Live check | you, with the game open | after a change that touches how the screen is read, after a resolution/UI-scale/game update, or when something feels off | set up five game states and press F9; read PASS/FAIL |
| 3. By hand | you, in the game | only for a change that **acts** in the game, before it merges | watch it do the thing once |

---

## 1. Test cards - automatic

`python tests/run_all.py` runs every card; GitHub runs the same thing on a fresh Windows machine
on every pull request. None of them needs the game. The full list is
[`test_cards.md`](test_cards.md). What they cover, in plain terms:

- **Every detector against real screenshots** of your game (`tests/fixtures/`): meters, the belt,
  in-play, the menus, OCR of item names and the lobby game name, map localization, the monster
  plate.
- **Every decision the bot makes**, with fake inputs: which potion when, which item first, when to
  teleport, when to give up, what F3 and F7 do at each step.
- **The pipeline wired together on a real screen** (L-6xx): a picture and an item name put on the
  screen come back as boxes in the right place, the capture paces itself, and a box goes away when
  its thing does.
- **The overlay window itself** (T-105/T-106): see-through and click-through, measured with real
  pixels and a real click. This used to be a by-hand check after every overlay change.
- **Finding the game window** (V-806/V-807) against real windows.

Not run by GitHub: **V-707**, which needs the trained monster model that only exists on your
machine. The run says so every time ("Not run here"). Run `python tests/run_all.py` yourself after
training a model.

## 2. Live check - a script judges, you set the scene

```
python tools/live_check.py
```

About two minutes. Close `main.py` / the control panel first, and run the game in a window or
borderless window so you can see the console beside it. For each step it tells you what to do in
the game; you do it and press **F9** (the game can stay in front). **F10** skips a step, **End**
stops.

| step | you | it checks |
|---|---|---|
| town | stand in town, nothing open | the game window is found and calibrated, in play, both orbs read full, the belt reads, nothing false-matches (no panel, no menu), actions allowed, and **how long one OCR scan takes on your machine** against the interval between scans |
| inventory | open the inventory | the inventory is recognised (so items in the bag are not chased) |
| stash | open the stash | the stash and inventory are both recognised |
| menu | press Esc | F3's Save and Exit button is found, and the orbs under the dimmed menu read full or "cannot tell" - never a believable low number that would drink a potion |
| away | click the console | the bot knows the game is not in front, and will not act |
| lobby | Save and Exit | the bot knows it is not in a game, finds the Create Game form, and reads the game name |

It **only looks** - it never moves the mouse or presses a key. Every run is saved to
`recordings/live_check_<date>_<time>/` (not committed): a screenshot per step plus `report.txt`.
**Paste `report.txt` into the project thread.** If anything failed, share the folder too: its
screenshots become committed fixtures, and from then on GitHub catches that failure instead of you.
`python tools/live_check.py --replay <folder>` re-judges a saved run with new code, no game needed -
that is how a fix gets checked against your screen without you.

Other scripts that measure so you do not have to watch:

- `tools/teleport_bot.py` - teleports out and back on its own for timing runs (`--measure`).
- `tools/diagnose_ocr.py`, `diagnose_quit.py`, `diagnose_lobby.py`, `diagnose_monsters.py` - when
  one specific thing fails, each prints exactly why.

## 3. By hand - the short list

Only these still need you, and each says why no script can do it:

1. **A change that acts in the game, before it merges** (the project's merge rule). The cards
   prove the decision is right with fake inputs; only the real game proves the keys and clicks land.
   Watch it once:
   - **potions** - take real damage and see the right potion drink (nothing safe can hurt your
     character for you);
   - **auto-collect** - drop or find an item and see it picked up;
   - **F3** - quit and create the next game;
   - **F7** - the Pindle run reaches the pack and fights.
2. **Does the game still feel right?** The pipeline measures its own CPU cost, but "the game feels
   less smooth" or "the boxes flicker annoyingly" is a judgement only the person playing can make.
3. **Policy, not correctness.** Whether 70% is the right health to drink at is your preference; the
   cards check that the number you choose is obeyed.

Everything else - including the overlay's see-through and click-through, which used to be on this
list - is checked by layer 1 or 2.

## When something breaks live

1. Run `python tools/live_check.py`. Most live failures show up as a FAIL line naming the cause.
2. Paste the report into the project thread, and share the recording folder if anything failed.
3. The fix comes with the failing screenshot added as a fixture and a card that would have caught
   it, so the same break cannot come back unnoticed.
