"""Load `node/reaper.py` the way the node receives it: STAMPED.

The in-tree file carries placeholder values on its marker lines; the payload
carries a site's. The suite tests the second thing, so this helper stamps the
source with a FICTIONAL site's values (the example site's `[reaper]` block
plus the fixture mount policy from `conftest.make_policy`), writes the result
to a scratch directory beside a copy of `search_rules.py` -- the payload is
flat, and the reaper imports the rule table as a sibling -- and imports it
from there. Nothing here is a site's value (ADR-0014).
"""
import copy
import os
import shutil

import _node_helpers as _node
from walk_blocker import __version__, paths, stamp

REAPER_SOURCE = os.path.join(paths.node_dir(), "reaper.py")
RULES_SOURCE = paths.rules_file()

# Every source `reaper.py` must carry a marker for: the `CONSUMERS["reaper.py"]`
# row, spelled out here so the suite pins the set independently of the table.
REQUIRED_SOURCES = (
    "VERSION",
    "site.toml:reaper.cgroup_root",
    "site.toml:reaper.slice_prefix",
    "site.toml:reaper.slice_suffix",
    "site.toml:reaper.origins",
    "site.toml:reaper.traversal_budget_s",
    "site.toml:reaper.fanout_n",
    "site.toml:reaper.io_stall_fraction",
    "site.toml:reaper.cpu_stall_fraction",
    "site.toml:reaper.kill_grace_s",
    "site.toml:reaper.max_kills",
    "site.toml:reaper.settle_s",
    "site.toml:reaper.audit_max_bytes",
    "site.toml:reaper.stream_filters",
    "site.toml:filesystems.remote_fstypes",
    "site.toml:filesystems.remote_proxy",
    "site.toml:filesystems.maxdepth_allowed",
    "site.toml:filesystems.unscoped_depth",
    "site.toml:filesystems.depth_allowance_max",
    "site.toml:filesystems.mounts",
    "site.toml:filesystems.mount_table",
)

# The fixture policy, as `[filesystems]` would spell it: the same values
# `conftest.make_policy` builds, so the reaper and the rule-table tests judge
# the fixture mount table identically.
FIXTURE_FILESYSTEMS = {
    "remote_fstypes": ["wekafs", "nfs4"],
    "remote_proxy": True,
    "maxdepth_allowed": 2,
    "unscoped_depth": 2,
    "depth_allowance_max": 8,
    "mounts": [{"path": "/home", "class": "expensive", "maxdepth": 4}],
}


def site_values(**overrides):
    """The `values` mapping for `stamp_text`: VERSION plus every `[reaper]`
    and `[filesystems]` key of the fictional site. `overrides` are keyed the
    way the mapping is (`"site.toml:reaper.origins"`)."""
    data = _node.example_site()
    data["filesystems"].update(copy.deepcopy(FIXTURE_FILESYSTEMS))
    values = {"VERSION": __version__}
    for section in ("reaper", "filesystems"):
        for key, value in data[section].items():
            values["%s%s.%s" % (stamp.SITE_PREFIX, section, key)] = value
    values.update(overrides)
    return values


def source_text():
    return _node.read_source(REAPER_SOURCE)


def stamped_text(values):
    """`node/reaper.py` with `values` stamped in, checked current and
    complete before it is handed back."""
    return _node.stamped_text(REAPER_SOURCE, values, REQUIRED_SOURCES)


def load_stamped_reaper(values, directory=None):
    """Write the stamped reaper and a copy of the rule table into
    `directory` (a fresh scratch directory by default, removed at exit) and
    import the reaper from there under a unique module name."""
    if directory is None:
        directory = _node.scratch_dir("walk-blocker-reaper-")
    path = os.path.join(directory, "reaper.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(stamped_text(values))
    shutil.copy(RULES_SOURCE, os.path.join(directory, "search_rules.py"))
    return _node.load_module(path, "reaper_under_test")
