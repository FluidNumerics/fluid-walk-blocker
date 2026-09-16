"""Render `guard.sh` and `wrapped_names.sh` from the rule table and a site.

The shim is POSIX `sh` and cannot import the rule table, so the table is
generated into it (ADR-0013, ADR-0015) through the whole-file templates
`node/shim/guard.sh.in` and `node/shim/wrapped_names.sh.in`. Placeholders are
`@@NAME@@`, not printf-style: shell parameter expansion is full of `%`
(`${x%/*}`, `printf '%s'`), and %-formatting a shell script means
double-escaping every one of them.

Three inputs, kept apart on purpose:

  * `policy`  -- a `search_rules.Policy`: the site's filesystem decisions,
                 already validated and default-filled by `config`.
  * `site`    -- the default-filled `SiteConfig.data` dict, for everything
                 that is not a filesystem decision: trusted binaries, the
                 install prefix, the scheduler advice, the display strings.
  * `version` -- the `MAJOR.MINOR.PATCH` string from `VERSION`.

Every value bound for single quotes in the generated `sh` is shape-checked
first: a quote, a backslash or a newline inside `SG_VERSION='...'` would close
the string and execute the rest, above `sg_exec_real`, in the region that must
fork nothing. The schema refuses those characters for most keys; this is the
check that does not depend on remembering which.
"""
import os
import re

from .. import paths
from .. import search_rules as R

GUARD_TEMPLATE = "guard.sh.in"
NAMES_TEMPLATE = "wrapped_names.sh.in"

_PLACEHOLDER = re.compile(r"@@[A-Z_]+@@")

# Characters no value may carry into a single-quoted `sh` literal.
_UNSAFE_QUOTED = re.compile(r"['\\\n\r]")


class RenderError(ValueError):
    """A site value that cannot be spliced into the shim as written."""


def template_path(name):
    return os.path.join(paths.node_dir(), "shim", name)


def _read_template(name):
    with open(template_path(name), encoding="utf-8") as fh:
        return fh.read()


def placeholders():
    """Every `@@KEY@@` the two templates use, as a frozenset."""
    found = set()
    for name in (GUARD_TEMPLATE, NAMES_TEMPLATE):
        found.update(_PLACEHOLDER.findall(_read_template(name)))
    return frozenset(found)


def sh_quoted(value, what):
    """`value` as it will sit inside single quotes, or RenderError.

    Refused rather than escaped: the values this guards are paths, binary
    names, filesystem types and a version string, none of which legitimately
    contains a quote, a backslash or a line break. Escaping would let a
    malformed site value through silently, and a clear refusal at build is
    the whole point of compiling at build (ADR-0013).
    """
    value = str(value)
    hit = _UNSAFE_QUOTED.search(value)
    if hit:
        raise RenderError("%s contains %r, which cannot be spliced into a "
                          "single-quoted sh literal: %r" % (what, hit.group(), value))
    return value


def _one_line(value, what):
    """A value bound for a comment or a printf argument: no line breaks."""
    value = str(value)
    if "\n" in value or "\r" in value:
        raise RenderError("%s contains a line break: %r" % (what, value))
    return value


def glob_to_ere(pattern):
    """One fnmatch-style glob as one anchored POSIX ERE.

    `*` and `?` are the wildcards; a bracket expression passes through with
    a leading `!` spelled `^`; every other ERE metacharacter is escaped. The
    twin of `sg_glob_to_ere` in the template, which converts a runtime
    WALK_BLOCKER_FSTYPES override the same way; the suite drives both against
    one list. `fuse.*` -> `^fuse\\..*$`.
    """
    out = ["^"]
    prev = ""
    for c in pattern:
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "!":
            out.append("^" if prev == "[" else "!")
        elif c in "[]":
            out.append(c)
        elif c in ".+(){}^$|\\":
            out.append("\\" + c)
        else:
            out.append(c)
        prev = c
    out.append("$")
    return "".join(out)


