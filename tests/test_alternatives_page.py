"""`docs/what-to-run-instead.md`: rendered per site, checked like the shim.

The refusal names this page by its installed path, so what it says about a
mount must be what the shim does about that mount, and what it shows as a
measurement must be what the site recorded -- rendered from the same
`site.toml`, never retyped.
"""
import importlib.util
import os

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
                     "`/usr/local/lib/walk-blocker`", "`/usr/local/lib/walk-blocker/bin`",
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
        "@@MIN_JOB_AGE@@", "@@EXTRA_JOB_TOOLS_LINES@@", "@@PREFIX@@", "@@BIN_DIR@@",
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
