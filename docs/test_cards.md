# Test cards

Every test in `tests/` is a numbered **card**: one thing that must stay true, with an ID like
`V-104`. This file lists every card with a one-line title. It cannot go stale: card **T-301**
(`tests/test_card_index.py`) fails whenever this list and the tests disagree, and prints the exact
row to paste.

## Running them

| what | command |
|---|---|
| everything, with a summary of failing cards | `python tests/run_all.py` (add `-v` for full output) |
| one file | `python tests/test_slots.py` |

**When they run on their own:** on every pull request and every push to `main`, GitHub Actions
runs `python tests/run_all.py` on a fresh Windows machine with Tesseract installed
(`.github/workflows/tests.yml`). A pull request is merged only once that run is green. Locally,
run them whenever you like; nothing forces it on a commit.

**What CI cannot run:**
- **V-707** needs the trained monster model, which is gitignored (`assets/monsters/`). Without it
  the card prints `NOT RUN` and passes; the run's summary lists it under "Not run here", so the
  gap is visible every time. Run it on your own machine after training a model.
- Nothing needs the game. What does - and how little of it still needs a person - is in
  [`testing.md`](testing.md): `tools/live_check.py` checks the live reading side with the game
  open and prints PASS/FAIL, and a short list covers what only a person can judge.

**Skips are failures under CI.** V-801 (a real screen capture), V-903/V-904 (real OCR), L-6xx (the
pipeline on a real screen), T-105/T-106 (the overlay window) and V-806/V-807 (window lookup) skip
politely on a machine without a screen, Tesseract or Windows, but fail when the `CI` environment
variable is set, because a check that quietly skips on the machine meant to run it looks exactly
like a pass. A committed fixture that is missing is always a failure, never a skip.

## Card IDs

`V-104` = category **V**, area **1**, card **04**.

- The **letter** is the category: **V**ision, **L**ogic, **A**ctions, **C**onfig, **T**ools.
- The **first digit** is the area inside it (V-1xx meters, V-4xx OCR, ...). Each area owns a
  block of 100, so a new card never forces anything else to be renumbered.
- **IDs are never reused or renumbered.** A deleted card's ID is retired.
- **Adding a card:** take the next free number in its area, print the header from the test as
  `print("\nV-123  What it checks")` (two spaces after the ID), and add its row here in the same
  change. A new area takes the next free hundred in its category.

The **Was** column is the card's old per-file section number. Older notes (`Error_history.txt`,
`updates.txt`) say things like "test_targets.py 12"; look that up here as `targets §12`.

## Known gaps

Critical code with no card yet, so nobody assumes it is covered:
- **The two pipeline threads that act** (auto-collect, potions) wired in with the rest. Their
  decisions are carded (L-1xx, L-2xx) and capture -> detect -> OCR runs end to end (L-6xx), but
  nothing starts the acting threads against a screen, because on a test machine they would act.
- **Anything that needs the real game.** What is left, and the script that checks most of it
  without you watching, is in [`testing.md`](testing.md).

## V — Vision

_Turning pixels into an answer._


### V-1xx  Meters (health / mana)

Code under test: core/game_state.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-101 | Accuracy across fill levels (full resolution) | test_game_state.py | game_state §1 |
| V-102 | Same, but downscaled to main.py's fast-path scale (0.3) | test_game_state.py | game_state §2 |
| V-103 | Robustness: sensor noise + a gloss highlight streak over the liquid | test_game_state.py | game_state §3 |
| V-104 | The reason for row-scanning: area fraction != height fraction on a circle | test_game_state.py | game_state §4 |
| V-105 | Wrong color does not read as full | test_game_state.py | game_state §5 |
| V-106 | A horizontal bar filling from the left | test_game_state.py | game_state §6 |
| V-107 | Off-screen / degenerate region returns None, not 0.0 | test_game_state.py | game_state §7 |
| V-108 | Smoother rejects a single bad frame | test_game_state.py | game_state §8 |
| V-109 | A stray patch of the meter's own color ABOVE the liquid does not inflate the reading | test_game_state.py | game_state §9 |
| V-110 | A gloss streak ABOVE the liquid does not read as liquid | test_game_state.py | game_state §10 |
| V-111 | A failed read carries the last good value, but NOT indefinitely | test_game_state.py | game_state §11 |
| V-112 | An UNREADABLE meter reports None, never 0% - the lobby / dimmed-menu bug | test_game_state.py | game_state §12 |
| V-113 | A single row of stray colour is not a reading - the lobby 2% bug | test_game_state.py | game_state §13 |
| V-114 | A DARK liquid is still liquid - the value floor must not exclude it | test_game_state.py | game_state §98 |

