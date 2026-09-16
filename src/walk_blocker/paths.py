"""Where this package finds its own files, in a checkout or installed."""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))

# In a checkout the package sits at src/walk_blocker/ and the repo root is two
# levels up. In an installed wheel the same files are force-included beside
# the package (see pyproject.toml), so each location is probed in turn.
_CHECKOUT_ROOT = os.path.dirname(os.path.dirname(_HERE))

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _first_existing(*candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("none of: %s" % ", ".join(candidates))


def version_file():
    return _first_existing(
        os.path.join(_CHECKOUT_ROOT, "VERSION"),
        os.path.join(_HERE, "_VERSION"),
    )


def read_version():
    """The one hand-written version, validated to the shape every generated
    artifact will embed inside single quotes. A version that is not
    MAJOR.MINOR.PATCH is refused, not passed through.
    """
    with open(version_file(), encoding="ascii") as fh:
        raw = fh.read().strip()
    if not VERSION_RE.match(raw):
        raise ValueError("VERSION is not MAJOR.MINOR.PATCH: %r" % raw)
    return raw
