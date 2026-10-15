"""Loading user-editable settings from user_config.txt.

WHY THIS IS A MODULE AND NOT A DICT IN main.py: the same reason assets/targets.txt and
assets/meters.json exist. Anything a user should be able to change without editing code belongs
in a file they can open, and the code that reads it belongs somewhere that can validate it
properly rather than trusting whatever it finds.

THIS MODULE KNOWS NOTHING ABOUT DIABLO II, potions, or belts. It parses "when this named meter
drops below this level, press these keys, then wait this long" - a shape that describes a potion
belt, a drone returning to base on low battery, and a pump switching on when a tank runs low,
equally well. Every game-specific value (which meters exist, which keys, which thresholds) lives
in the file, not here. Same split as text_detection.py vs targets.txt.

STABILITY IS THE POINT OF MOST OF THIS FILE. It runs unattended next to a game, so a typo in a
config file must never be able to stop it or, worse, let it run with a setting nobody intended.
Every value is range-checked, every bad one falls back to a documented default with a console
warning naming the section and the key, and a missing file is a normal condition rather than an
error. The one thing it will NOT do is guess: a rule that names a meter that does not exist is
dropped rather than pointed at some other meter.
"""
from __future__ import annotations

import configparser
import contextlib
import io
import os
import re
import tempfile
from collections import namedtuple

#One rule: when `meter` reads at or below `at_or_below`, press one of `keys`, then leave THIS
#rule alone for `cooldown` seconds. `label` is the section name, used for logging and to key the
#per-rule cooldown. Deliberately the same shape main.py already used as a literal.
#`keys` is the literal keys to press. `use` names a SUPPLY instead - "press whichever key
#currently holds this" - which a caller resolves against something it can see (main.py reads the
#potion belt; core/slots.py is what reads it). That indirection is the point: a key written down
#by hand is a claim about the world that stops being true the moment the player rearranges the
#belt or runs out, and the program then presses a key that does nothing, or worse, spends the
#wrong supply. `keys` remains as the fallback for when the supply cannot be seen at all.
#`only_if_missing` names a supply this rule is a LAST RESORT for: it fires only when none is left.
Rule = namedtuple("Rule", "meter at_or_below keys cooldown label use only_if_missing",
                  defaults=("", ""))

#Reserved section name for things that are not rules. Every other section is a rule.
SETTINGS_SECTION = "settings"

DEFAULTS = {
    "enabled": True,
    "min_gap": 0.6,
    "snooze": 10.0,
    "ignore_below": 0.03,
    #Game-flow settings (see main.py's next_game). use_password is off by default because the
    #safe thing for an automatic sequence is to leave a field ALONE unless told otherwise - and
    #because an unwanted password on a game is invisible until someone cannot join it.
    "use_password": False,
    "password": "123",
    "game_name": "",
    #Teleporting onto a drop before picking it up (see main.py's auto-collect). OFF by default,
    #and deliberately so: every other default here reproduces what the program did before the
    #setting existed, and a key pressed into a live game is the last place to break that rule -
    #on a character with something else bound to that key, a default of "f" would cast it.
    "teleport_key": "",
    #How far away an item has to be before teleporting to it beats walking, as a fraction of the
    #screen's height. Nothing here knows what a "yard" is; a fraction of the frame is the only
    #unit that survives a resolution change, which is the same reason meters.json stores regions
    #that way.
    "teleport_min_distance": 0.25,
    #Seconds between repeat attempts when an item is collected with a KEY rather than a click.
    #Its own setting because the two are paced by completely different things: a click means
    #"walk over there", so re-issuing it too fast retargets the character before it arrives,
    #while a cast is over the instant it lands and is paced by the character's cast rate - which
    #is gear, and therefore the user's business rather than a developer's.
    "key_collect_retry": 0.35,
    #How long to leave the cursor sitting on a target before acting on it. Two values, because
    #the two events are not the same shape: a click's button-down carries the cursor position in
    #its own Win32 message, while a keypress carries no coordinates at all and is resolved against
    #whatever the game last decided the cursor was over. They are settings rather than constants
    #because what a game needs here depends on the game and the machine, and the only way to find
    #it is to try numbers while playing.
    "click_settle": 0.05,
    "aim_settle": 0.20,
    #Whether the reference-image detector (main.py's needle search) runs at all. ON when the file
    #is missing, so behaviour is unchanged from before the setting existed - the same rule every
    #default here follows. It is the single most expensive thing on the per-frame path (~3ms of a
    #~4.5ms frame), so a user who has no picture to look for should be able to stop paying for it.
    "image_search": True,
}