### V-2xx  Belt slots (what is in each cell)

Code under test: core/slots.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-201 | The owner's real belt, read off a full frame | test_slots.py | slots §1 |
| V-202 | An empty slot reads as empty, not as something | test_slots.py | slots §2 |
| V-203 | Nothing to read is a different answer from nothing there | test_slots.py | slots §3 |
| V-204 | Synthetic colours land in the right bucket | test_slots.py | slots §4 |

### V-3xx  Screen-art presence (in play, menus, buttons)

Code under test: core/presence.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-301 | The shipped in-play config loads | test_presence.py | presence §1 |
| V-302 | A missing or broken config is 'no check available', never a crash | test_presence.py | presence §2 |
| V-303 | An unanswerable question returns None, which is NOT False | test_presence.py | presence §3 |
| V-304 | A blank frame is a confident NO, not an error | test_presence.py | presence §4 |
| V-305 | The template is rescaled to the HUD's size, not assumed | test_presence.py | presence §5 |
| V-306 | Real screenshots: in-play and lobby separate by a wide margin | test_presence.py | presence §6 |
| V-307 | Occlusion: one covered reference must not read as 'not in a game' | test_presence.py | presence §8 |
| V-308 | The negative is debounced, the positive is not | test_presence.py | presence §9 |
| V-309 | The button config loads and is shaped as expected | test_quit_game.py | quit_game §1 |
| V-310 | Nothing on screen -> no position, and never a crash | test_quit_game.py | quit_game §2 |
| V-311 | On a real Esc menu it finds the RIGHT button | test_quit_game.py | quit_game §3 |
| V-312 | The threshold sits between the right button and the best wrong one | test_quit_game.py | quit_game §4 |
| V-313 | Matching at the fast path's 0.3x would NOT be safe - why it takes its own capture | test_quit_game.py | quit_game §5 |

### V-4xx  Text and OCR

Code under test: core/text_detection.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-401 | '[exact]' refuses to match a name inside a longer label | test_targets.py | targets §8 |
| V-402 | Blue item labels survive preprocessing (the channel, not the threshold) | test_targets.py | targets §9 |
| V-403 | A word that identifies nothing gets one more misread character | test_targets.py | targets §10 |
| V-404 | ...and the look-alike protection still holds with that slack active | test_targets.py | targets §11 |
| V-405 | This program's OWN overlay text must never match a target | test_targets.py | targets §12 |
| V-406 | '[exact]' is not fooled when Tesseract cuts a label into two lines | test_targets.py | targets §13 |
| V-407 | Reading the name off a real lobby field | test_next_game.py | next_game §9 |
| V-408 | Read_line does not disturb the shared OCR engine | test_next_game.py | next_game §10 |
| V-409 | Locating the Create Game form on a real lobby | test_next_game.py | next_game §11 |
| V-410 | Reading the name Diablo II pre-filled, and incrementing it | test_next_game.py | next_game §12 |
| V-411 | Case comes from memory, because OCR cannot supply it | test_next_game.py | next_game §13 |
| V-412 | The name-clash dialog is detected, and only when it is there | test_next_game.py | next_game §14 |
| V-413 | A name no single threshold reads correctly is still resolved, per character | test_next_game.py | next_game §16 |
| V-414 | A name with an unreadable character is resolved from what we already typed | test_next_game.py | next_game §17 |
| V-415 | The Game Name box's arrow marker is not read as part of the name | test_next_game.py | next_game §18 |

### V-5xx  Image search (template matching)

Code under test: main.py `_match_needle`

