"""The Hunter's Eye engine: the parts that know nothing about any particular game.

Everything in here takes a frame (or a region, or a key name) and answers a question about
it. Nothing in here knows what Diablo II is - the game-specific parts are main.py's threads
and the data files in assets/. That split is the whole architecture, and the import
direction is how it is enforced: main.py imports core, and core imports nothing back.

Note that the detector modules do not import EACH OTHER either, which is a deliberate rule
rather than an accident - see CLAUDE.md's "Detector independence". It is also what made
moving them into a package a safe change instead of a risky one.
"""
