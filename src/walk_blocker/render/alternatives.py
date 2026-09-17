"""Render `docs/what-to-run-instead.md`, the users' page, from its template.

The refusal every user sees names this page by its installed path, so it is
the one document a refused user is certain to be able to read: it lives in
the payload beside the code, world-readable, and needs no URL a site has yet
to publish. It is rendered at build from the same `site.toml` the shim is
compiled from (ADR-0013), so the depth it quotes for a mount is the depth
the shim applies, and the figures it shows are the ones the survey read.

Markdown has its own splice hazards, smaller than `sh`'s but real: a `|` in
a value breaks the mount table, a backtick closes a code span, a newline
starts a new row. The schema's `sink_path` and `embedded_text` admit none of
them; `md_cell` is the check that does not depend on remembering which.
"""
import os
import re

from .. import paths
from .. import search_rules as R
from . import shim

TEMPLATE = "what-to-run-instead.md.in"
PAYLOAD_PATH = shim.GUIDE_REL

_MD_UNSAFE = re.compile(r"[|`\n\r]")


def template_path():
    return os.path.join(paths.node_dir(), "docs", TEMPLATE)


def _read_template():
    with open(template_path(), encoding="utf-8") as fh:
        return fh.read()


def placeholders():
    """Every `@@KEY@@` the page template uses, as a frozenset."""
    return frozenset(shim._PLACEHOLDER.findall(_read_template()))


def md_cell(value, what):
    """A value bound for a table cell or a code span: no `|`, no backtick,
    no line break."""
    value = str(value)
    if _MD_UNSAFE.search(value):
        raise shim.RenderError(
            "%s: %r contains a character Markdown would read as structure "
            "(a pipe, a backtick or a line break)" % (what, value))
    return value


def md_code(value, what):
    return "`%s`" % md_cell(value, what)


def human_bytes(n):
    """Twin of `survey.human_bytes`, pinned equal by a test; `src` cannot
    import the node script."""
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % n
        value /= 1024.0
    return str(n)


def human_count(n):
    """Twin of `survey.human_count`. Suffixed, never comma-grouped: a page
    the example site renders is a tracked file, and the IP gate reads a
    comma-grouped or nine-digit run as a site figure."""
    for unit, size in (("G", 10 ** 9), ("M", 10 ** 6), ("k", 10 ** 3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return str(n)


def _normalised(path):
    return path.rstrip("/") or "/"


def mount_rows(policy, site):
    """One table row per `[[filesystems.mounts]]` entry, sorted by path so a
    build is byte-stable (the same order `shim.mount_overrides` uses)."""
    recorded = {}
    for entry in shim._site_data(site)["filesystems"]["mounts"]:
        recorded[_normalised(entry["path"])] = entry
    rows = []
    for path in sorted(policy.mounts):
        cls, maxdepth = policy.mounts[path]
        if cls == "expensive":
            depth = str(maxdepth if maxdepth is not None else policy.maxdepth_allowed)
        else:
            depth = "not bounded (cheap)"
        entry = recorded.get(_normalised(path), {})
        facts = []
        if "inodes_used" in entry:
            facts.append("%s inodes in use" % human_count(entry["inodes_used"]))
        if "capacity_bytes" in entry:
            facts.append(human_bytes(entry["capacity_bytes"]))
        if facts:
            measured = "%s, as of %s" % (", ".join(facts),
                                         md_cell(entry["surveyed"], "surveyed"))
        elif "surveyed" in entry:
            measured = "surveyed %s, figures not recorded" % md_cell(entry["surveyed"], "surveyed")
        else:
            measured = "not surveyed"
        rows.append("| %s | %s | %s | %s |" % (md_code(path, "mount path"), cls, depth, measured))
    return "\n".join(rows)


def _optional_md_line(site, key, label, autolink=False):
    value = shim._site_data(site)["site"].get(key)
    if not value:
        return ""
    shown = md_cell(value, "site." + key)
    if autolink:
        shown = "<%s>" % shown
    return "%s: %s\n\n" % (label, shown)


def _extra_job_tools_lines(site):
    lines = []
    for tool in shim._site_data(site)["slurm"]["extra_job_tools"]:
        lines.append("    %s <jobid>\n" % md_cell(tool, "slurm.extra_job_tools"))
    return "".join(lines)


def substitutions(policy, site, version):
    """Every `@@KEY@@` -> text pair the page template needs."""
    data = shim._site_data(site)
    slurm = data["slurm"]
    prefix = data["install"]["prefix"]
    return {
        "@@DISPLAY_NAME@@": md_cell(shim._one_line(data["site"]["display_name"],
                                                    "site.display_name"),
                                    "site.display_name"),
        "@@MOUNT_TABLE@@": mount_rows(policy, site),
        "@@MAXDEPTH@@": str(policy.maxdepth_allowed),
        "@@UNSCOPED_DEPTH@@": str(policy.unscoped_depth),
        "@@REMOTE_FSTYPES@@": ", ".join(md_code(f, "filesystems.remote_fstypes")
                                        for f in policy.remote_fstypes),
        "@@PARTITION@@": md_cell(slurm["partition"], "slurm.partition"),
        "@@DEFAULT_TIME@@": md_cell(slurm["default_time"], "slurm.default_time"),
        "@@DEFAULT_MEM@@": md_cell(slurm["default_mem"], "slurm.default_mem"),
        "@@MIN_JOB_AGE@@": str(int(slurm["min_job_age_s"])),
        "@@EXTRA_JOB_TOOLS_LINES@@": _extra_job_tools_lines(site),
        "@@PREFIX@@": md_code(prefix, "install.prefix"),
        # The grimoire beside this page: one code span over the whole
        # path, so the rendered line is not half-quoted.
        "@@ALTERNATIVES_PATH@@": md_code(os.path.join(prefix, "docs", "alternatives.md"),
                                        "install.prefix"),
        "@@BIN_DIR@@": md_code(os.path.join(prefix, "bin"), "install.prefix"),
        "@@DOCS_LINE@@": _optional_md_line(site, "docs_url", "Documentation", autolink=True),
        "@@CONTACT_LINE@@": _optional_md_line(site, "contact", "Contact"),
        "@@ESCAPE@@": R.ESCAPE_HATCH,
        "@@VERSION@@": md_cell(version, "VERSION"),
    }


def render_page(policy, site, version):
    """The full text of `docs/what-to-run-instead.md`."""
    return shim._fill(_read_template(), substitutions(policy, site, version), TEMPLATE)
