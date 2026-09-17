"""Marker-line stamping: the grammar, the two emitters, the shape guard, and
the two failure classes (stale, missing) that the predecessor's version
stamper had and this generalisation keeps.

Every value here is a fixture; none is a site's (ADR-0014).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from walk_blocker import config, stamp  # noqa: E402
from conftest import EXAMPLE_SITE  # noqa: E402

V = {
    "VERSION": "1.2.3",
    "site.toml:install.prefix": "/opt/example/walk-blocker",
    "site.toml:install.tool_search_path": ["/usr/local/bin", "/usr/bin", "/bin"],
    "site.toml:reaper.fanout_n": 4,
    "site.toml:reaper.io_stall_fraction": 0.01,
    "site.toml:timer.persistent": False,
    "site.toml:filesystems.mounts[0].path": "/home",
    "site.toml:shim.unwrapped_tools": ["fzf", "sk"],
    "site.toml:reaper.mounts": {"/home": ("expensive", 4), "/opt/x": ("cheap", None)},
}


# ---------------------------------------------------------------- grammar --

@pytest.mark.parametrize("line", [
    "X = 1  # GENERATED from VERSION",
    "x=1 # GENERATED from VERSION",
    "    indented = 'a'  # GENERATED from VERSION",
    "\t_tab = 'a'\t# GENERATED from VERSION",
    "PREFIX = '/x'  # GENERATED from site.toml:install.prefix",
    "P = '/x'  # GENERATED from site.toml:filesystems.mounts[0].path",
    "P = '/a:/b'  # GENERATED from site.toml:install.tool_search_path[:]",
    "n2 = ()  # GENERATED from site.toml:a_b.c9",
])
def test_marker_grammar_accepts(line):
    found = stamp.find_markers(line + "\n")
    assert len(found) == 1
    assert found[0].lineno == 1


@pytest.mark.parametrize("line", [
    "X = 1  # generated from VERSION",             # case
    "X = 1  # GENERATED from version",             # case in the source
    "X = 1  # GENERATED from OTHER",               # unknown source class
    "X = 1  # GENERATED from site.toml:",          # empty key
    "X = 1  # GENERATED from site.toml:Install.Prefix",   # uppercase key
    "X = 1  # GENERATED from site.toml:a b",       # space in key
    "X = 1  # GENERATED from VERSION trailing",    # anything after the source
    "X = 1# GENERATED from VERSION",               # no space before the comment
    "2X = 1  # GENERATED from VERSION",            # not an identifier
    "X == 1  # GENERATED from VERSION",            # not an assignment
    "# GENERATED from VERSION",                    # no assignment at all
    "if X = 1  # GENERATED from VERSION",          # a keyword in front
])
def test_marker_grammar_rejects(line):
    assert stamp.find_markers(line + "\n") == []


def test_a_blank_line_is_never_swallowed_into_the_assignment_below_it():
    text = "\n\nX = 1  # GENERATED from VERSION\n"
    found = stamp.find_markers(text)
    assert [(m.lineno, m.name) for m in found] == [(3, "X")]


def test_join_suffix_is_parsed_off_the_key():
    m = stamp.find_markers("P = ''  # GENERATED from site.toml:install.tool_search_path[:]\n")[0]
    assert m.source == "site.toml:install.tool_search_path[:]"
    assert m.key == "site.toml:install.tool_search_path"
    assert m.join == ":"


# --------------------------------------------------------------- emission --

@pytest.mark.parametrize("value, want", [
    ("s", "'s'"),
    ("it's", '"it\'s"'),
    (7, "7"),
    (0.25, "0.25"),
    (True, "True"),
    (None, "None"),
    (["a", "b"], "('a', 'b')"),
    (["only"], "('only',)"),
    ((), "()"),
    ([1, [2, 3]], "(1, (2, 3))"),
    ({"/home": ("expensive", 4)}, "{'/home': ('expensive', 4)}"),
    ({"k": [None]}, "{'k': (None,)}"),
])
def test_py_literal(value, want):
    assert stamp.py_literal(value) == want
    assert eval(want) == _as_tuples(value)  # round-trips, lists as tuples


def _as_tuples(value):
    if isinstance(value, (list, tuple)):
        return tuple(_as_tuples(v) for v in value)
    if isinstance(value, dict):
        return {k: _as_tuples(v) for k, v in value.items()}
    return value


@pytest.mark.parametrize("value", [object(), {1: "x"}, {"k": object()}, b"bytes", {"a"}])
def test_py_literal_refuses_shapes_outside_the_closed_set(value):
    with pytest.raises(stamp.StampError):
        stamp.py_literal(value)


@pytest.mark.parametrize("value, want", [
    ("/opt/x", "'/opt/x'"),
    ("a b $c `d` \"e\" ; & |", "'a b $c `d` \"e\" ; & |'"),   # inert inside single quotes
    (7, "'7'"),
    (0.5, "'0.5'"),
    (True, "'true'"),
    (False, "'false'"),
])
def test_sh_literal(value, want):
    assert stamp.sh_literal(value) == want


@pytest.mark.parametrize("value", ["it's", "back\\slash", "two\nlines", "cr\rhere",
                                   "1.2.3'; rm -rf /; echo '"])
def test_sh_shape_guard(value):
    with pytest.raises(stamp.StampError):
        stamp.sh_literal(value)


@pytest.mark.parametrize("value", [["a", "b"], ("a",), {"k": "v"}, None, object()])
def test_sh_literal_refuses_compound_values(value):
    with pytest.raises(stamp.StampError):
        stamp.sh_literal(value)


def test_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        stamp.literal("x", "toml")


# ---------------------------------------------------------------- stamping --

def test_stamp_text_rewrites_every_marker_and_nothing_else():
    text = ("#!/bin/sh\n"
            "VERSION='0.0.0'  # GENERATED from VERSION\n"
            "unrelated='keep me'\n"
            "PREFIX='/wrong'  # GENERATED from site.toml:install.prefix\n"
            "  TOOL_PATH=''  # GENERATED from site.toml:install.tool_search_path[:]\n"
            "FANOUT='0'  # GENERATED from site.toml:reaper.fanout_n\n"
            "PERSIST='maybe'  # GENERATED from site.toml:timer.persistent\n")
    out = stamp.stamp_text(text, V, "sh")
    assert out == ("#!/bin/sh\n"
                   "VERSION='1.2.3'  # GENERATED from VERSION\n"
                   "unrelated='keep me'\n"
                   "PREFIX='/opt/example/walk-blocker'  # GENERATED from site.toml:install.prefix\n"
                   "  TOOL_PATH='/usr/local/bin:/usr/bin:/bin'  # GENERATED from site.toml:install.tool_search_path[:]\n"  # noqa: E501
                   "FANOUT='4'  # GENERATED from site.toml:reaper.fanout_n\n"
                   "PERSIST='false'  # GENERATED from site.toml:timer.persistent\n")
    assert stamp.check_text(out, V, "sh") == []


def test_stamp_text_py_kind():
    text = ('__version__ = "0"  # GENERATED from VERSION\n'
            "UNWRAPPED = None  # GENERATED from site.toml:shim.unwrapped_tools\n"
            "MOUNTS = {}  # GENERATED from site.toml:reaper.mounts\n"
            "FRACTION = 0  # GENERATED from site.toml:reaper.io_stall_fraction\n"
            "JOINED = ''  # GENERATED from site.toml:install.tool_search_path[:]\n")
    out = stamp.stamp_text(text, V, "py")
    assert "__version__ = '1.2.3'  # GENERATED from VERSION" in out
    assert "UNWRAPPED = ('fzf', 'sk')  # GENERATED" in out
    assert "MOUNTS = {'/home': ('expensive', 4), '/opt/x': ('cheap', None)}  # GENERATED" in out
    assert "FRACTION = 0.01  # GENERATED" in out
    assert "JOINED = '/usr/local/bin:/usr/bin:/bin'  # GENERATED" in out
    ns = {}
    exec(out, ns)  # the stamped text is valid Python that means what it says
    assert ns["__version__"] == "1.2.3" and ns["MOUNTS"]["/home"] == ("expensive", 4)


def test_stamp_is_idempotent():
    text = "X = 'old'  # GENERATED from VERSION\n"
    once = stamp.stamp_text(text, V, "sh")
    assert stamp.stamp_text(once, V, "sh") == once


def test_a_list_bound_to_sh_needs_the_join_convention():
    with pytest.raises(stamp.StampError, match=r"\[:\]"):
        stamp.stamp_text("T=''  # GENERATED from site.toml:shim.unwrapped_tools\n", V, "sh")


def test_join_refuses_an_element_carrying_the_separator():
    values = dict(V)
    values["site.toml:install.tool_search_path"] = ["/usr/bin", "/a:b"]
    with pytest.raises(stamp.StampError, match="contains ':'"):
        stamp.stamp_text("T=''  # GENERATED from site.toml:install.tool_search_path[:]\n",
                         values, "sh")


def test_join_refuses_a_non_list():
    with pytest.raises(stamp.StampError, match="not a list"):
        stamp.stamp_text("T=''  # GENERATED from site.toml:install.prefix[:]\n", V, "sh")


def test_an_unknown_source_is_an_error_not_a_skip():
    with pytest.raises(stamp.StampError, match="no such source"):
        stamp.stamp_text("T=''  # GENERATED from site.toml:install.nope\n", V, "sh")


def test_the_shape_guard_fails_the_stamp_with_the_line_named():
    values = dict(V)
    values["VERSION"] = "1.2.3'"
    with pytest.raises(stamp.StampError, match=r"line 2: VERSION") as info:
        stamp.stamp_text("#!/bin/sh\nVERSION='x'  # GENERATED from VERSION\n", values, "sh")
    assert "single quote" in str(info.value)


# ------------------------------------------------------- stale and missing --

def test_check_reports_stale_with_both_values():
    text = "PREFIX='/wrong'  # GENERATED from site.toml:install.prefix\n"
    findings = stamp.check_text(text, V, "sh")
    assert len(findings) == 1
    assert findings[0].startswith("stale: line 1: PREFIX = '/wrong'")
    assert "'/opt/example/walk-blocker'" in findings[0]


def test_check_is_clean_after_a_stamp():
    text = "PREFIX='/wrong'  # GENERATED from site.toml:install.prefix\n"
    assert stamp.check_text(stamp.stamp_text(text, V, "sh"), V, "sh") == []


def test_check_reports_missing_required_sources():
    text = "PREFIX='/opt/example/walk-blocker'  # GENERATED from site.toml:install.prefix\n"
    findings = stamp.check_text(text, V, "sh",
                                required=["site.toml:install.prefix", "VERSION"])
    assert findings == ["missing: no `# GENERATED from VERSION` marker"]


def test_a_renamed_marker_is_reported_missing_not_silently_dropped():
    """The failure the predecessor called out: rename or mangle the marker
    and the value stops being stamped and stops being checked at once."""
    good = "VERSION='1.2.3'  # GENERATED from VERSION\n"
    assert stamp.check_text(good, V, "sh", required=["VERSION"]) == []
    for mangled in ("VERSION='1.2.3'  # generated from VERSION\n",
                    "VERSION='1.2.3'  # GENERATED from RELEASE\n",
                    "VERSION='1.2.3'\n"):
        findings = stamp.check_text(mangled, V, "sh", required=["VERSION"])
        assert findings and findings[0].startswith("missing:"), mangled


def test_missing_with_the_join_suffix_is_matched_on_the_source_as_written():
    text = "T='/usr/bin'  # GENERATED from site.toml:install.tool_search_path\n"
    findings = stamp.check_text(text, V, "py",
                                required=["site.toml:install.tool_search_path[:]"])
    assert any(f.startswith("missing:") for f in findings)


# --------------------------------------------------------------- values --

def test_site_values_resolve_through_the_config(tmp_path):
    site = config.load_site(os.path.join(ROOT, "examples", "site.example.toml"))
    values = stamp.SiteValues(site, "9.9.9")
    assert values["VERSION"] == "9.9.9"
    assert values["site.toml:install.prefix"] == site.lookup("install.prefix")
    assert values["site.toml:filesystems.mounts[0].path"] == site.lookup("filesystems.mounts[0].path")
    for bad in ("site.toml:install.nope", "site.toml:filesystems.mounts[99].path",
                "OTHER", "site.toml:install.prefix.deeper"):
        with pytest.raises(KeyError):
            values[bad]


def test_consumers_rows_are_well_formed():
    """Every row names a payload path and sources in the marker grammar, so
    a later milestone adding one cannot typo it silently."""
    for rel, sources in stamp.CONSUMERS.items():
        assert not rel.startswith("/") and ".." not in rel
        for source in sources:
            line = "X = 0  # GENERATED from %s\n" % source
            assert stamp.find_markers(line), (rel, source)


def test_the_mounts_value_carries_only_what_a_judgement_reads():
    """The example site records `inodes`, `capacity_bytes` and `surveyed`
    on `/home`; the reaper's stamped rows must not: nothing on the node
    carries a figure it does not read, and a capacity in bytes is exactly
    the kind of literal the IP gate hunts."""
    site = config.load_site(EXAMPLE_SITE)
    assert any("inodes" in m for m in site.lookup("filesystems.mounts")), \
        "the example no longer exercises the projection"
    rows = stamp.SiteValues(site, "0.0.0")["site.toml:filesystems.mounts"]
    assert rows and all(set(r) <= set(stamp.JUDGEMENT_MOUNT_KEYS) for r in rows), rows
    assert {r["path"] for r in rows} == {m["path"] for m in site.lookup("filesystems.mounts")}
    assert stamp.SiteValues(site, "0.0.0")["site.toml:filesystems.mounts[0].path"] == "/home"
