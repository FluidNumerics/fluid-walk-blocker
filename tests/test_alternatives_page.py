"""`docs/what-to-run-instead.md`: rendered per site, checked like the shim.

The refusal names this page by its installed path, so what it says about a
mount must be what the shim does about that mount, and what it shows as a
measurement must be what the site recorded -- rendered from the same
`site.toml`, never retyped.
"""
import importlib.util
import os
import re

import pytest

import conftest
from conftest import EXAMPLE_SITE, make_policy
from walk_blocker import __version__, build, config, paths
from walk_blocker.render import alternatives, shim


@pytest.fixture(scope="module")
def example():
    site = config.load_site(EXAMPLE_SITE)
    return site, site.policy()


@pytest.fixture(scope="module")
def page(example):
    site, policy = example
    return alternatives.render_page(policy, site, __version__)


def test_the_page_lands_where_the_footer_says(example):
    assert alternatives.PAYLOAD_PATH == shim.GUIDE_REL == "docs/what-to-run-instead.md"
    site, _policy = example
    files = build.render_payload(site, b"", __version__)
    assert alternatives.PAYLOAD_PATH in files
    assert files[alternatives.PAYLOAD_PATH][1] == build.MODE_FILE


def test_every_mount_has_a_row_with_its_depth_and_measurement(page, example):
    site, policy = example
    for path, (cls, maxdepth) in policy.mounts.items():
        row = [ln for ln in page.splitlines() if ln.startswith("| `%s` |" % path)]
        assert len(row) == 1, (path, row)
        cells = [c.strip() for c in row[0].strip("|").split("|")]
        assert cells[1] == cls
        if cls == "expensive":
            assert cells[2] == str(maxdepth if maxdepth is not None else policy.maxdepth_allowed)
        else:
            assert cells[2] == "not bounded (cheap)"
    # The example records fictional figures on /home and none on the cheap export.
    home = [ln for ln in page.splitlines() if ln.startswith("| `/home` |")][0]
    assert "24.4M inodes in use" in home and "81.9 TiB" in home and "as of 2026-01-15" in home
    tools = [ln for ln in page.splitlines() if ln.startswith("| `/opt/site-tools` |")][0]
    assert tools.endswith("| not surveyed |")


def test_the_page_carries_the_sites_own_values(page):
    for expected in ("Example HPC login node", "`datamover`", "1:00:00", "8G",
                     "`/usr/local/lib/walk-blocker/bin`",
                     "`/usr/local/lib/walk-blocker/docs/alternatives.md`",
                     "<https://docs.example.org/hpc/walk-blocker>", "Contact: hpc-help@example.org",
                     "WALK_BLOCKER_UNSCOPED=1", "walk-job", "`lustre`", "`wekafs`",
                     "    job-report <jobid>", "for 300 seconds",
                     "walk-blocker %s" % __version__):
        assert expected in page, expected
    assert "@@" not in page


def test_a_bare_site_prints_no_documentation_or_contact_line(example):
    site, policy = example
    bare = conftest.make_site("/proc/mounts")
    del bare["site"]["docs_url"]
    del bare["site"]["contact"]
    text = alternatives.render_page(policy, bare, __version__)
    assert "Documentation:" not in text and "Contact:" not in text
    assert "@@" not in text


def test_the_template_uses_exactly_the_placeholders_the_renderer_fills(example):
    site, policy = example
    filled = set(alternatives.substitutions(policy, site, __version__))
    assert filled == alternatives.placeholders()
    assert alternatives.placeholders() == {
        "@@DISPLAY_NAME@@", "@@MOUNT_TABLE@@", "@@MAXDEPTH@@", "@@UNSCOPED_DEPTH@@",
        "@@REMOTE_FSTYPES@@", "@@PARTITION@@", "@@DEFAULT_TIME@@", "@@DEFAULT_MEM@@",
        "@@MIN_JOB_AGE@@", "@@EXTRA_JOB_TOOLS_LINES@@", "@@BIN_DIR@@",
        "@@ALTERNATIVES_PATH@@",
        "@@DOCS_LINE@@", "@@CONTACT_LINE@@", "@@ESCAPE@@", "@@VERSION@@",
    }


@pytest.mark.parametrize("bad", ["a|b", "a`b", "a\nb", "a\rb"])
def test_a_value_markdown_would_read_as_structure_fails_the_render(bad):
    with pytest.raises(shim.RenderError):
        alternatives.md_cell(bad, "test")


def test_a_hand_built_site_with_a_pipe_in_a_path_is_refused(example):
    """The schema forbids it first; the renderer does not depend on that."""
    site, _policy = example
    policy = make_policy(mounts={"/a|b": ("expensive", None)})
    with pytest.raises(shim.RenderError):
        alternatives.render_page(policy, site, __version__)


def test_the_figure_formatters_match_the_surveys():
    spec = importlib.util.spec_from_file_location(
        "survey_for_page_test", os.path.join(paths.node_dir(), "survey.py"))
    survey = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(survey)
    for n in (1, 999, 1000, 1536, 24_400_000, 536_900_000, 12_158_200_000_000, 2 ** 62):
        assert alternatives.human_count(n) == survey.human_count(n), n
        assert alternatives.human_bytes(n) == survey.human_bytes(n), n
    # Never a comma group or a nine-digit run: the rendered example is a
    # tracked file and the IP gate reads those shapes as site figures.
    for n in (24_400_000, 12_158_200_000_000):
        assert "," not in alternatives.human_count(n)
        assert not any(len(run) >= 9 for run in
                       __import__("re").findall(r"\d+", alternatives.human_count(n)))


