"""Renderers: `site.toml` plus the rule table, compiled into node artifacts.

Each module here turns validated site configuration and the rule table into
one kind of artifact text (ADR-0013). `shim` renders the whole-file `@@KEY@@`
templates under `node/shim/`; nothing rendered here is read back by the node
as configuration -- every value lands as a literal.
"""