#Sanity bounds. These are not taste, they are "this value cannot possibly be what you meant":
#a negative cooldown, a threshold above 100%, a min_gap of an hour. Anything inside the bounds is
#the user's business even if it is unusual.
_LIMITS = {
    "min_gap": (0.0, 60.0),
    "snooze": (0.0, 3600.0),
    "ignore_below": (0.0, 1.0),
    "cooldown": (0.0, 3600.0),
    "below": (0.0, 1.0),
    "teleport_min_distance": (0.0, 1.0),
    "key_collect_retry": (0.0, 10.0),
    "click_settle": (0.0, 2.0),
    "aim_settle": (0.0, 2.0),
}


class Config(namedtuple("Config",
                       "enabled min_gap snooze ignore_below use_password password game_name "
                       "teleport_key teleport_min_distance key_collect_retry click_settle "
                       "aim_settle image_search rules source")):
    """Loaded settings. `source` is the file it came from, or None when defaults were used."""

    @property
    def loaded(self):
        return self.source is not None


_FIELDS = ("enabled", "min_gap", "snooze", "ignore_below", "use_password", "password",
           "game_name", "teleport_key", "teleport_min_distance", "key_collect_retry",
           "click_settle", "aim_settle", "image_search")


def _defaults(rules):
    """A Config with every setting at its documented default. One definition, so a new setting
    cannot be added to DEFAULTS and forgotten on one of the fall-back paths."""
    return Config(*[DEFAULTS[k] for k in _FIELDS], tuple(rules), None)


def _warn(where, message):
    print(f"WARNING: user_config {where}: {message}")


def _number(raw, where, key, default):
    """Parses a number, accepting a trailing '%' or 's' so the file can read naturally.

    '20%' and '0.20' both mean 0.20 - a percent sign is how a person naturally writes a
    threshold, and rejecting it would be a papercut in the one file meant to be hand-edited.
    """
    text = str(raw).strip().lower().rstrip("s").strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        value = float(text)
    except ValueError:
        _warn(where, f"{key} = {raw!r} is not a number; using {default}")
        return default
    if percent:
        value /= 100.0
    lo, hi = _LIMITS.get(key, (None, None))
    if lo is not None and not (lo <= value <= hi):
        _warn(where, f"{key} = {raw!r} is outside {lo}-{hi}; using {default}")
        return default
    return value


def _keys(raw, where):
    """'2, 3' -> ('2', '3'). Order is the order they will be used in."""
    keys = tuple(k.strip() for k in str(raw).split(",") if k.strip())
    if not keys:
        _warn(where, "keys is empty; rule ignored")
    return keys


