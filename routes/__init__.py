"""Game-specific scripted sequences, built on the engine in core/.

The import direction is the same as main.py's: routes import core, core never imports routes.
A route also never imports main - main.py hands each route the few things it needs from the
running pipeline (a frame grabber, the safety guards) as plain callables, so a route can be tested
against saved frames without starting the pipeline at all.
"""