def profile_case(profile):
    """One `case` arm: every variable the static logic needs for this tool."""
    return (
        "    %s)\n"
        "        sg_always=%d\n"
        "        sg_diract='%s'\n"
        "        sg_rec='%s'\n"
        "        sg_recval='%s'\n"
        "        sg_depth='%s'\n"
        "        sg_outdepth='%s'\n"
        "        sg_devflags='%s'\n"
        "        sg_devoff='%s'\n"
        "        sg_value='%s'\n"
        "        sg_cluster='%s'\n"
        "        sg_pat='%s'\n"
        "        sg_exact='%s'\n"
        "        sg_abbrev=%d\n"
        "        sg_execflags='%s'\n"
        "        sg_execplus='%s'\n"
        "        sg_rootflags='%s'\n"
        "        sg_cwdflags='%s'\n"
        "        sg_skip=%d\n"
        "        sg_mode=%s\n"
        "        sg_dashdash=%s\n"
        "        sg_default='%s'\n"
        "        sg_defdepth='%s'\n"
        "        sg_depthrec=%d\n"
        "        sg_depthrange=%d\n"
        # The digit class is a tool-grammar fact, not a site figure; the
        # in-line waiver keeps the IP gate's nine-digit pattern quiet on it.
        "        sg_digitchars='%s'  # site-literal-ok: tool digit class\n"
        "        sg_ttyfallback=%d\n"
        "        ;;\n"
    ) % (
        "|".join(profile.names),
        1 if profile.always else 0,
        profile.dir_action_default or "",
        " ".join(profile.rec_flags),
        # `flag=v1,v2`: one token per FLAG, with its on-values comma-joined.
        # Not one token per (flag, value) -- every shell loop that reads
        # this `break`s on the first token whose flag matches, so a second
        # token for the same flag would never be reached and
        # `-d dereference-recurse` would read as "skip".
        " ".join("%s=%s" % (flag, ",".join(values))
                 for flag, values in profile.rec_value_flags),
        " ".join(profile.depth_flags),
        " ".join(profile.output_only_depth_flags),
        " ".join(profile.device_flags),
        " ".join(profile.device_off_flags),
        " ".join(profile.value_flags),
        "".join(sorted(profile.cluster_letters or "")),
        " ".join(profile.pat_flags),
        " ".join(profile.exact_flags),
        1 if profile.abbreviates else 0,
        " ".join(profile.exec_flags),
        " ".join(profile.exec_plus_flags),
        " ".join(profile.root_flags),
        " ".join(profile.cwd_flags),
        profile.skip_pos,
        profile.root_mode,
        profile.dashdash,
        profile.default_root,
        "" if profile.default_depth is None else profile.default_depth,
        1 if profile.depth_enables_traversal else 0,
        1 if profile.depth_range else 0,
        "".join(sorted(profile.depth_digits or "")),
        1 if profile.fallback_needs_tty else 0,
    )


def mount_overrides(policy):
    """`[[filesystems.mounts]]` as the shim's `PATH=CLASS[=MAXDEPTH]` tokens.

    Space-separated, sorted by path so a build is byte-stable. Both readers
    in the shim word-split this and split each token on `=`; the schema's
    `sink_path` admits neither whitespace nor `=`, and the same rule is
    asserted here so a policy built by hand cannot produce a token that
    splits wrongly.
    """
    tokens = []
    for path in sorted(policy.mounts):
        cls, maxdepth = policy.mounts[path]
        if re.search(r"[\s=]", path):
            raise RenderError("mount override path %r contains whitespace or "
                              "'=', which the shim's override table cannot "
                              "carry" % path)
        token = "%s=%s" % (path, cls)
        if maxdepth is not None:
            token += "=%d" % maxdepth
        tokens.append(token)
    return " ".join(tokens)


def _extra_job_tools(site):
    """One `printf` line per configured tool, or nothing."""
    lines = []
    for tool in site["slurm"]["extra_job_tools"]:
        lines.append("        printf '    %%s %%s\\n' '%s' \"$sg_jobid\"\n"
                     % sh_quoted(tool, "slurm.extra_job_tools"))
    return "".join(lines)


def _optional_line(site, key, label):
    value = site["site"].get(key)
    if not value:
        return ""
    return ("    printf '%%s: %%s\\n' '%s' '%s'\n"
            % (label, sh_quoted(value, "site." + key)))


