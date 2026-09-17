"""Load, validate and complete `site.toml` (ADR-0013).

Three passes, in order: the JSON Schema (shape: every key, type, pattern and
range), a default fill so a `SiteConfig` always carries every key, then the
semantic checks the schema cannot express -- a path being canonical, one
bound not exceeding another, a regex compiling. Everything here runs on the
workstation; the node never sees this file or this module.
"""
import copy
import json
import os
import re
import shutil
import subprocess

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

import jsonschema
from jsonschema.exceptions import best_match

from . import paths

SINK_PATH_REF = "#/$defs/sink_path"

# Sink paths whose *only* legal values may be one component deep. Everything
# else must be at least two, so a typo cannot name `/usr` or `/etc` as an
# install target. Indices are erased before lookup (`mounts[3]` -> `mounts[]`).
SINGLE_COMPONENT_OK = frozenset([
    "install.staging_parent",       # `/run` is the canonical staging parent
    "install.tool_search_path[]",   # `/bin`, `/sbin` are legitimate entries
    "filesystems.mounts[].path",    # `/home`, `/scratch` are mount points
])

# systemd's named calendar shortcuts. Anything else needs a digit or a `*`
# somewhere; this is the portable floor under `systemd-analyze calendar`,
# which is consulted too when it is on PATH.
_CALENDAR_WORDS = frozenset([
    "minutely", "hourly", "daily", "monthly", "weekly", "yearly",
    "quarterly", "semiannually", "annually",
])


class ConfigError(ValueError):
    """A semantically invalid site value. `path` is dotted, with indices:
    `filesystems.mounts[1].maxdepth`."""

    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__("%s: %s" % (path, message))


# Everything `load_site()` can raise: a semantic fault, a shape fault, an
# unreadable file, a TOML syntax error (tomllib raises a ValueError subclass).
LOAD_ERRORS = (ConfigError, jsonschema.ValidationError, OSError, ValueError)


def describe_load_error(path, exc):
    """One line naming what stopped `load_site(path)`, for whichever of
    `LOAD_ERRORS` it was: the dotted key for a semantic fault, the JSON
    pointer for a shape fault, the OS or parser message otherwise. Both
    `validate` and `build` word a refusal this way, from here, so the two
    commands cannot describe the same site differently."""
    if isinstance(exc, ConfigError):
        return "%s: %s: %s" % (path, exc.path, exc.message)
    if isinstance(exc, jsonschema.ValidationError):
        pointer = "/" + "/".join(str(p) for p in exc.absolute_path)
        return "%s: %s: %s" % (path, pointer, exc.message)
    return "%s: %s" % (path, exc)


def schema_path():
    return paths.schema_file()


