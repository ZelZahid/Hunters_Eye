# Test fixtures

Real frames the tests measure against, committed so the tests always have them.

**Do not point a test at `assets/zelScreenshots/`.** That folder is the project owner's scratch
space for sharing screenshots during a session; it is gitignored and gets cleared whenever it
suits them. A test that reads from it does not fail when the file disappears - it *skips*, which
looks exactly like passing. That happened twice: `test_presence.py`'s whole real-screenshot
section quietly stopped running, and `test_quit_game.py`'s button tests skipped for long enough that a refactor broke them unnoticed - the multi-reference change moved an attribute they used, and nothing caught it until `esc_menu.png` was added here and the tests ran again.

If a new test needs a real frame, add it here.

## What each one is

| file | what it is | what it is for |
|---|---|---|
| `lobby.png` | the Create Game lobby, game name `z25pin38` | form location, row spacing, reading + incrementing the name, and the in-play check correctly saying "not in a game" |
| `lobby_name_clash.png` | the same lobby with "A Game Already Exists With That Name" | detecting the clash dialog, and not detecting it when it is absent |
| `in_game_tooltip.png` | in game, an item tooltip covering the right-hand orb | the occlusion bug: one in-play reference covered, the other still visible |
| `in_game_dim.png` | in game, Catacombs Level 2 - an unlit room, both orbs clear | the in-play check must not track how well-lit the scene is (`Error_history.txt` #44) |
| `lobby_form_crop.png` | a close crop of the Create Game form | reading a game name out of the text box |
| `lobby_zze9.png` | the lobby with game name `zze9` | the per-character name vote - no single OCR threshold reads this name correctly (`Error_history.txt` #43) |
| `lobby_zelgt0.png` | the lobby with game name `zelgt0`, the owner's crop of the form pasted into a full-size black frame at the position the form occupies at 1920x1080 | the name whose last character NO threshold can read - '0' and 'O' are one shape, so it is resolved from the name the program itself typed (`Error_history.txt` #47) |
| `pindle_pack.png` | Nihlathak's Temple entrance, ~8-9 Defiled Warriors | the monster-detection experiments - see `docs/monster_detection_plan.txt` section 9; also `test_route.py`'s "not Harrogath" case |
| `route_harrogath_01/05/06/09/10.png` | Harrogath, the waypoint->portal walk, **stored at 0.25x** | `test_route.py`: locating frames the route map was *not* built from, against ground truth from the registration |
| `portal_label.png` / `portal_no_label.png` | full-resolution crops round Nihlathak's portal, hovered (label showing) and not | `test_route.py`: the portal is only clicked once its label is read |
| `route_temple_19/20/21/24/28.png` | Nihlathak's Temple, arrival -> fighting spot, **stored at 0.25x** like the Harrogath ones | `test_route.py`: 21/24/28 are held out of the temple map; 19 is the arrival (found by the portal landmark); 20 is the frame that once matched the *Harrogath* map at 0.630 |
| `plate_pindleskin.png` / `plate_defiled_warrior.png` / `plate_none.png` | full-size frames masked to the top strip, with Pindleskin, a Defiled Warrior, and nothing hovered | `test_pindle_fight.py`: the red monster plate is the "a monster is under the cursor" signal |
| `fight_motion_27.png` / `fight_motion_28.png` | two frames from the fighting spot with the same camera, **stored at 0.5x** | `test_pindle_fight.py`: what moves between them points the cursor at the pack |

The `route_harrogath_*` frames are the other exception, in the opposite direction: they are
**downscaled rather than masked**, to exactly the map's scale (`map_scale` in
`assets/routes/harrogath_to_nihlathak.json`). That is the array the live path produces after its
own resize, so the test skips the resize and measures bit-for-bit what runs live - while each file
is ~240 KB instead of ~3 MB. If `map_scale` ever changes, these have to be regenerated from the
original screenshots at the new scale, or the test will be measuring a different image from the
one the game produces.

`pindle_pack.png` is the one fixture kept **unmasked and full-size**, deliberately. Everywhere else the
subject is a small HUD region and the rest is noise worth blacking out; here the monsters scattered
across the frame *are* the subject, and the viewport-crop result in the plan depends on this exact
geometry. Masking it would delete the thing it exists to show.

## Why they are mostly black

Everything outside the regions the tests actually search is masked out, at the **same frame
dimensions** - so every fraction, offset and coordinate is unchanged while the files compress to
roughly half. Rebuilt and verified equivalent to the originals: identical in-play scores
(0.815 / 0.272 / 0.268), identical row spacing, click points, name read and dialog detection, and identical
Save-and-Exit scores (0.889 right button vs 0.529 wrong).

One thing that took a second pass, and is the rule for adding any fixture: **preserve the numbers
a test asserts on, not just the answers.** The first attempt masked the orb corners out of the
lobby frames. Every True/False still matched - but the in-play score fell from 0.272 to 0.038,
so the test that checks the *margin* between in-play and lobby would have been measuring black
pixels and would have passed however far the real margin eroded.