| Card | What it checks | File | Was |
|---|---|---|---|
| V-501 | No needle on screen: nothing found, same as before | test_image_search.py | image_search §1 |
| V-502 | Needle pasted in: identical boxes to the old version | test_image_search.py | image_search §2 |

### V-6xx  Map localization

Code under test: core/localize.py, routes/shuttle.py (scene_shift)

| Card | What it checks | File | Was |
|---|---|---|---|
| V-601 | The routes load and are shaped as expected | test_route.py | route §1 |
| V-602 | Nothing to go on -> 'cannot tell', never a guess and never a crash | test_route.py | route §2 |
| V-603 | Harrogath screenshots the map was NOT built from are located where they really were | test_route.py | route §3 |
| V-604 | Neither place is ever mistaken for the other | test_route.py | route §4 |
| V-605 | Temple screenshots the map was NOT built from are located where they really were | test_route.py | route §5 |
| V-606 | The temple's arrival point: the red portal places it, independently of the map | test_route.py | route §6 |
| V-607 | Where the route says the portal is, is where the portal is - in a screenshot it never saw | test_route.py | route §7 |
| V-608 | Cost of one live locate (full-size frame, including its resize) - informational | test_route.py | route §11 |
| V-609 | How far the view slid, measured on real frames - and 'cannot tell' between different places | test_shuttle.py | **new** |

### V-7xx  Monster detection

Code under test: core/monster_detection.py, routes/pindle.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-701 | The Detection contract - box + centre, and it unpacks like every other detector | test_monster_detection.py | monster_detection |
| V-702 | Letterbox: aspect preserved, and the padding is recoverable | test_monster_detection.py | monster_detection |
| V-703 | A box decoded from the network lands where it was put | test_monster_detection.py | monster_detection |
| V-704 | Suppression is WITHIN a class - a boss standing in its pack is not deleted | test_monster_detection.py | monster_detection |
| V-705 | Per-class thresholds, and boxes clipped to the frame | test_monster_detection.py | monster_detection |
| V-706 | No model and no onnxruntime are normal states, not crashes | test_monster_detection.py | monster_detection |
| V-707 | The real model (a build artifact - absent is a valid checkout, not a pass) | test_monster_detection.py | monster_detection |
| V-708 | The monster plate: up with a monster under the cursor, and only then | test_pindle_fight.py | pindle_fight §1 |
| V-709 | Movement from a still camera points at the pack | test_pindle_fight.py | pindle_fight §2 |

### V-8xx  Frame capture and window geometry

Code under test: core/frame_source.py, core/window_region.py

| Card | What it checks | File | Was |
|---|---|---|---|
| V-801 | A real screen region comes back as BGRA pixels of exactly the size asked for | test_frame_source.py | **new** |
| V-802 | No DXGI means mss - never an exception | test_frame_source.py | **new** |
| V-803 | A backend that dies mid-session degrades to mss, and the frame still arrives | test_frame_source.py | **new** |
| V-804 | DXGI's 'no new frame' re-serves the last frame - a static screen is not an error | test_frame_source.py | **new** |
| V-805 | A region given as fractions of a WINDOW lands on the right pixels of the capture | test_frame_source.py | **new** |
| V-806 | Finding a real window: its CLIENT area, and an exact title beats a bigger window | test_window_lookup.py | **new** |
| V-807 | Focus is three-valued: True or False for a real window, None when it cannot tell | test_window_lookup.py | **new** |

### V-9xx  The contract every detector shares

Code under test: all detectors

| Card | What it checks | File | Was |
|---|---|---|---|
| V-901 | No detector crashes on a frame it was never meant to see | test_detector_contract.py | **new** |
| V-902 | Image search finds an icon on a document page, not just in a game | test_detector_contract.py | **new** |
| V-903 | OCR finds listed words on a white document page, and says where | test_detector_contract.py | **new** |
| V-904 | OCR reads light text on a dark background - a night-time sign, a dark UI | test_detector_contract.py | **new** |
| V-905 | 'Cannot tell' comes back as None from every detector, never as a confident no | test_detector_contract.py | **new** |

## L — Logic

