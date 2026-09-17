"""Load `node/deploy.py` the way the node receives it: STAMPED.

The in-tree file carries sentinel values on its marker lines; the payload
carries a site's. The suite tests the second thing, so this helper stamps
the source with a FICTIONAL site's values -- the example site's `[install]`,
`[hooks]`, `[timer]`, `[trusted_binaries]` and `[site]` blocks -- through
the same `stamp_text()` the build uses, writes the result to a scratch
directory as `deploy.py` (the payload is flat and `REPO` is "the directory
this file is in"), and imports it from there under a unique module name.

The stamped locations are the example's literals, not tmp paths: the
autouse `_test_paths` fixture in `test_deploy.py` moves the module's
`DEFAULT_*` constants into each test's tmp tree, and the constants are the
seam (ADR-0005). Tests that `monkeypatch.undo()` to read the compiled
literals therefore see the fictional site's, which is what the shape
assertions are about. Nothing here is a site's value (ADR-0014).
"""
import os

import _node_helpers as _node
from _node_helpers import example_site
from walk_blocker import __version__, paths, stamp

# Re-exported: test_deploy imports both from here.
ROOT = _node.ROOT
EXAMPLE_SITE = _node.EXAMPLE_SITE
DEPLOY_SOURCE = os.path.join(paths.node_dir(), "deploy.py")

# Every source `deploy.py` must carry a marker for: the
# `CONSUMERS["deploy.py"]` row, spelled out here so the suite pins the set
# independently of the table.
REQUIRED_SOURCES = (
    "VERSION",
    "site.toml:install.prefix",
    "site.toml:install.unit_dir",
    "site.toml:install.spool_dir",
    "site.toml:install.audit_filename",
    "site.toml:install.staging_parent",
    "site.toml:hooks.bash.file",
    "site.toml:hooks.bash.enabled",
    "site.toml:hooks.zsh.file",
    "site.toml:hooks.zsh.enabled",
    "site.toml:hooks.fish.file",
    "site.toml:hooks.fish.enabled",
    "site.toml:timer.on_calendar",
    "site.toml:timer.randomized_delay_sec",
    "site.toml:timer.accuracy_sec",
    "site.toml:timer.persistent",
    "site.toml:timer.timeout_start_sec",
    "site.toml:timer.relink_timeout_s",
    "site.toml:timer.relink_kill_after_s",
    "site.toml:trusted_binaries.timeout",
    "site.toml:trusted_binaries.sh",
    "site.toml:trusted_binaries.python3",
    "site.toml:site.display_name",
)


def site_values(**overrides):
    """The `values` mapping for `stamp_text`: VERSION plus every key of the
    fictional site that `deploy.py` reads. `overrides` are keyed the way the
    mapping is (`"site.toml:install.prefix"`), or by the bare dotted key."""
    data = example_site()
    values = {"VERSION": __version__}
    for section in ("install", "timer", "trusted_binaries", "site"):
        for key, value in data[section].items():
            values["%s%s.%s" % (stamp.SITE_PREFIX, section, key)] = value
    for shell in ("bash", "zsh", "fish"):
        for key, value in data["hooks"][shell].items():
            values["%shooks.%s.%s" % (stamp.SITE_PREFIX, shell, key)] = value
    for key, value in overrides.items():
        full = key if key == "VERSION" or key.startswith(stamp.SITE_PREFIX) \
            else stamp.SITE_PREFIX + key
        values[full] = value
    return values


def source_text():
    return _node.read_source(DEPLOY_SOURCE)


def stamped_text(values):
    """`node/deploy.py` with `values` stamped in, checked current and
    complete before it is handed back."""
    return _node.stamped_text(DEPLOY_SOURCE, values, REQUIRED_SOURCES)


def write_stamped_deploy(values, directory):
    """The stamped file at `<directory>/deploy.py`, mode 0755 as the build
    emits it. Returns its path."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "deploy.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(stamped_text(values))
    os.chmod(path, 0o755)
    return path


def load_stamped_deploy(values, directory=None):
    """Write the stamped deployer into `directory` (a fresh scratch directory
    by default, removed at exit) and import it from there under a unique
    module name, so `REPO` is that directory."""
    if directory is None:
        directory = _node.scratch_dir("walk-blocker-deploy-")
    return _node.load_module(write_stamped_deploy(values, directory),
                             "deploy_under_test")
