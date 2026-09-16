"""Stamp site values and VERSION into node files as literals (ADR-0013).

A node file that needs a site value or the version carries it on a *marker
line*: an assignment whose trailing comment names where the value comes from.
`walk-blocker build` rewrites the value on every marker line and leaves the
rest of the file alone, so the assignment can sit wherever it reads best
rather than at an offset the build would have to track.

    AUDIT_PATH = '/var/spool/example/audit.jsonl'  # GENERATED from site.toml:install.spool_dir
    __version__ = '1.2.3'  # GENERATED from VERSION
    TOOL_PATH='/usr/bin:/bin'  # GENERATED from site.toml:install.tool_search_path[:]

The marker grammar:

    <indent><identifier> = <value>  # GENERATED from <source>

where `<source>` is either `VERSION` or `site.toml:<dotted.key>`. A dotted
key is resolved through `SiteConfig.lookup`, so `filesystems.mounts[0].path`
addresses into an array. One transform exists for lists: a key ending in
`[:]` means "join the elements with `:`" -- the shape of a PATH -- and an
element containing `:` is refused, because the join could not be undone.
It is the only list convention; a list bound to `sh` without it fails the
build, since `sh` has no list literal. A second joiner, if one is ever
needed, is a second suffix here and a test, not a special case in a template.

The value is emitted per the target language, `kind`:

* `py`: `repr()`. Strings, ints, floats, bools, `None`, lists and tuples of
  those (emitted as tuples, so the constant is immutable) and dicts of str to
  any of those. Anything else is a build error.
* `sh`: a single-quoted literal. Strings and numbers are emitted as-is; bools
  as `true`/`false`, the TOML spelling. A value containing `'`, `\\` or a
  newline is refused -- the predecessor's version-shape guard, generalised:
  nothing may be able to end the quote it is emitted inside.

Two failure classes, both fatal, from the predecessor:

* **stale**: a marker line whose value differs from what the config says.
  `--check` reports it; a build rewrites it.
* **missing**: `CONSUMERS` says file X must carry source Y and no marker
  names Y. Louder than stale on purpose: a marker that is renamed or deleted
  stops being stamped and stops being checked at the same instant, which is
  the one failure this gate must never report as success.

Everything here runs on the workstation. The node reads the literal.
"""
import re

MARKER = "# GENERATED from"

# One line, three groups around the value: the assignment head, the value,
# and the comment tail whose fourth group is the source. `[ \t]` rather than
# `\s` so that in MULTILINE mode a blank line can never be swallowed into the
# head of the assignment below it; `(?!=)` so a comparison is not read as an
# assignment whose value starts with `=`.
MARKER_RE = re.compile(
    r"^([ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*=(?!=)[ \t]*)"
    r"(.*?)"
    r"([ \t]+# GENERATED from (site\.toml:[a-z0-9_.:\[\]]+|VERSION))$",
    re.MULTILINE)

SITE_PREFIX = "site.toml:"
JOIN_SUFFIX = "[:]"

