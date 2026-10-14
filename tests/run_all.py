"""Runs every test file, then says which test CARDS failed. No game required.

    python tests/run_all.py          # one summary line per file, details only for failures
    python tests/run_all.py -v       # also print every file's full output

This is exactly what CI runs on every pull request (.github/workflows/tests.yml), so a green run
here is the same bar a merge has to clear.

Each test file runs in its OWN process, the same way it runs by hand. They were written to be run
directly - module-level state, sys.exit at the end, some patch globals in the modules they test -
so importing them all into one process would let one file's leftovers change another's result.

A file that runs longer than TIMEOUT_SECONDS is killed and counted as a failure. A test suite that
can hang is no better than a pipeline that can: nobody watching a CI run would ever see it finish.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIMEOUT_SECONDS = 600

CARD = re.compile(r"^([A-Z]-\d{3})  (.+)$")
OK = re.compile(r"^\s+(ok|PASS)\s")
FAIL = re.compile(r"^\s+FAIL\s+(.*)$")
SKIP = re.compile(r"\b(SKIPPED|NOT RUN)\b")
#Two files print only their failures, then a closing "N checks, M failed" line - count from that.
TALLY = re.compile(r"^(\d+) checks, (\d+) failed$")


def run(path):
    """(exit code, output, seconds). Exit code None means it was killed for running too long."""
    start = time.perf_counter()
    try:
        #UTF-8 both ways: on Windows a child writing to a pipe otherwise uses the ANSI code page,
        #and one non-Latin character in a check label would crash the test instead of the check.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        done = subprocess.run([sys.executable, str(path)], cwd=TESTS.parent, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=TIMEOUT_SECONDS)
        return done.returncode, done.stdout + done.stderr, time.perf_counter() - start
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        return None, partial, time.perf_counter() - start


def summarise(output):
    """(cards seen, checks passed, [(card, failed check)], [skip notes])."""
    cards, passed, failed, skips = [], 0, [], []
    card = "(before any card)"
    for line in output.splitlines():
        match = CARD.match(line)
        if match:
            card = match.group(1)
            cards.append(card)
        elif OK.match(line):
            passed += 1
        elif FAIL.match(line):
            failed.append((card, FAIL.match(line).group(1).strip()))
        elif TALLY.match(line):
            total, bad = TALLY.match(line).groups()
            passed += int(total) - int(bad)
        elif SKIP.search(line):
            skips.append((card, line.strip()))
    return cards, passed, failed, skips


def main():
    verbose = "-v" in sys.argv[1:]
    #A console that cannot show a character gets a '?' instead of a crash in the summary.
    sys.stdout.reconfigure(errors="replace")
    files = sorted(TESTS.glob("test_*.py"))
    broken, all_cards, all_skips = [], 0, []
    total_start = time.perf_counter()

    for path in files:
        code, output, seconds = run(path)
        cards, passed, failed, skips = summarise(output)
        all_cards += len(cards)
        all_skips += [(path.name, card, note) for card, note in skips]
        ok = code == 0 and not failed
        status = "PASS" if ok else ("HUNG" if code is None else "FAIL")
        print(f"{status}  {path.name:<28} {len(cards):3d} cards  {passed:4d} checks  {seconds:5.1f}s")
        if verbose:
            print(output)
        if not ok:
            broken.append(path.name)
            for card, check in failed:
                print(f"        {card}: {check}")
            if code is None:
                print(f"        killed after {TIMEOUT_SECONDS}s - something in it waits forever")
            elif not failed:
                #Crashed before reporting a FAIL - show where, since there is no check to point at.
                print("        crashed - last lines of its output:")
                for line in output.strip().splitlines()[-12:]:
                    print(f"          {line}")

    print(f"\n{len(files)} files, {all_cards} cards, {time.perf_counter() - total_start:.0f}s")
    if all_skips:
        #Listed every time on purpose: a skip looks exactly like a pass unless someone says so.
        print("\nNot run here (each says why):")
        for name, card, note in all_skips:
            print(f"  {card} ({name}): {note}")
    if broken:
        print(f"\nFAILED: {', '.join(broken)}")
        return 1
    print("\nALL TEST FILES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
