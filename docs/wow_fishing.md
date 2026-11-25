# wow.py — the World of Warcraft fishing helper

A small offshoot of Hunter's Eye. It does not touch `main.py` or anything Diablo II; it reuses the
engine's screen capture, its reference-image check and its key presses, and adds two detectors: the
engine's first **ear** (`core/sound_match.py`, hears one particular sound in what the speakers are
playing) and **spot watch** (`core/spot_watch.py`, notices something suddenly appearing at one spot).

It reels in on **either** sign of a bite, whichever comes first: the **splash heard**, or the
**lure seen bobbing**. On Zel's four recordings the lure was seen 0.3-0.45 s before the sound was
heard, and each covers for the other: the sound when the lure is off screen or covered, the sight
when the game's sound never reaches the program.

> ## ⚠ Read this first: it can get your account banned
> World of Warcraft is an **online** game. Blizzard's terms of use forbid automating gameplay with
> third-party programs, and fishing bots are one of the best-known things they look for. An
> account caught running one can be **suspended or permanently banned**. `wow.py` reads only
> pixels and sound (no memory reading, no injected code), but that does not make it allowed.
> Use it at your own risk, on an account you are prepared to lose.

## The fishing panel — `python wow_gui.py`

The easiest way to run it (`pythonw wow_gui.py` on Windows for no console window). It starts and
stops `wow.py` for you and shows, live:

- **Auto-pause** — a countdown, 30 minutes by default (10 to 120 in the box next to it). When it
  reaches zero the helper pauses itself and plays three falling tones, so it never fishes on for
  hours while you are away. **Every F5 press — pause or resume — starts the 30 minutes over**, and
  so does the panel's Pause/Resume button, which does exactly what F5 does.
- **This session** — the session length, which counts **only while actually fishing** (it stands
  still while paused, auto-paused, with WoW not in front or with wow.py stopped), fish caught, the
  catch rate per hour of that fishing, one fish every how many seconds, bites reeled in (and how
  many got away or timed out), and the last catch.
- **Earnings** — what the session's catch is worth to a vendor, and that per hour of fishing, in
  gold, silver and copper.
- **Catches** — every fish caught, how many, its share, its vendor price, what they are worth and
  how many an hour. A fish with no price yet is in red: **double-click it** and type the tooltip's
  "Sell Price" (`24`, `2s 40c`, `19s`, `1g 5s`). If OCR ever misreads a name into a new "fish",
  pick it and press **It's a misreading of...** to fold it into the right one.
- **All time** — every catch ever counted, from `recordings/wow/catches.csv`.
- **Log** — everything `wow.py` prints. **Setup** — where the chat box is (below).

Closing the panel stops `wow.py` too. `F5` and `End` keep working while it runs.

## Counting catches — the chat box

`wow.py` reads WoW's chat box (bottom left) for the green **"You receive loot: [...]"** lines.
There is **no list of fish built in**: whatever the chat says was looted is counted under that
name, and a name never seen before is added by itself (price unknown until you set one).