def load_schema():
    with open(schema_path(), encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(schema, node):
    """Follow a local `$ref`, keeping sibling keywords (`default`,
    `description`) from the referring node, which 2020-12 permits."""
    ref = node.get("$ref")
    if ref is None:
        return node
    if not ref.startswith("#/"):
        raise ValueError("only local $refs are supported: %r" % ref)
    target = schema
    for part in ref[2:].split("/"):
        target = target[part]
    merged = dict(_resolve(schema, target))
    for key, value in node.items():
        if key != "$ref":
            merged[key] = value
    return merged


def fill_defaults(schema, data, node=None):
    """Recursively add every `default` the schema declares and `data` lacks.
    An absent object with properties is created empty and filled; an absent
    property with no default is left absent. Mutates and returns `data`."""
    node = _resolve(schema, schema if node is None else node)
    if isinstance(data, dict) and "properties" in node:
        for name, sub in node["properties"].items():
            sub = _resolve(schema, sub)
            if name not in data:
                if "default" in sub:
                    data[name] = copy.deepcopy(sub["default"])
                elif sub.get("type") == "object" and "properties" in sub:
                    data[name] = {}
                else:
                    continue
            fill_defaults(schema, data[name], sub)
    elif isinstance(data, list) and "items" in node:
        for item in data:
            fill_defaults(schema, item, node["items"])
    return data


def _iter_refs(schema, data, want_ref, node=None, path=""):
    """Yield (dotted_path, value) for every value whose schema node refers
    to `want_ref`, so a rule about one `$def` is written once."""
    raw = schema if node is None else node
    if isinstance(raw, dict) and raw.get("$ref") == want_ref:
        yield path, data
        return
    node = _resolve(schema, raw)
    if isinstance(data, dict) and "properties" in node:
        for name, sub in node["properties"].items():
            if name in data:
                child = "%s.%s" % (path, name) if path else name
                for found in _iter_refs(schema, data[name], want_ref, sub, child):
                    yield found
    elif isinstance(data, list) and "items" in node:
        for i, item in enumerate(data):
            for found in _iter_refs(schema, item, want_ref, node["items"],
                                    "%s[%d]" % (path, i)):
                yield found


def _generic(path):
    return re.sub(r"\[\d+\]", "[]", path)


def validate_shape(schema, data):
    """Raise the single most relevant `jsonschema.ValidationError`."""
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    error = best_match(validator.iter_errors(data))
    if error is not None:
        raise error


def _iter_strings(data, path=""):
    if isinstance(data, str):
        yield path, data
    elif isinstance(data, dict):
        for key, value in data.items():
            for found in _iter_strings(value, "%s.%s" % (path, key) if path else key):
                yield found
    elif isinstance(data, list):
        for i, value in enumerate(data):
            for found in _iter_strings(value, "%s[%d]" % (path, i)):
                yield found


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_semantics(schema, data):
    """The rules the schema cannot state. Raise `ConfigError` on the first."""
    # Python's `re` lets `$` match before a trailing newline, so a schema
    # pattern anchored with `$` accepts "value\n". No site value may carry a
    # control character at all; every one is pasted into a line somewhere.
    for path, value in _iter_strings(data):
        hit = _CONTROL.search(value)
        if hit:
            raise ConfigError(path, "control character U+%04X is not allowed "
                              "in any site value" % ord(hit.group()))

    for path, value in _iter_refs(schema, data, SINK_PATH_REF):
        if os.path.normpath(value) != value:
            raise ConfigError(path, "path is not canonical (normalises to %r)"
                              % os.path.normpath(value))
        components = value.strip("/").split("/")
        if len(components) < 2 and _generic(path) not in SINGLE_COMPONENT_OK:
            raise ConfigError(path, "path needs at least two components; %r "
                              "names a top-level system directory" % value)

    fs = data["filesystems"]
    if fs["maxdepth_allowed"] > fs["depth_allowance_max"]:
        raise ConfigError("filesystems.maxdepth_allowed",
                          "%d exceeds depth_allowance_max %d"
                          % (fs["maxdepth_allowed"], fs["depth_allowance_max"]))
    if len(set(fs["remote_fstypes"])) != len(fs["remote_fstypes"]):
        raise ConfigError("filesystems.remote_fstypes", "duplicate entries")
    seen = {}
    for i, mount in enumerate(fs["mounts"]):
        where = "filesystems.mounts[%d]" % i
        if mount["path"] in seen:
            raise ConfigError(where + ".path", "duplicate of mounts[%d]"
                              % seen[mount["path"]])
        seen[mount["path"]] = i
        if "maxdepth" in mount:
            if mount["class"] != "expensive":
                raise ConfigError(where + ".maxdepth", "a depth ceiling only "
                                  "applies to an expensive mount; a cheap mount "
                                  "has no mount judgement to bound")
            if mount["maxdepth"] > fs["depth_allowance_max"]:
                raise ConfigError(where + ".maxdepth", "%d exceeds "
                                  "depth_allowance_max %d"
                                  % (mount["maxdepth"], fs["depth_allowance_max"]))
        # A measurement carries its date. The figures are shown to users as
        # "as of <date>"; a figure with no date would be shown as a fact about
        # today, which a year-old survey is not (ADR-0016, tier three).
        for figure in ("inodes_used", "capacity_bytes"):
            if figure in mount and "surveyed" not in mount:
                raise ConfigError(where + "." + figure,
                                  "a measurement carries its date: set "
                                  "surveyed = \"YYYY-MM-DD\" beside it")

    for i, origin in enumerate(data["reaper"]["origins"]):
        try:
            re.compile(origin["pattern"])
        except re.error as exc:
            raise ConfigError("reaper.origins[%d].pattern" % i,
                              "does not compile: %s" % exc)

    for shell, hook in data["hooks"].items():
        if "file" not in hook:
            # Required even when disabled: the uninstall path removes the
            # file a previous install wrote, so the installer must know it.
            raise ConfigError("hooks.%s.file" % shell,
                              "required (even when enabled = false, so uninstall "
                              "can remove a previously written hook)")

    timer = data["timer"]
    floor = timer_floor(data)
    if timer["timeout_start_sec"] < floor:
        raise ConfigError("timer.timeout_start_sec",
                          "%d is below the floor %s = relink_timeout_s + "
                          "relink_kill_after_s + settle_s + "
                          "max_kills * (kill_grace_s + 1)"
                          % (timer["timeout_start_sec"], floor))
    _check_calendar(timer["on_calendar"])


def timer_floor(data):
    r, t = data["reaper"], data["timer"]
    floor = (t["relink_timeout_s"] + t["relink_kill_after_s"] + r["settle_s"]
             + r["max_kills"] * (r["kill_grace_s"] + 1))
    return int(floor) if float(floor).is_integer() else floor


def _check_calendar(expr):
    words = expr.strip().lower()
    if words not in _CALENDAR_WORDS and not re.search(r"[0-9*]", expr):
        raise ConfigError("timer.on_calendar", "%r is not a systemd calendar "
                          "expression (no digit, no `*`, not a named shortcut)"
                          % expr)
    tool = shutil.which("systemd-analyze")
    if tool is None:
        return
    try:
        run = subprocess.run([tool, "calendar", expr], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return  # advisory: an unusable systemd-analyze is not a config fault
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        raise ConfigError("timer.on_calendar", "systemd-analyze rejects %r: %s"
                          % (expr, detail[-1] if detail else "exit %d" % run.returncode))


class SiteConfig:
    """A validated, default-complete site configuration."""

    def __init__(self, data, source=None):
        self.data = data
        self.source = source

    def lookup(self, dotted):
        """`filesystems.mounts[0].path` -> value. KeyError/IndexError on a
        missing key, like the containers underneath."""
        node = self.data
        for part in dotted.split("."):
            m = re.match(r"^([^\[]+)((?:\[\d+\])*)$", part)
            if not m:
                raise KeyError(dotted)
            node = node[m.group(1)]
            for idx in re.findall(r"\[(\d+)\]", m.group(2)):
                node = node[int(idx)]
        return node

    def policy(self):
        """The rule table's view of this site. Imported lazily so this
        module loads before the table does."""
        from . import search_rules
        fs = self.data["filesystems"]
        return search_rules.Policy(
            remote_fstypes=list(fs["remote_fstypes"]),
            remote_proxy=fs["remote_proxy"],
            maxdepth_allowed=fs["maxdepth_allowed"],
            unscoped_depth=fs["unscoped_depth"],
            depth_allowance_max=fs["depth_allowance_max"],
            mounts={m["path"]: (m["class"], m.get("maxdepth"))
                    for m in fs["mounts"]},
        )

    def derived(self):
        inst = self.data["install"]
        return {
            "audit_path": os.path.join(inst["spool_dir"], inst["audit_filename"]),
            "bin_dir": os.path.join(inst["prefix"], "bin"),
            "timer_floor_s": timer_floor(self.data),
        }


def from_dict(data, source=None, schema=None):
    """Validate, fill and check an already-parsed table."""
    schema = load_schema() if schema is None else schema
    validate_shape(schema, data)
    data = fill_defaults(schema, data)
    validate_shape(schema, data)  # the schema's own defaults are values too
    check_semantics(schema, data)
    return SiteConfig(data, source)


def load_site(path):
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return from_dict(data, source=path)