_Decisions made from what vision saw._


### L-1xx  Potions

Code under test: main.py

| Card | What it checks | File | Was |
|---|---|---|---|
| L-101 | A healthy character drinks nothing | test_potions.py | potions §1 |
| L-102 | An unreadable meter is NOT an emergency | test_potions.py | potions §2 |
| L-103 | A reading below the noise floor is ignored, not treated as critical | test_potions.py | potions §3 |
| L-104 | The right tier fires for the right severity | test_potions.py | potions §4 |
| L-105 | Urgency wins: the first matching rule takes priority | test_potions.py | potions §5 |
| L-106 | Each rule cools down on its OWN clock | test_potions.py | potions §6 |
| L-107 | The global floor stops any burst, whatever the rules say | test_potions.py | potions §7 |
| L-108 | Sustained low health drinks at the cooldown rate, not the poll rate | test_potions.py | potions §8 |
| L-109 | Alternating keys spread across every column a rule lists | test_potions.py | potions §9 |
| L-110 | The keys come from the BELT, not from the config file | test_potions.py | potions §11 |
| L-111 | Rearranging the belt moves the keys, with nothing to edit | test_potions.py | potions §12 |
| L-112 | An empty slot is never pressed | test_potions.py | potions §13 |
| L-113 | With a healing potion in the belt, nothing changes | test_potions.py | potions §14 |
| L-114 | With NO healing potion, rejuvenation stands in - but only from 50% | test_potions.py | potions §15 |
| L-115 | A belt that cannot be read falls back to the configured keys | test_potions.py | potions §16 |
| L-116 | The shipped rules express the owner's policy | test_potions.py | potions §17 |
| L-117 | The in-play gate actually stops the potion layer in a lobby | test_presence.py | presence §7 |
| L-118 | Snoozing potions again ADDS time, up to a cap | test_potions.py | **new** |

### L-2xx  Auto-collect

Code under test: main.py

| Card | What it checks | File | Was |
|---|---|---|---|
| L-201 | Far-away items are worth teleporting onto | test_auto_collect.py | auto_collect §1 |
| L-202 | Close items are just clicked - the owner's 'within a few feet' rule | test_auto_collect.py | auto_collect §2 |
| L-203 | Telekinesis items are never teleported onto | test_auto_collect.py | auto_collect §3 |
| L-204 | The cooldown stops a failed re-find becoming a teleport loop | test_auto_collect.py | auto_collect §4 |
| L-205 | An unknown character position means walking, not guessing | test_auto_collect.py | auto_collect §5 |
| L-206 | The re-find window can actually outlast one OCR cycle | test_auto_collect.py | auto_collect §6 |
| L-207 | After a teleport, a position is only believed once it has MOVED | test_auto_collect.py | auto_collect §7 |
| L-208 | Which item to go for when several are on the ground | test_auto_collect.py | auto_collect §8 |
| L-209 | Every collectable item has a distinct rank | test_auto_collect.py | auto_collect §9 |
| L-210 | A key-collected item retries on its own clock, not the click's | test_auto_collect.py | auto_collect §10 |
| L-211 | An item already on an open UI panel is not chased | test_auto_collect.py | auto_collect §12 |
| L-212 | An action that measurably did nothing is retried early | test_auto_collect.py | auto_collect §13 |
| L-213 | The settle before acting comes from user_config.txt | test_auto_collect.py | auto_collect §14 |

### L-3xx  New game (F3) and quitting

Code under test: main.py

| Card | What it checks | File | Was |
|---|---|---|---|
| L-301 | The number on the end goes up by one | test_next_game.py | next_game §1 |
| L-302 | A name with no number gets a '0' | test_next_game.py | next_game §2 |
| L-303 | Only a number at the very END counts | test_next_game.py | next_game §3 |
| L-304 | Zero padding is kept at its width, and grows when it has to | test_next_game.py | next_game §4 |
| L-305 | Junk in, nothing out - it never invents a name | test_next_game.py | next_game §5 |
| L-306 | A name that does not look like a name is REJECTED, not cleaned up | test_next_game.py | next_game §6 |
| L-307 | Increment and validation compose the way the sequence uses them | test_next_game.py | next_game §7 |
| L-308 | Next_game refuses to start if it cannot leave the current game | test_next_game.py | next_game §15 |
| L-309 | Quit_game refuses to act when the guards say no | test_quit_game.py | quit_game §8 |