| file | what | in git? |
|---|---|---|
| `assets/wow/fish_prices.json` | the starting prices, from Zel's tooltips on 2026-11-25 | yes |
| `recordings/wow/fish_prices.json` | **yours**: new fish, prices typed into the panel, merged misreadings. Wins over the one above | no — `git pull` never touches it |
| `assets/wow/chat.json` | where the chat box is by default (WoW's default layout at 1920x1080) | yes |
| `recordings/wow/chat.json` | **yours**, written by the panel's *Set chat area* | no |
| `recordings/wow/catches.csv` | one row per catch, every session | no |

The starting prices are each tooltip's **Sell Price** line, which is what a vendor pays: Raw
Brilliant Smallfish 19c, Raw Slitherskin Mackerel 18c, Raw Longjaw Mud Snapper 20c, Oily Blackmouth
24c, Raw Rainbow Fin Albacore 26c, Darkshore Grouper 30c, Raw Bristle Whisker Catfish 40c, 19 Pound
Catfish 19s. (Zygor's own "Vendor" line disagreed for the fish — it showed 1-4c — but agreed for the
19 Pound Catfish; the game's line was taken.)

**If your chat box is not where the default says** (moved, resized, another font size, another
resolution), open the panel's **Setup** tab, press **Set chat area...**, click into WoW, and drag a
box around the chat text in the screenshot it shows. Then **Test chat reading** with a loot line
on screen (or `python wow.py --read-chat`): it lists every line it sees and marks the loot ones.

### How it reads the chat — `core/text_feed.py`, then OCR

Reading the whole box every time cannot tell an old "You receive loot: [Snapper]" from a new one:
same words, one line higher. What tells them apart is that **old lines slide up**. So the box is
captured 4 times a second (about 400x180 pixels, 3-9 ms a look) and `core/text_feed.py`, a new
detector that knows nothing about WoW, works out which lines are new:

1. **Ink.** The brightest colour channel (the loot green is 0,160,0 — a grey conversion makes it
   94, EH#48), then a morphological top-hat, which keeps thin bright strokes and drops the smooth
   world behind the see-through box — dark water and bright fog alike.
2. **Lines.** Chat lines are 18 px apart at 1080 p. They are cut where the gap rows hold the least
   ink; a line cut off by the box's edge is left alone until it is whole.
3. **Matching.** A line matches itself in the next picture by correlation of its ink, faint ink
   (the background showing through) zeroed first. Measured: the same line two frames apart over
   moving grass 0.90-1.00, a line fading to 60% still matches; different lines at most 0.58, loot
   lines for two different fish 0.47. Threshold 0.75.
4. **Alignment.** The new picture should be the old one with its top lines gone; whatever is left
   below is new. A picture that lines up with nothing (the box scrolled by hand, a monster walking
   behind an empty box) is "cannot tell" and counts nothing — it never guesses a whole box is new.

Only new lines with the loot green in them (335-379 green pixels on a loot line, 0 on every other
line measured) are OCR'd, about 0.1 s each, so OCR runs once per catch. Every line of Zel's chat
screenshot read exactly; the opening bracket is sometimes read as `(` and is allowed for. A misread
letter snaps to the closest known name (`Raw Longiaw Mud Snapper` → `Raw Longjaw Mud Snapper`),
but only to a name with the same numbers in it — `17 Pound Catfish` and `19 Pound Catfish` are 94%
alike and different fish.

Measured on all four recordings: the two loot lines in the fourth (Gaffer Jack and Raw Rainbow Fin
Albacore at 28.5 s) counted once each, the moment each arrived; nothing counted anywhere else,
including a crab walking behind the empty box in the first.

**The one case pixels cannot settle, said plainly.** A box showing nothing but the same loot line
(eight Snappers in a row) looks exactly the same after a ninth arrives. `text_feed` flags that as
*ambiguous* rather than inventing a line. `wow.py` knows one more thing — it just reeled in a bite —
so when it reels in, no new loot line arrives within 5 s, the box looked ambiguous meanwhile, and
the bottom line is a loot line, it counts that fish once more, marked **inferred** (in the log, the
panel and `catches.csv`). A fish that got away cancels it. Any other line in the box (a skill-up,
another fish, NPC chatter) makes the case unambiguous again.

Loot that arrives while paused is not counted as fishing, except in the first 3 s after the pause
(the fish reeled in just before it).

## Running it

One-time: `python -m pip install -r requirements.txt` (this adds `pyaudiowpatch`, Windows only,
which is what lets a program hear the speakers' output).

| command | what it does |
|---|---|
| `python wow.py --dry-run` | **Try this first.** Watches and listens exactly as the real thing does, but prints `would press F` instead of pressing. |
| `python wow.py` | The real thing. Keys are only pressed while WoW is the window in front. |
| `python wow.py --replay FILE` | Offline: lists every splash heard in a recording (`.wav`, or a video if `ffmpeg` is installed) and, for a video, every time the lure was seen bobbing. Nothing is pressed. |
| `python wow.py --check-audio` | **If the splash is never heard live.** Listens for 15 s (play WoW, or one of your fishing videos), prints how loud it was each second, saves what it heard to `recordings/wow/`, and says how many splashes were in it. "No sound at all reached wow.py" means the program is listening to the wrong device. |
| `python wow.py --list-audio` | The sound devices it can listen to, with the Windows default marked. |
| `python wow.py --read-chat` | One look at the chat box: every line it sees, the loot lines read, and the picture saved as `recordings/wow/chat_check.png`. The panel's *Test chat reading* runs this. |
| `python wow.py --auto-pause 45` | Auto-pause after 45 minutes instead of 30 (10, 15, 20, 30, 45, 60, 90 or 120). |

Every run writes a log to `recordings/wow/wow_<date>_<time>.log` with more than the console shows:
the sound level every 10 s (or "nothing at all from ..."), every loud sound that was **not** taken
for a splash and which test it failed, the fishing bar and the lure coming and going, and every
bite that was noticed but not acted on, with why (paused, WoW not in front, not fishing yet, the
lure only just landed). When a fish is missed, that file is what to send.

Hotkeys, global like Hunter's Eye's (they work while WoW has focus):

| key | effect | sound |
|---|---|---|
| `F5` | pause, and press again to resume. **Either one restarts the auto-pause countdown** | pause: two falling tones; resume: two rising tones |
| (none) | auto-pause: 30 minutes with no `F5` | three falling tones |
| `End` | quit, and print how many were reeled in (and whether by sound, sight or both) / missed, what was caught, the catch rate and what it is worth | |

`F5` reaches WoW too (by default it targets party member 4 — harmless when solo).

## What it does

```
  cast (F) --> fishing bar appears --> watch the lure and listen
                                         |
      splash heard, or lure bobbed ------+--> press F at once (reel in)
        (not in the first 1.5 s: that is the lure landing)
      "Your fish got away!" appears -----+
      the bar went away, no bite --------+--> wait a random 1-6 s --> cast (F) --> ...
      no bite for 30 s ------------------+--> press F (restart), then the same wait
```

Every press is held for a random 0.2–0.6 s, most often about 0.3 s. If three casts in a row never
bring up the fishing bar (not facing water, wrong key) it stops and says why rather than pressing
forever.

## Settings

At the top of `wow.py`, under `SETTINGS`. Edit, then restart.

| setting | default | meaning |
|---|---|---|
| `FISH_KEY` | `f` | the key that reels in and casts |
| `RECAST_DELAY_SECONDS` | `(1.0, 6.0)` | random wait before each cast |
| `NO_SPLASH_TIMEOUT_SECONDS` | `30.0` | bar up this long with no bite → press anyway and restart |
| `LISTEN_FOR_SPLASH` / `WATCH_LURE` | `True` / `True` | which signs of a bite to use; either one reels in |
| `AUDIO_DEVICE` | `None` | `None` = the Windows default speakers; else part of a device's name from `--list-audio`, for when WoW plays its sound somewhere else (a headset) |
| `LURE_CHECKS_PER_SECOND` | `20` | how often the lure is looked at (about 2 ms a look) |
| `KEY_HOLD_SECONDS` / `KEY_HOLD_TYPICAL_SECONDS` | `(0.2, 0.6)` / `0.28` | how long each press is held |
| `WINDOW_TITLE` | `World of Warcraft` | the window it looks at and must be in front |
| `AUTO_PAUSE_MINUTES` | `30` | pause by itself after this long with no `F5` (the panel and `--auto-pause` override it per run) |
| `READ_CHAT` | `True` | count catches from the chat box |

**The timeout decides which slow bites are abandoned.** The cast bar lasts about 30 s. In the
reference video the second cast's splash was heard 20.2 s after its bar appeared (bar at 17.0 s,
splash heard at 37.18 s), so 20 s would have restarted just before it. Zel tried 15 s in game, then
20 s, then found it "keeps reeling in the fish before the fishing bar runs out" and asked for 30 s
(v0.025).

## How it decides

### The lure — the cog over it, then `core/spot_watch.py`

**Finding it.** WoW draws a small **cog icon** over whatever the interact key would use, and while
fishing that is the lure. The cog is UI art: the same size and look on any water, in any light, at
any camera distance, which the lure itself is not (a template of the lure cut from one recording
scored only 0.5-0.7 on the others). The cog scored **0.96-1.00** in all four recordings and at most
0.64 when no lure was out (`assets/wow/bobber.json`, threshold 0.85). The lure sits a fixed
distance below it: **4 px left, 91 px down** at 1920 wide, measured the same in all four.

**Watching it.** A bite throws up white spray around the lure, and the lure dips and jumps. What
is counted is the **colourless, bright** pixels (saturation ≤ 44, brightness ≥ 95) in a 72x72 patch
on the lure. Water, even choppy water, has 10-40 of them; a bite 100-220. A bite is a frame with at
least **3x the patch's recent median plus 1.2% of the patch** (60 px at 1920 wide). The recent
median is its own per lure, so calm and choppy water each set their own bar.

| | result on the four recordings (2 min 42 s of fishing in all) |
|---|---|
| bites seen | **9 of 9**, at 20, 12 and 8 looks a second |
| false alarms | **0** - including the mouse pointer crossing the lure (it reached 102 of the 114 needed) |
| how early | 0.0-0.25 s **before** the splash sound starts, so 0.3-0.45 s before the ear hears it |
| cost | searching for the cog: 4 ms, 4 times a second, at half size; watching: 2 ms a look on a 140x220 capture |

Two lessons from tuning it. Asking for two bite frames in a row instead of one lost 4 of 9 bites at
12 looks a second (the spray lasts about a quarter of a second), so the ratio, not a second frame,
is what keeps one odd frame out. And the first second after the lure appears is never judged: that
is the lure **landing**, which throws up its own small splash.

### The splash — `core/sound_match.py`

A splash is noise, and no two splashes have the same waveform, so it is not matched as a waveform.
What repeats is the **spectrum**: how the energy spreads over 19 frequency bands (180 Hz–7.5 kHz).
Three tests must pass on the same moment:

| test | threshold | splashes in the video | best non-splash in the video |
|---|---|---|---|
| **sudden** — louder than the last 2 s (dB, averaged over the bands) | 15 dB | 37–54 dB | 28–30 dB (ambient fading in after near-silence) |
| **loud for this place** — louder than the loud end (90th percentile) of the last 15 s | 8 dB | 16–24 dB | 1.8–2.3 dB |
| **shaped like the reference** — spectral correlation with the best of `assets/wow/splash/*.wav` | 0.78 | 0.85–0.99 | −0.4 for a tonal sound; a spell 0.71 (at 44.1/16 kHz) |

The first test alone was not enough: the ambient sound of the water fading in at 16.0 s rose 29 dB
over the near-silence before it. The second test is what separates it (+1.8 dB). The third is what
makes it *that* sound rather than *any* loud sound.

**A library, not an average (v0.025).** Splashes come in several distinct sounds. The reference is
now **8 clips** (`splash_1-3` from the first recording, `4` from the second, `5-8` from the third),
and a sound is compared with each and scored by the best. Each clip's fingerprint is the **first
0.3 s** of the sound, because that is the moment the matcher decides on; the loudest part, used
before, comes later for one variant and matched a spell's crackle better than its own start.

Measured on all four recordings, each converted to 16, 44.1 and 48 kHz: **every splash heard, 0
false alarms** (30 of 30). Leaving a recording's own clips out of the library still hears all of
its splashes, which is the case that matters for a new fishing spot. The threshold went from 0.70
to 0.78 because one loud spell sound in the third recording scored 0.67 at 48 kHz but 0.71 at 44.1
and 16 kHz - many PCs run their speakers at 44.1 kHz.

**Thin margins, said plainly.** A splash is reported the moment all three tests pass, which is
typically right when "louder than recently" crosses its +8 dB, so the replay shows +8 to +10 there.
In the first recording a wave breaking on the shore has a splash's shape and peaks at +7.5 dB, just
under. The lure watcher is the stronger of the two signals; the sound is the backup.

Measured results on the reference recording (57 s, three splashes):
- **3 of 3 splashes heard, 0 false alarms**, at both 48 kHz and 16 kHz.
- Leave-one-out: each splash is still heard with the reference built from the other two only.
- Any threshold from 4 to 14 dB for the second test gives the same 3-of-3, 0 false alarms.
- A second splash only 3.5 s after the first is heard (a heard splash is kept out of the
  "recent loudness" it would otherwise raise). One 12 dB quieter is heard; 18 dB quieter is not.
- Heard **0.13–0.25 s after the sound starts**. The matcher costs about 3% of one CPU core.

### The screen — `core/presence.py`, the same check as Hunter's Eye's in-play guard

| what | how | measured on the video |
|---|---|---|
| the fishing bar | the word **"Fishing"** under the bar (`assets/wow/fishing_bar.json`) | bar up: 0.98–1.00; no bar: at most 0.38; threshold 0.70 |
| "Your fish got away!" | the **yellow letters only** — both screen and template are reduced to "which pixels are that yellow" first, so the water behind does not matter (`fish_got_away.json`) | message up: 0.77–0.86; no message: at most 0.20; threshold 0.60 |

The bar's green fill drains as the cast runs and other bars (monster nameplates) share its frame
art — a template of the bar's left end scored 0.78 on a nameplate — so only the label is used.
Both searches look in a region of the WoW window (fractions in the `.json`), captured as two small
rectangles 8 times a second.

"Your fish got away!" only counts when it **appears** while fishing: it stays on screen for a few
seconds, and with a 1 s recast it can still be showing when the next cast starts.

## Why the first version heard nothing on later recordings (v0.025)

Zel's second and third recordings (2026-11-24) missed every splash live. Offline, the v0.024
matcher missed them too: those splashes were variants the three reference clips did not have, and
a matched sound was kept out of the 2 s background as well as the recent level, so a long spell
sound that matched once stayed "sudden" and fired every 3 s. Both are fixed (the library, and
"matched" sounds now count toward the background but not the recent level).

But on the recordings Zel *said* it heard **nothing at all**, and the console prints `splash heard`
for every match whether or not it presses. So the other suspect is the sound never reaching the
program live: WoW playing to a device other than the Windows default, or a device that only
delivers while something plays. That is what `--check-audio`, `--list-audio`, `AUDIO_DEVICE` and the
10 s heartbeat in the log are for. Zel's timestamps (22, 58, 79 and 114 s) were also about 3 s
before the splashes in the audio (24.5, 61.5, 82.3, 117.0 s).

## What the reference video showed

All three splashes in the video ended with **"Your fish got away!" 0.2–0.6 s after the splash
sound began** (16.42 → 16.65 s, 37.05 → 37.25 s, 51.60 → 52.20 s). That is a very short window.
`wow.py` presses about 0.15–0.25 s after the sound starts, which is inside it, but not by much.
Two things decide whether that is enough, and neither can be checked from a recording:
- **Does WoW act on the key going down, or coming up?** Modern clients act on key-down by default
  (the `ActionButtonUseKeyDown` setting). If yours acts on key-up, the 0.2–0.6 s hold delays every
  reel-in by that much and most fish will get away — lower `KEY_HOLD_SECONDS` to fix it.
- Whether the window really is that short, or whether the fish in the video got away for another
  reason (pressed too early, the interact key not aimed at the bobber).

## Not tested, and why

**The catch counter and the panel (2026-11-25)** were built from Zel's chat screenshot and the two
loot lines in his fourth recording; the panel was tested against a stand-in for `wow.py` (T-406 to
T-410). Not yet seen live: the default chat area on his screen (the recordings' layout), a long
session's worth of loot lines, the inferred count in a box of repeats, and the Set chat area
screenshot on Windows. `python wow.py --read-chat` with a loot line on screen is the first check.

v0.024 was measured on one 57-second recording, then **tried by Zel in game on 2026-11-24 ("works
like a charm")**; later runs missed splashes (above). v0.025 is measured on all four recordings,
not yet in game. Still untested:
- **The lure watcher live.** It needs WoW's soft-target interact icons (the cog), which come with
  an interact key; if no cog appears over your lure, the log says `watching the lure` but never
  `lure found`, and the sound still works on its own. Keep the mouse pointer off the lure: it is
  white, and crossing the lure came within 12 pixels of reading as spray.
- **Other places and sounds**: combat, other players' splashes, music, a city. Another player's
  bobber splashing nearby will sound like yours — there is no way to tell them apart by ear.
- **Other UI layouts or scales.** The templates were cut at 1920×1080. A different resolution is
  handled (the template is rescaled to the window width), a different *UI scale* or an addon that
  moves the cast bar is not — widen `search_region` in the `.json`, or re-cut the template.
- The "got away" template came from a cropped screenshot, assumed to be at 1920×1080 scale; it
  matched the video's own frames (0.77–0.86), which supports that.
- **macOS**: there is no system loopback, so it cannot hear the game. A virtual device such as
  BlackHole would be the way in (parked in `todolist.md`).

## Adding another sound

`core/sound_match.py` knows nothing about fishing. To listen for something else, put a few `.wav`
clips of it (16-bit, any sample rate, cut roughly — half a second either side is fine) in a folder
and hand that folder to `load_reference_folder()`. Check it with `python wow.py --replay` on a
recording first: the thresholds were tuned on a splash, and a different sound may need different
ones.
