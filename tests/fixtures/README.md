# Test fixtures

Real frames the tests measure against, committed so the tests always have them.

**Do not point a test at `assets/zelScreenshots/`.** That folder is the project owner's scratch
space for sharing screenshots during a session; it is gitignored and gets cleared whenever it
suits them. A test that reads from it does not fail when the file disappears - it *skips*, which
looks exactly like passing. That happened twice: `test_presence.py`'s whole real-screenshot
section quietly stopped running, and `test_quit_game.py`'s button tests are still skipping because
the Esc-menu screenshot they used is gone.

If a new test needs a real frame, add it here.

## What each one is

| file | what it is | what it is for |
|---|---|---|
| `lobby.png` | the Create Game lobby, game name `z25pin38` | form location, row spacing, reading + incrementing the name, and the in-play check correctly saying "not in a game" |
| `lobby_name_clash.png` | the same lobby with "A Game Already Exists With That Name" | detecting the clash dialog, and not detecting it when it is absent |
| `in_game_tooltip.png` | in game, an item tooltip covering the right-hand orb | the occlusion bug: one in-play reference covered, the other still visible |
| `lobby_form_crop.png` | a close crop of the Create Game form | reading a game name out of the text box |

## Why they are mostly black

Everything outside the regions the tests actually search is masked out, at the **same frame
dimensions** - so every fraction, offset and coordinate is unchanged while the files compress to
roughly half. Rebuilt and verified equivalent to the originals: identical in-play scores
(0.815 / 0.272 / 0.268), identical row spacing, click points, name read and dialog detection.

One thing that took a second pass, and is the rule for adding any fixture: **preserve the numbers
a test asserts on, not just the answers.** The first attempt masked the orb corners out of the
lobby frames. Every True/False still matched - but the in-play score fell from 0.272 to 0.038,
so the test that checks the *margin* between in-play and lobby would have been measuring black
pixels and would have passed however far the real margin eroded.