### L-4xx  Routes and the fight (F7)

Code under test: routes/pindle.py, routes/shuttle.py

| Card | What it checks | File | Was |
|---|---|---|---|
| L-401 | The walks' decisions, simulated - no game, no mouse | test_route.py | route §8 |
| L-402 | 'F4' stops a walk outright, without waiting for anything | test_route.py | route §9 |
| L-403 | The portal is only clicked once its label is READ | test_route.py | route §10 |
| L-404 | The fight, driven through a fake game | test_pindle_fight.py | pindle_fight §3 |
| L-405 | 'F4' stops the fight outright - it does not resume like the auto-collect snooze | test_pindle_fight.py | pindle_fight §4 |
| L-406 | The bot test mode's aim: out, back to the start, short landings corrected | test_shuttle.py | **new** |
| L-407 | The bot test mode's loop, driven through a fake game | test_shuttle.py | **new** |

### L-5xx  A bot session (start, stop, what it reports)

Code under test: main.py

| Card | What it checks | File | Was |
|---|---|---|---|
| L-501 | Total Runtime reads as minutes and seconds | test_session.py | **new** |

### L-6xx  The pipeline (main.py's threads wired together)

Code under test: main.py `get_screenshot`, `detect_objects`, `detect_text`

| Card | What it checks | File | Was |
|---|---|---|---|
| L-601 | Capture -> detect -> OCR, wired together on a real screen | test_pipeline.py | **new** |
| L-602 | The pipeline paces itself, and every stage is still alive after running | test_pipeline.py | **new** |
| L-603 | Something taken off screen stops being reported | test_pipeline.py | **new** |

## A — Actions

_Mouse and keyboard._


### A-1xx  Click, press, wait

Code under test: core/actions.py

| Card | What it checks | File | Was |
|---|---|---|---|
| A-101 | Click_when_seen waits for the target, clicks once, and gives up cleanly | test_quit_game.py | quit_game §6 |
| A-102 | Wait_until sequences on a condition, not a clock | test_quit_game.py | quit_game §7 |
| A-103 | What is done to a target is separate from how long to keep doing it | test_quit_game.py | quit_game §9 |

## C — Config

_The files a person edits._


### C-1xx  user_config.txt

Code under test: core/user_config.py

| Card | What it checks | File | Was |
|---|---|---|---|
| C-101 | A well-formed file parses as written | test_user_config.py | user_config §1 |
| C-102 | File ORDER is rule priority - the emergency tier must stay first | test_user_config.py | user_config §2 |
| C-103 | '20%' and '0.20' mean the same thing | test_user_config.py | user_config §3 |
| C-104 | Keys are configurable, including reassigning the belt | test_user_config.py | user_config §4 |
| C-105 | A missing file is a normal condition, not an error | test_user_config.py | user_config §5 |
| C-106 | Bad values fall back to a default and never crash | test_user_config.py | user_config §6 |
| C-107 | A rule naming a meter that does not exist is DROPPED, not repointed | test_user_config.py | user_config §7 |
| C-108 | Incomplete rules are dropped, the rest of the file still works | test_user_config.py | user_config §8 |
| C-109 | A file with no usable rule at all falls back rather than doing nothing | test_user_config.py | user_config §9 |
| C-110 | Garbage that is not even INI shape does not crash the program | test_user_config.py | user_config §10 |
| C-111 | 'enabled = no' turns it off without deleting the rules | test_user_config.py | user_config §11 |
| C-112 | Teleporting onto a drop is off unless the file asks for it | test_user_config.py | user_config §12 |
| C-113 | The key-collect retry is its own setting, not the click's | test_user_config.py | user_config §13 |
| C-114 | Image_search defaults ON, so a missing setting changes nothing | test_user_config.py | user_config §14 |
| C-115 | The shipped user_config.txt is usable | test_auto_collect.py | auto_collect §11 |
| C-116 | The SHIPPED user_config.txt is sane (not: has particular values) | test_potions.py | potions §10 |
| C-117 | The password / game-name settings load from user_config.txt | test_next_game.py | next_game §8 |
| C-118 | The shipped user_config.txt says whether the search runs | test_image_search.py | image_search §3 |
| C-119 | Editing user_config.txt keeps everything it was not asked to change | test_control_panel.py | **new** |

