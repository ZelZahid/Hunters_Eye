# wow.py — the World of Warcraft fishing helper

A small offshoot of Hunter's Eye. It does not touch `main.py` or anything Diablo II; it reuses the
engine's screen capture, its reference-image check and its key presses, and adds the engine's first
**ear**: a matcher that hears one particular sound in what the speakers are playing.

> ## ⚠ Read this first: it can get your account banned
> World of Warcraft is an **online** game. Blizzard's terms of use forbid automating gameplay with
> third-party programs, and fishing bots are one of the best-known things they look for. An
> account caught running one can be **suspended or permanently banned**. `wow.py` reads only
> pixels and sound (no memory reading, no injected code), but that does not make it allowed.
> Use it at your own risk, on an account you are prepared to lose.

## Running it

One-time: `python -m pip install -r requirements.txt` (this adds `pyaudiowpatch`, Windows only,
which is what lets a program hear the speakers' output).

| command | what it does |
|---|---|
| `python wow.py --dry-run` | **Try this first.** Watches and listens exactly as the real thing does, but prints `would press F` instead of pressing. |
| `python wow.py` | The real thing. Keys are only pressed while WoW is the window in front. |
| `python wow.py --replay FILE` | Offline: lists every splash heard in a recording (`.wav`, or a video if `ffmpeg` is installed). Nothing is pressed. |

Hotkeys, global like Hunter's Eye's (they work while WoW has focus):

| key | effect | sound |
|---|---|---|
| `F5` | pause | two falling tones |
| `F6` | resume | two rising tones |
| `End` | quit, and print how many were reeled in / missed | |

`F5`/`F6` reach WoW too (by default they target party members 4 and 5 — harmless when solo).

## What it does

```
  cast (F) --> fishing bar appears --> listen
                                         |
      splash heard ----------------------+--> press F at once (reel in)
      "Your fish got away!" appears -----+
      the bar went away, nothing heard --+--> wait a random 1-6 s --> cast (F) --> ...
      nothing heard for 30 s ------------+--> press F (restart), then the same wait
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
| `NO_SPLASH_TIMEOUT_SECONDS` | `30.0` | bar up this long with no splash → press anyway and restart |
| `KEY_HOLD_SECONDS` / `KEY_HOLD_TYPICAL_SECONDS` | `(0.2, 0.6)` / `0.28` | how long each press is held |
| `WINDOW_TITLE` | `World of Warcraft` | the window it looks at and must be in front |

**The timeout decides which slow bites are abandoned.** The cast bar lasts about 30 s. In the
reference video the second cast's splash was heard 20.2 s after its bar appeared (bar at 17.0 s,
splash heard at 37.18 s), so 20 s would have restarted just before it; 15 s (the first default)
would have too, and 25 s would have caught it. Zel tried 15 s and then 20 s in game on 2026-11-24;
20 s kept restarting before the bar ran out, so it is 30 s - about the bar's full length. If the
bar ends first, that is caught on its own: the bar going away with no splash is a missed round.

## How it decides

### The splash — `core/sound_match.py`

A splash is noise, and no two splashes have the same waveform, so it is not matched as a waveform.
What repeats is the **spectrum**: how the energy spreads over 19 frequency bands (180 Hz–7.5 kHz).
Three tests must pass on the same moment:

| test | threshold | splashes in the video | best non-splash in the video |
|---|---|---|---|
| **sudden** — louder than the last 2 s (dB, averaged over the bands) | 15 dB | 37–54 dB | 28–30 dB (ambient fading in after near-silence) |
| **loud for this place** — louder than the loud end (90th percentile) of the last 15 s | 8 dB | 16–24 dB | 1.8–2.3 dB |
| **shaped like the reference** — spectral correlation with `assets/wow/splash/*.wav` | 0.70 | 0.85–0.99 | −0.4 for a tonal sound; casts 0.55–0.84 |

The first test alone was not enough: the ambient sound of the water fading in at 16.0 s rose 29 dB
over the near-silence before it. The second test is what separates it (+1.8 dB). The third is what
makes it *that* sound rather than *any* loud sound.

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

Everything above was measured on one 57-second recording, then **tried by Zel in game on
2026-11-24 ("works like a charm")**. Still untested:
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