# --------------------------------------------------------------------------
# the page is wrapped AFTER substitution (#38)
# --------------------------------------------------------------------------

# The test classifies lines on its own rather than reusing the renderer's
# classifier: a bug in that classifier is one of the things this is for. It
# deliberately has NO ordered-list rule, so a paragraph whose line begins with
# a substituted number is prose here and must be wrapped as prose.
_STRUCTURE = re.compile(r"^ {0,3}(?:\||#|>|[-*+]\s)")


def _classified(text):
    """(kind, line) for every line: prose, code, structure or blank."""
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            yield "code", line
        elif fenced or line.startswith(("    ", "\t")):
            yield "code", line
        elif not line.strip():
            yield "blank", line
        elif _STRUCTURE.match(line):
            yield "structure", line
        else:
            yield "prose", line


def _prose_runs(text):
    """Consecutive prose lines, grouped into the paragraphs they form."""
    runs, current = [], []
    for kind, line in _classified(text):
        if kind == "prose":
            current.append(line)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _long_site_and_policy():
    """Values chosen to overflow: sixteen filesystem types spliced inline."""
    site = conftest.make_site("/proc/mounts")
    site["site"]["display_name"] = "the shared login node of a long-named cluster"
    site["site"]["docs_url"] = (
        "https://docs.example.org/very/long/path/to/the/hpc/handbook/walk-blocker")
    policy = make_policy(remote_fstypes=(
        "lustre", "wekafs", "beegfs", "gpfs", "ceph", "nfs4", "nfs", "cifs",
        "smb3", "glusterfs", "panfs", "fuse.sshfs", "9p", "sshfs", "s3fs", "daos"))
    return site, policy


def _short_site_and_policy():
    """And the other extreme: one short type, no optional lines."""
    site = conftest.make_site("/proc/mounts")
    site["site"]["display_name"] = "a node"
    del site["site"]["docs_url"]
    del site["site"]["contact"]
    return site, make_policy(remote_fstypes=("nfs",))


@pytest.mark.parametrize("build_site", [_long_site_and_policy, _short_site_and_policy],
                         ids=["long-values", "short-values"])
def test_every_prose_line_is_within_the_width_however_long_the_values_are(build_site):
    """The template is hard-wrapped by hand and the substitutions land inside
    those lines, so before this pass a site listing sixteen filesystem types
    produced a line of a few hundred columns. Rendered as Markdown that is
    invisible; read with `cat` on the node, which is where this page lives, it
    is not."""
    site, policy = build_site()
    text = alternatives.render_page(policy, site, __version__)
    for line in [l for run in _prose_runs(text) for l in run]:
        if len(line) <= alternatives.WRAP_WIDTH:
            continue
        # The one permitted overflow: a single word that cannot be broken,
        # such as a URL, which a reader may need to copy whole.
        assert len(line.split()) == 1, "%d cols: %r" % (len(line), line)


@pytest.mark.parametrize("build_site", [_long_site_and_policy, _short_site_and_policy],
                         ids=["long-values", "short-values"])
def test_no_paragraph_is_left_ragged(build_site):
    """The other half of #38, and the half a width check alone would pass: a
    SHORT value leaves a short line. Every line of a paragraph except its last
    must be full, meaning the first word of the next line could not have been
    added without exceeding the width. That is the definition of a greedy
    fill, and it fails on text that was wrapped before substitution."""
    site, policy = build_site()
    text = alternatives.render_page(policy, site, __version__)
    for run in _prose_runs(text):
        for line, following in zip(run, run[1:]):
            nxt = following.split()[0]
            assert len(line) + 1 + len(nxt) > alternatives.WRAP_WIDTH, (
                "ragged: %r could have taken %r" % (line, nxt))


def test_the_wrapper_leaves_structure_and_code_exactly_as_it_found_them():
    """Line breaks are significant inside all four, so the pass must not
    gather them into paragraphs."""
    source = "\n".join([
        "# A heading that is quite long but is a heading and so is left alone",
        "",
        "| Mount | Class | A table row that runs past the width on purpose |",
        "|---|---|---|",
        "",
        "    an indented code line that is deliberately much longer than the width",
        "",
        "- a bullet item that is also deliberately longer than the wrapping width",
        "",
        "> a quoted line that is likewise longer than the width and must survive",
        "",
        "```",
        "a fenced line that is longer than the width and must survive untouched",
        "```",
        "",
    ])
    assert alternatives.reflow(source) == source


def test_a_substituted_number_at_the_start_of_a_line_is_not_an_ordered_list():
    """`@@MAXDEPTH@@. The measurements ...` renders as `2. The measurements`,
    which looks like a list item and is not one -- CommonMark lets an ordered
    list interrupt a paragraph only when it starts at 1. Reading it as
    structure stranded the rest of the sentence on its own line."""
    source = "guarded on the compiled default: depth\n2. The measurements follow.\n"
    assert alternatives.reflow(source) == (
        "guarded on the compiled default: depth 2. The measurements follow.\n")

    # A real list, which starts its own block, still survives.
    real = "A paragraph.\n\n1. first\n2. second\n"
    assert alternatives.reflow(real) == real