def _site_data(site):
    """The default-filled dict. The contract is `SiteConfig.data`; a
    `SiteConfig` itself is unwrapped so a caller holding one need not know
    which the renderer wants."""
    return getattr(site, "data", site)


def substitutions(policy, site, version):
    """Every `@@KEY@@` -> text pair the guard template needs."""
    site = _site_data(site)
    unwrapped = set(site["shim"]["unwrapped_tools"])
    fstypes = [sh_quoted(f, "filesystems.remote_fstypes") for f in policy.remote_fstypes]
    for f in fstypes:
        if re.search(r"\s", f):
            raise RenderError("remote_fstypes entry %r contains whitespace" % f)
    trusted = site["trusted_binaries"]
    return {
        # wrapped_profiles(), not PROFILES: a tool the site leaves alone gets
        # no case arm at all, matching its absence from wrapped_names.sh --
        # the two artifacts are built from the SAME filtered tuple so they
        # cannot drift from each other.
        "@@CASES@@": "".join(profile_case(p) for p in R.wrapped_profiles(unwrapped)),
        "@@FSTYPES@@": " ".join(fstypes),
        "@@REMOTE_FSTYPES_RE@@": " ".join(glob_to_ere(f) for f in fstypes),
        "@@REMOTE_PROXY@@": "1" if policy.remote_proxy else "0",
        "@@MOUNT_OVERRIDES@@": mount_overrides(policy),
        "@@MAXDEPTH@@": str(policy.maxdepth_allowed),
        "@@UNSCOPED_DEPTH@@": str(policy.unscoped_depth),
        "@@DEPTH_ALLOWANCE_MAX@@": str(policy.depth_allowance_max),
        "@@MOUNTS_DEFAULT@@": sh_quoted(site["filesystems"]["mount_table"],
                                        "filesystems.mount_table"),
        "@@LOGGER@@": sh_quoted(trusted["logger"], "trusted_binaries.logger"),
        "@@AWK@@": sh_quoted(trusted["awk"], "trusted_binaries.awk"),
        "@@ID@@": sh_quoted(trusted["id"], "trusted_binaries.id"),
        "@@DATE@@": sh_quoted(trusted["date"], "trusted_binaries.date"),
        "@@BIN_DIR@@": sh_quoted(os.path.join(site["install"]["prefix"], "bin"),
                                 "install.prefix"),
        "@@ESCAPE@@": R.ESCAPE_HATCH,
        "@@EXIT@@": str(R.EXIT_REFUSED),
        "@@VERSION@@": sh_quoted(version, "VERSION"),
        "@@DISPLAY_NAME@@": _one_line(site["site"]["display_name"], "site.display_name"),
        "@@DOCS_LINE@@": _optional_line(site, "docs_url", "Documentation"),
        "@@CONTACT_LINE@@": _optional_line(site, "contact", "Contact"),
        "@@MIN_JOB_AGE@@": str(int(site["slurm"]["min_job_age_s"])),
        "@@EXTRA_JOB_TOOLS@@": _extra_job_tools(site),
        # find's leading grammar, generated so it cannot drift from the table.
        "@@FIND_PREFIX@@": "|".join(R.FIND_PREFIX_FLAGS),
        "@@FIND_VALUE_LEAD@@": "|".join(R.FIND_VALUE_LEADING_FLAGS),
        "@@FIND_GLUED@@": "|".join(p + "*" for p in R.FIND_GLUED_LEADING_PREFIXES),
    }


def _fill(text, table, what):
    for key, value in table.items():
        text = text.replace(key, value)
    assert "@@" not in text, "unsubstituted placeholder in %s" % what
    return text


def render_shim(policy, site, version):
    """The full text of `guard.sh`."""
    return _fill(_read_template(GUARD_TEMPLATE), substitutions(policy, site, version),
                 GUARD_TEMPLATE)


def render_wrapped_names(policy, site, version):
    """The full text of `wrapped_names.sh`."""
    unwrapped = set(_site_data(site)["shim"]["unwrapped_tools"])
    names = " ".join(sh_quoted(n, "wrapped name") for n in R.wrapped_names(unwrapped))
    return _fill(_read_template(NAMES_TEMPLATE), {"@@WRAPPED_NAMES@@": names},
                 NAMES_TEMPLATE)
