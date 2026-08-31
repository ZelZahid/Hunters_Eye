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
from collections import namedtuple

#One rule: when `meter` reads at or below `at_or_below`, press one of `keys`, then leave THIS
#rule alone for `cooldown` seconds. `label` is the section name, used for logging and to key the
#per-rule cooldown. Deliberately the same shape main.py already used as a literal.
Rule = namedtuple("Rule", "meter at_or_below keys cooldown label")

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
}


class Config(namedtuple("Config",
                       "enabled min_gap snooze ignore_below use_password password game_name "
                       "rules source")):
    """Loaded settings. `source` is the file it came from, or None when defaults were used."""

    @property
    def loaded(self):
        return self.source is not None


_FIELDS = ("enabled", "min_gap", "snooze", "ignore_below", "use_password", "password", "game_name")


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
        for key in ("min_gap", "snooze", "ignore_below"):
            if key in section:
                try:
                    settings[key] = _number(section[key], f"[{SETTINGS_SECTION}]", key, DEFAULTS[key])
                except Exception as exc:  # noqa: BLE001
                    _warn(f"[{SETTINGS_SECTION}]", f"{key} could not be read ({exc}); "
                                                   f"using {DEFAULTS[key]}")
        for key in ("enabled", "use_password"):
            if key in section:
                try:
                    settings[key] = section.getboolean(key)
                except ValueError:
                    _warn(f"[{SETTINGS_SECTION}]", f"{key} = {section[key]!r} is not yes/no; "
                                                   f"using {DEFAULTS[key]}")
        for key in ("password", "game_name"):
            if key in section:
                #Taken verbatim - a password is not a number and not a name to be tidied up.
                settings[key] = section[key].strip()

    rules = []
    for name in parser.sections():
        if name == SETTINGS_SECTION:
            continue
        where = f"[{name}]"
        section = parser[name]
        missing = [k for k in ("meter", "below", "keys") if k not in section]
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

        keys = _keys(section["keys"], where)
        if not keys:
            continue

        try:
            rules.append(Rule(
                meter=meter,
                at_or_below=_number(section["below"], where, "below", 0.0),
                keys=keys,
                cooldown=_number(section.get("cooldown", "1.0"), where, "cooldown", 1.0),
                label=name,
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