# Payload-relative path -> the sources that file MUST carry a marker for.
# `search_rules.py` and `survey.py` are copied verbatim and the shim is a
# whole-file template. A file with a marker that is not in this table fails
# the build (see `build.py`), so a row cannot be forgotten silently.
CONSUMERS = {
    "deploy.py": (
        "VERSION",
        "site.toml:install.prefix", "site.toml:install.unit_dir",
        "site.toml:install.spool_dir", "site.toml:install.audit_filename",
        "site.toml:install.staging_parent",
        "site.toml:hooks.bash.file", "site.toml:hooks.bash.enabled",
        "site.toml:hooks.zsh.file", "site.toml:hooks.zsh.enabled",
        "site.toml:hooks.fish.file", "site.toml:hooks.fish.enabled",
        "site.toml:timer.on_calendar", "site.toml:timer.randomized_delay_sec",
        "site.toml:timer.accuracy_sec", "site.toml:timer.persistent",
        "site.toml:timer.timeout_start_sec", "site.toml:timer.relink_timeout_s",
        "site.toml:timer.relink_kill_after_s",
        "site.toml:trusted_binaries.timeout", "site.toml:trusted_binaries.sh",
        "site.toml:trusted_binaries.python3",
        "site.toml:site.display_name",
    ),
    "walk-job": (
        "VERSION",
        "site.toml:slurm.partition", "site.toml:slurm.qos", "site.toml:slurm.account",
        "site.toml:slurm.default_time", "site.toml:slurm.default_mem",
        "site.toml:slurm.sbatch_glob", "site.toml:slurm.nice",
        "site.toml:slurm.output_pattern", "site.toml:trusted_binaries.bfs",
    ),
    "reaper.py": (
        "VERSION",
        "site.toml:reaper.cgroup_root", "site.toml:reaper.slice_prefix",
        "site.toml:reaper.slice_suffix", "site.toml:reaper.origins",
        "site.toml:reaper.traversal_budget_s", "site.toml:reaper.fanout_n",
        "site.toml:reaper.io_stall_fraction", "site.toml:reaper.cpu_stall_fraction",
        "site.toml:reaper.kill_grace_s", "site.toml:reaper.max_kills",
        "site.toml:reaper.settle_s", "site.toml:reaper.audit_max_bytes",
        "site.toml:reaper.stream_filters",
        "site.toml:filesystems.remote_fstypes", "site.toml:filesystems.remote_proxy",
        "site.toml:filesystems.maxdepth_allowed", "site.toml:filesystems.unscoped_depth",
        "site.toml:filesystems.depth_allowance_max", "site.toml:filesystems.mounts",
        "site.toml:filesystems.mount_table",
    ),
    "shim/install.sh": (
        "VERSION",
        "site.toml:install.prefix", "site.toml:install.spool_dir",
        "site.toml:install.audit_filename", "site.toml:install.tool_search_path[:]",
        "site.toml:hooks.bash.file", "site.toml:hooks.bash.package",
        "site.toml:hooks.bash.enabled", "site.toml:hooks.bash.gate",
        "site.toml:hooks.zsh.file", "site.toml:hooks.zsh.package",
        "site.toml:hooks.zsh.enabled", "site.toml:hooks.zsh.gate",
        "site.toml:hooks.fish.file", "site.toml:hooks.fish.enabled",
        "site.toml:hooks.fish.gate",
        "site.toml:filesystems.mount_table", "site.toml:filesystems.remote_fstypes[:]",
        "site.toml:filesystems.remote_proxy", "site.toml:derived.mount_overrides",
        "site.toml:trusted_binaries.logger",
    ),
}

# `site.toml:derived.<name>` sources are computed from the config rather
# than read from it. Each is produced by the SAME function the shim
# renderer uses, so the two consumers of a derived value cannot drift.
DERIVED = ("mount_overrides",)

# Keys the schema leaves ABSENT when a site omits them. A consumer that
# stamps one reads the empty string as "not configured" (walk-job omits
# --qos / --account, and the bfs pin arm, on an empty value).
OPTIONAL_EMPTY = frozenset(["slurm.qos", "slurm.account", "trusted_binaries.bfs"])

KINDS = ("py", "sh")


class StampError(ValueError):
    """A value that cannot be emitted, or a marker that cannot be resolved."""


class Marker(object):
    """One marker line. `source` is the comment's operand as written,
    including any `[:]`; `key` is what `values` is asked for."""

    __slots__ = ("lineno", "name", "value", "source", "key", "join")

    def __init__(self, lineno, name, value, source):
        self.lineno = lineno
        self.name = name
        self.value = value
        self.source = source
        key, join = source, None
        if key.endswith(JOIN_SUFFIX):
            key, join = key[:-len(JOIN_SUFFIX)], ":"
        self.key = key
        self.join = join


def find_markers(text):
    """Every marker line in `text`, in file order."""
    found = []
    for m in MARKER_RE.finditer(text):
        lineno = text.count("\n", 0, m.start()) + 1
        name = m.group(1).split("=")[0].strip()
        found.append(Marker(lineno, name, m.group(2), m.group(4)))
    return found


class SiteValues(object):
    """The `values` mapping a build hands to `stamp_text`: `VERSION` and
    `site.toml:<dotted.key>` through `SiteConfig.lookup`. A plain dict keyed
    the same way works too, which is what the tests use."""

    def __init__(self, site, version):
        self.site = site
        self.version = version

    def _derived(self, name):
        return _derived_value(self.site, name)

    def __getitem__(self, key):
        if key == "VERSION":
            return self.version
        if key.startswith(SITE_PREFIX):
            dotted = key[len(SITE_PREFIX):]
            if dotted.startswith("derived."):
                return self._derived(dotted[len("derived."):])
            try:
                return self.site.lookup(dotted)
            except (KeyError, IndexError, TypeError):
                if dotted in OPTIONAL_EMPTY:
                    return ""
                raise KeyError(key)
        raise KeyError(key)


