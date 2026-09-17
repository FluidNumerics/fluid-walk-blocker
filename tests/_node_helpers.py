"""Shared mechanics for the two stamped-node harnesses.

`_deploy_helpers` and `_reaper_helpers` each load a `node/` Python file
the way the node receives it: STAMPED with a fictional site's values,
checked current and complete, written to a scratch directory and imported
from there under a unique module name. What differs between them is which
source, which values and which siblings the flat payload puts beside the
file; what is the same lives here, once. Nothing here is a site's value
(ADR-0014).
"""
import atexit
import copy
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from walk_blocker import config, stamp  # noqa: E402

EXAMPLE_SITE = os.path.join(ROOT, "examples", "site.example.toml")


def example_site():
    """The example site's data, default-complete, as a fresh copy."""
    return copy.deepcopy(config.load_site(EXAMPLE_SITE).data)


def read_source(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def stamped_text(source, values, required):
    """`source` with `values` stamped in through the same `stamp_text()` the
    build uses, checked current and complete against `required` before it
    is handed back."""
    text = stamp.stamp_text(read_source(source), values, "py")
    findings = stamp.check_text(text, values, "py", required)
    assert findings == [], findings
    return text


def scratch_dir(prefix):
    """A fresh directory, removed at interpreter exit."""
    directory = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, directory, True)
    return directory


_loaded = [0]


def load_module(path, name_prefix):
    """Import the file at `path` under a unique module name, so two stamped
    copies never collide in `sys.modules`."""
    _loaded[0] += 1
    spec = importlib.util.spec_from_file_location(
        "%s_%d" % (name_prefix, _loaded[0]), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
