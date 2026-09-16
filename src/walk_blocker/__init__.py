"""walk-blocker: config-driven guardrails against unbounded filesystem walks.

The workstation-side tool. Everything that ships to a node lives under
``node/`` and is stdlib-only Python 3.9 or POSIX sh (ADR-0015); this package
is the part that compiles ``site.toml`` into those artifacts (ADR-0013).
"""
from .paths import read_version

__version__ = read_version()