def _derived_value(site, name):
    if name == "mount_overrides":
        from .render.shim import mount_overrides
        return mount_overrides(site.policy())
    raise KeyError(SITE_PREFIX + "derived." + name)


def resolve(marker, values):
    """The Python value a marker stands for, after the `[:]` transform."""
    try:
        value = values[marker.key]
    except KeyError:
        raise StampError("line %d: %s: no such source %r"
                         % (marker.lineno, marker.name, marker.key))
    if marker.join is None:
        return value
    if not isinstance(value, (list, tuple)):
        raise StampError("line %d: %s: %s is not a list; `[:]` joins lists"
                         % (marker.lineno, marker.name, marker.key))
    parts = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise StampError("line %d: %s: %s holds a non-string or empty "
                             "element; only strings can be `:`-joined"
                             % (marker.lineno, marker.name, marker.key))
        if ":" in item:
            raise StampError("line %d: %s: element %r of %s contains ':', "
                             "the join separator" % (marker.lineno, marker.name,
                                                     item, marker.key))
        parts.append(item)
    return ":".join(parts)


_SCALARS = (str, int, float, bool, type(None))


def py_literal(value):
    """`repr()` over the closed set of shapes a stamped constant may take.
    Lists become tuples; the constant on the node is not meant to change."""
    if isinstance(value, _SCALARS):
        return repr(value)
    if isinstance(value, (list, tuple)):
        items = [py_literal(v) for v in value]
        if len(items) == 1:
            return "(%s,)" % items[0]
        return "(%s)" % ", ".join(items)
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            if not isinstance(k, str):
                raise StampError("dict key %r is not a string" % (k,))
            pairs.append("%s: %s" % (repr(k), py_literal(v)))
        return "{%s}" % ", ".join(pairs)
    raise StampError("cannot emit %s as a Python literal" % type(value).__name__)


def sh_literal(value):
    """A single-quoted `sh` literal, or a refusal. Inside single quotes only
    `'` itself is special, but a value carrying a backslash or a newline is
    refused too: the predecessor's version guard did, and a stamped constant
    that reads differently from how it is written is a review hazard."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = repr(value)
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        raise StampError("a list bound to sh needs the `[:]` join convention; "
                         "sh has no list literal")
    else:
        raise StampError("cannot emit %s as an sh literal" % type(value).__name__)
    for bad, what in (("'", "a single quote"), ("\\", "a backslash"),
                      ("\n", "a newline"), ("\r", "a carriage return")):
        if bad in text:
            raise StampError("value %r contains %s and cannot sit inside "
                             "single quotes" % (text, what))
    return "'%s'" % text


def literal(value, kind):
    if kind == "py":
        return py_literal(value)
    if kind == "sh":
        return sh_literal(value)
    raise ValueError("kind must be one of %s, not %r" % (KINDS, kind))


def _rendered(marker, values, kind):
    try:
        return literal(resolve(marker, values), kind)
    except StampError as exc:
        if str(exc).startswith("line "):
            raise
        raise StampError("line %d: %s: %s" % (marker.lineno, marker.name, exc))


def stamp_text(text, values, kind):
    """`text` with every marker line's value rewritten. Raises `StampError`
    on a source `values` cannot supply or a value `kind` cannot emit."""
    markers = iter(find_markers(text))

    def replace(m):
        marker = next(markers)
        return "%s%s%s" % (m.group(1), _rendered(marker, values, kind), m.group(3))

    return MARKER_RE.sub(replace, text)


def check_text(text, values, kind, required=()):
    """Stale and missing findings for one file, as human-readable lines; an
    empty list means the file is current. `required` lists the sources the
    file must carry a marker for (a `CONSUMERS` row)."""
    findings = []
    present = set()
    for marker in find_markers(text):
        present.add(marker.source)
        want = _rendered(marker, values, kind)
        if marker.value != want:
            findings.append("stale: line %d: %s = %s (%s says %s)"
                            % (marker.lineno, marker.name, marker.value,
                               marker.source, want))
    for source in required:
        if source not in present:
            findings.append("missing: no `%s %s` marker" % (MARKER, source))
    return findings