def load(path, known_meters=None, default_rules=()):
    """Reads `path` and returns a Config. Never raises.

    known_meters: optional collection of meter names that actually exist. A rule naming anything
        else is DROPPED with a warning rather than silently watching nothing - a rule that can
        never fire is exactly as bad as one that fires wrongly, and much harder to notice.
    default_rules: used when the file is missing or defines no usable rule at all, so the program
        behaves identically to before this file existed rather than quietly doing nothing.
    """
    #interpolation=None is REQUIRED, not stylistic. configparser's default interpolation treats
    #'%' as a substitution marker, so a perfectly reasonable "below = 20%" raises
    #InterpolationSyntaxError - and it raises on ACCESS, not on read_file(), so it lands in the
    #middle of parsing rather than anywhere a file-level try/except would catch it. A percent
    #sign is how a person naturally writes a threshold in the one file meant to be hand-edited.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError:
        return _defaults(default_rules)
    except (OSError, configparser.Error) as exc:
        #A malformed file is worth shouting about - the user edited it and meant something.
        print(f"WARNING: could not read {path}: {exc}\n"
              f"         Falling back to built-in defaults.")
        return _defaults(default_rules)

    settings = dict(DEFAULTS)
    if parser.has_section(SETTINGS_SECTION):
        section = parser[SETTINGS_SECTION]
        for key in ("min_gap", "snooze", "ignore_below", "teleport_min_distance",
                    "key_collect_retry", "click_settle", "aim_settle"):
            if key in section:
                try:
                    settings[key] = _number(section[key], f"[{SETTINGS_SECTION}]", key, DEFAULTS[key])
                except Exception as exc:  # noqa: BLE001
                    _warn(f"[{SETTINGS_SECTION}]", f"{key} could not be read ({exc}); "
                                                   f"using {DEFAULTS[key]}")
        for key in ("enabled", "use_password", "image_search"):
            if key in section:
                try:
                    settings[key] = section.getboolean(key)
                except ValueError:
                    _warn(f"[{SETTINGS_SECTION}]", f"{key} = {section[key]!r} is not yes/no; "
                                                   f"using {DEFAULTS[key]}")
        for key in ("password", "game_name", "teleport_key"):
            if key in section:
                #Taken verbatim - a password is not a number and not a name to be tidied up.
                #teleport_key is not checked against the keyboard HERE: this module has no
                #business importing an input library to validate a string, and the caller that
                #will actually press it is the one that can say what to do when it cannot.
                settings[key] = section[key].strip()

    rules = []
    for name in parser.sections():
        if name == SETTINGS_SECTION:
            continue
        where = f"[{name}]"
        section = parser[name]
        #`keys` OR `use` - either says what to press, and "use" is the better one because it says
        #what to press it FOR. A rule with neither can never do anything.
        required = ("meter", "below") + (() if ("keys" in section or "use" in section) else ("keys",))
        missing = [k for k in required if k not in section]
        if missing:
            _warn(where, f"missing {', '.join(missing)}; rule ignored")
            continue

        try:
            meter = section["meter"].strip()
        except Exception as exc:  # noqa: BLE001 - a config file must never be able to stop the program
            _warn(where, f"could not be read ({exc}); rule ignored")
            continue
        if known_meters is not None and meter not in known_meters:
            #Not a guess-and-continue: pointing a rule at the wrong meter would drink potions
            #based on a number measured somewhere else entirely.
            _warn(where, f"meter = {meter!r} is not a calibrated meter "
                         f"({', '.join(sorted(known_meters)) or 'none'}); rule ignored")
            continue

        use = section.get("use", "").strip()
        only_if_missing = section.get("only_if_missing", "").strip()
        if "keys" in section:
            keys = _keys(section["keys"], where)
            if not keys and not use:
                continue  #nothing to press and nothing to look up - the rule is inert
        elif use:
            keys = ()  #resolved from what the caller can see, every time it is needed
        else:
            continue

        try:
            rules.append(Rule(
                meter=meter,
                at_or_below=_number(section["below"], where, "below", 0.0),
                keys=keys,
                cooldown=_number(section.get("cooldown", "1.0"), where, "cooldown", 1.0),
                label=name,
                use=use,
                only_if_missing=only_if_missing,
            ))
        except Exception as exc:  # noqa: BLE001 - same: a bad rule is dropped, never fatal
            _warn(where, f"could not be read ({exc}); rule ignored")

    if not rules:
        #An empty or all-invalid rule list means the file said nothing usable. Falling back beats
        #running with no rules at all, which would look identical to "working fine" from outside.
        if parser.sections():
            _warn("", "no usable rules found; falling back to built-in defaults")
        rules = list(default_rules)

    return Config(*[settings[k] for k in _FIELDS], tuple(rules), str(path))


#--- Editing the file from a program (gui.py's Settings tab) ---------------------------------------
#WHY NOT configparser.write(): it throws away every comment, and the comments ARE this file - they
#are where a person learns what each number does. So editing is done on the text itself: find the
#"key = value" line inside its [section], replace only what follows the "=", and leave every other
#character of the file as it was. Only plain one-line "key = value" lines starting at column 0 are
#edited, which is every line this file has; anything else is left alone, never rewritten.
Entry = namedtuple("Entry", "section key value help line")
#section, key - where it lives;  value - the raw text after "=" (so "20%" stays "20%")
#help - the comment block directly above it, '#' stripped, for showing beside the field
#line - its 0-based line number in the text

Section = namedtuple("Section", "name help entries")

_SECTION_LINE = re.compile(r"^\[([^\]]+)\]\s*$")
_VALUE_LINE = re.compile(r"^([A-Za-z0-9_]+)(\s*[=:]\s*)(.*?)\s*$")


def _comment_text(line):
    stripped = line.strip()
    if stripped.startswith(("#", ";")):
        text = stripped[1:]
        return text[1:] if text.startswith(" ") else text
    return None


