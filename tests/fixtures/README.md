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
| `charm_grand.png` | a blue "Grand Charm" label on the ground, terrain around it, the rest blacked out | blue labels survive preprocessing: luminance maxes at 127 inside the label, the strongest channel at 255 (`Error_history.txt` #48) |
| `charm_small.png` | the same for "Small Charm", in a pile of six overlapping labels | the same, plus evidence that a crowded drop still groups the charm as its own OCR line |
| `charm_grand_of_inertia.png` | a blue "Grand Charm of Inertia" label, masked to the label | `[exact]` must refuse it. Tesseract reads it as two lines, `GRAND CHARM 6F` and `INERTIA`, which is the whole point of the frame (`Error_history.txt` #51) |
| `inventory_open.png` | the inventory panel open, with a Full Rejuvenation Potion on the ground AND the same potion's tooltip inside the panel | the inventory filter: the ground one is collected, the one in the bag is not. Both in one frame is the whole point - either alone proves nothing about telling them apart |
| `stash_open.png` | in town with the stash AND inventory open, a stash item's tooltip showing | the second panel: both regions excluded at once, and the strip of world between them still collectable |
| `belt_full.png` | the HUD belt with healing / rejuvenation / rejuvenation / mana in slots 1-4 | reading which potion is in each slot, and that the answer survives a resolution change |
| `belt_empty_slot.png` | the same belt row with slot 4 emptied | an empty slot must read as empty, never as a potion - pressing a dead key during an emergency is the expensive failure |
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
| `wow/fishing_session_16k.wav` | the owner's 57 s World of Warcraft fishing recording, audio only, **16 kHz mono** (the video is 48 kHz stereo) - three bobber splashes at about 16.4, 37.0 and 51.6 s | `test_sound_match.py`, L-708: the splash is heard three times and nothing else is. 16 kHz keeps it at 1.8 MB, and scanning it with the 48 kHz reference clips checks a sample-rate change. (Rate is not entirely free: a spell sound in the third recording scored shape 0.67 at 48 kHz but 0.71 at 44.1 and 16 kHz, which is why the shape threshold is 0.78 - see `docs/wow_fishing.md`) |
| `wow/bar_full_6s.png` / `bar_draining_36s.png` / `no_bar_39s.png` / `no_bar_nameplates_0s.png` | full-resolution crops of the same video at 1920x1080, each exactly the region `wow.py` captures for the fishing bar (`fishing_bar.json`'s `search_region`) | V-314: bar seen / not seen. The nameplate frame is the one that rejected a template of the bar's end (0.78 on a monster health bar) |
| `wow/got_away_17s.png` / `no_got_away_10s.png` | the same, for the region searched for "Your fish got away!" | V-315/V-316 |
| `wow/fishing_session2_16k.wav` | the owner's second recording (2026-11-24 00-13-40), all 37 s, audio only, 16 kHz mono - one splash at about 32.25 s, the one the first version missed live | S-108 |
| `wow/fishing_session3_50-90s_16k.wav` | 50 s to 90 s of the third recording (2026-11-24 00-17-13), audio only, 16 kHz mono - splashes at about 11.45 and 32.3 s by its own clock, and the lure LANDING 0.3 s after the bar came up at 23.6 s | S-108; the landing is what card L-709 ignores |
| `wow/lure_bite_v4.png` / `lure_bite_choppy_v3.png` / `lure_pointer_no_bite_v4.png` | strips of 150x230 crops, **10 frames a second stacked top to bottom**, of the fourth (2026-11-24 00-24-51) and third recordings at 1920x1080: the cog WoW draws over the lure and the water under it. Crop origins in the window and first-frame times are in the test (`LURE_STRIPS`) | V-318: a bite at 27.2 s (calm water) and 24.4 s (choppy), and the mouse pointer crossing the lure at 10.0 s, which is not a bite |
| `wow/lure_v1_12s.png` / `no_lure_v4_29s.png` | single 150x230 crops: the lure in the first recording (different water and light), and the fourth recording's water after the lure is gone | V-317 |
| `wow/chat_zel_screenshot.png` | the owner's own screenshot of his chat box (2026-11-25), 436x147 at native size: a skill-up line, three green "You receive loot" lines (Catfish, Snapper, Snapper), NPC chatter wrapped over two lines, and a line cut off at the top | V-416/V-417/V-419/V-319: line cutting, matching (the two Snapper lines are the same line, nothing else is), the loot read, and the synthetic "box of repeats" built from its real lines |
| `wow/chat_loot_28_25s.png` / `chat_loot_28_5s.png` / `chat_loot_28_75s.png` | the fourth recording (2026-11-24 00-24-51) at 28.25 / 28.5 / 28.75 s, each exactly the chat box `wow.py` captures by default (406x178 at 1920x1080, `assets/wow/chat.json`): "You receive loot: [Gaffer Jack]" then "[Raw Rainbow Fin Albacore]" arriving under NPC dialogue, over moving grass | V-416/V-417/V-418/V-319: lines found as they arrive, one at a time, read correctly |
| `wow/chat_crab_behind.png` | the first recording at 39.0 s, the same box: empty chat, with a monster walking through behind it | V-418: never taken for new lines |