### C-2xx  assets/targets.txt

Code under test: core/text_detection.py

| Card | What it checks | File | Was |
|---|---|---|---|
| C-201 | The line syntax parses as documented | test_targets.py | targets §1 |
| C-202 | A duplicate name WARNS instead of silently overwriting | test_targets.py | targets §2 |
| C-203 | An unrecognised tag warns and falls back, it does not crash | test_targets.py | targets §3 |
| C-204 | The shipped targets.txt has no duplicates and reports the small Rejuvenation Potion | test_targets.py | targets §4 |
| C-205 | Both potions still resolve correctly now that neither is a look-alike | test_targets.py | targets §5 |
| C-206 | Look-alikes still do their job for the runes | test_targets.py | targets §6 |
| C-207 | How to collect an item is a tag, like its colour | test_targets.py | targets §7 |

## T — Tools

_The display, the labelling tool and the test suite itself._


### T-1xx  Overlay panel

Code under test: core/overlay.py

| Card | What it checks | File | Was |
|---|---|---|---|
| T-101 | Nothing renders outside the backing plate, in any panel state | test_overlay.py | overlay §1 |
| T-102 | The plate grows only when it has to | test_overlay.py | overlay §2 |
| T-103 | The reported bug specifically | test_overlay.py | overlay §3 |
| T-104 | Measuring is robust to junk input | test_overlay.py | overlay §4 |
| T-105 | The overlay is see-through: what is under it shows wherever it draws nothing | test_overlay_window.py | **new** |
| T-106 | The overlay is click-through: a click on a drawn box lands on the window underneath | test_overlay_window.py | **new** |

### T-2xx  Monster labelling tool

Code under test: tools/label_monsters.py

| Card | What it checks | File | Was |
|---|---|---|---|
| T-201 | Round trip: what was drawn is what trains | test_label_monsters.py | label_monsters |
| T-202 | An empty label and a missing label are opposite intents | test_label_monsters.py | label_monsters |
| T-203 | Class ids never shift | test_label_monsters.py | label_monsters |
| T-204 | A monster half off screen is clipped, not moved | test_label_monsters.py | label_monsters |
| T-205 | Import is by content, so re-running it is a no-op | test_label_monsters.py | label_monsters |
| T-206 | A draft is not a label until a person accepts it | test_label_monsters.py | label_monsters |
| T-207 | Opening a draft is the review, moving on accepts it | test_label_monsters.py | label_monsters |

### T-3xx  The test suite

Code under test: tests/, tools/live_check.py

| Card | What it checks | File | Was |
|---|---|---|---|
| T-301 | The test card index lists exactly the cards the tests run | test_card_index.py | **new** |
| T-302 | The live check passes real screenshots of the states it asks for | test_live_check.py | **new** |
| T-303 | A screen in the wrong state FAILS, and the line says what was expected | test_live_check.py | **new** |
| T-304 | --replay re-judges a saved run from the command line | test_live_check.py | **new** |

### T-4xx  The control panel

Code under test: gui.py, core/command_channel.py

| Card | What it checks | File | Was |
|---|---|---|---|
| T-401 | The command channel: one word per line, unknown words ignored, errors survived | test_control_panel.py | **new** |
| T-402 | Starting, commanding, reading and stopping the bot - against a stand-in | test_control_panel.py | **new** |
| T-403 | The control panel window: settings form, Save, buttons, status | test_control_panel.py | **new** |
| T-404 | Each button shows the hotkey main.py really binds, in F-key order | test_control_panel.py | **new** |
| T-405 | The potion snooze counts down in red on its button; the log is readable | test_control_panel.py | **new** |