def parse_sections(text):
    """The file's [sections] and their settings, in file order, each with its help text.

    Help is the run of comment lines directly above a key (or a section header), stopping at a
    blank line or a rule of dashes - the shape this file is written in.
    """
    glossary = _glossary(text)
    sections = []
    pending = []  #comment lines seen since the last blank or setting line
    previous = None  #the entry on the line just above, for keys documented as a pair
    for number, line in enumerate(text.splitlines()):
        comment = _comment_text(line)
        if comment is not None:
            if set(comment.strip()) <= set("-=") and comment.strip():
                pending = []  #a "# -----" rule separates blocks; it is not help
            else:
                pending.append(comment)
            previous = None
            continue
        if not line.strip():
            pending = []
            previous = None
            continue
        header = _SECTION_LINE.match(line)
        if header:
            sections.append(Section(header.group(1).strip(), "\n".join(pending).strip(), []))
            pending = []
            previous = None
            continue
        match = _VALUE_LINE.match(line)
        if match and sections:
            key = match.group(1)
            help_text = "\n".join(pending).strip()
            if not help_text:
                help_text = glossary.get(key.lower(), "")
            if not help_text and previous is not None:
                help_text = previous.help  #"click_settle = ..." then "aim_settle = ..." under one comment
            previous = Entry(sections[-1].name, key, match.group(3), help_text, number)
            sections[-1].entries.append(previous)
        else:
            previous = None
        pending = []
    return sections


_GLOSSARY_ITEM = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s+-\s+(\S.*)$")


def _glossary(text):
    """{key: text} from definition lists written in comments, like the potion rules' block:

        #   meter    - which meter to watch. Must be a name from
        #              assets/meters.json ...
        #   below    - drink when ...

    Keys that appear in many sections (every rule has a "meter") are documented once like this
    rather than above every line, so this is where their help is. A later definition of the same
    word does not replace an earlier one.
    """
    found = {}
    key, parts, indent = None, [], 0
    for line in text.splitlines():
        comment = _comment_text(line)
        item = _GLOSSARY_ITEM.match(comment) if comment is not None else None
        if item:
            if key and key not in found:
                found[key] = " ".join(parts)
            key, parts, indent = item.group(2).lower(), [item.group(3).strip()], len(item.group(1))
            continue
        continues = (key is not None and comment is not None and comment.strip()
                     and len(comment) - len(comment.lstrip()) > indent)
        if continues:
            parts.append(comment.strip())
            continue
        if key and key not in found:
            found[key] = " ".join(parts)
        key, parts = None, []
    if key and key not in found:
        found[key] = " ".join(parts)
    return found


def apply_values(text, changes):
    """`text` with each {(section, key): new_value} written in. Everything else is untouched.

    Raises KeyError naming any (section, key) that is not in the text - a setting the caller
    thinks exists but does not is a bug to hear about, not something to append silently.
    Raises ValueError for a value with a line break in it, which would split into a second line.
    """
    lines = text.splitlines(keepends=True)
    remaining = dict(changes)
    for (section, key), value in changes.items():
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"[{section}] {key}: a value cannot contain a line break")
    for section in parse_sections(text):
        for entry in section.entries:
            wanted = (entry.section, entry.key)
            if wanted not in remaining:
                continue
            value = str(remaining.pop(wanted)).strip()
            original = lines[entry.line]
            ending = original[len(original.rstrip("\r\n")):]
            match = _VALUE_LINE.match(original.rstrip("\r\n"))
            separator = match.group(2).rstrip()
            lines[entry.line] = (f"{match.group(1)}{separator} {value}{ending}" if value
                                 else f"{match.group(1)}{separator}{ending}")
    if remaining:
        raise KeyError(", ".join(f"[{s}] {k}" for s, k in remaining))
    return "".join(lines)


def check(text, known_meters=None):
    """The warnings load() would print for this text, as a list of strings. [] means clean.

    Uses load() itself rather than a second copy of its rules, so the Settings tab can never
    accept something the program would then quietly replace with a default. load() reports by
    printing, so its output is captured here - which swaps sys.stdout for the duration, so this
    belongs in a program whose other threads are not printing at the same moment (the control
    panel, not the pipeline).
    """
    handle, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(text)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            load(path, known_meters=known_meters)
    finally:
        os.remove(path)
    return [line.strip() for line in captured.getvalue().splitlines() if line.strip()]


def save_text(path, text):
    """Writes `text` to `path` all at once: a crash or a full disk mid-write leaves the old file,
    never half of each. (Write a sibling file, then rename it over - a rename is atomic.)"""
    path = os.fspath(path)
    folder = os.path.dirname(os.path.abspath(path))
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".user_config.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise
