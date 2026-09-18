"""Harness for `tests/test_install.py`: a STAMPED installer in a sandbox.

`node/shim/install.sh` carries every site value as a `# GENERATED from`
marker line (ADR-0013) and takes no path flags, so a test that needs its
writes sandboxed under `tmp_path` cannot pass different locations on the
command line the way the predecessor's suite did. It stamps a copy instead,
through the same `stamp_text()` the build uses, with a FICTIONAL site whose
prefix, hook files, spool and tool path all live under `tmp_path` -- and
stages a rendered `guard.sh` and `wrapped_names.sh` beside it, from the real
renderer, because the installer's behaviour depends on its own location
(`$HERE/wrapped_names.sh` is sourced; `require_deployed_copy()` compares
`$HERE` against `$PREFIX/shim`).

No side effects outside the tmp tree, including the journal. The stamped
copy points `trusted_binaries.logger` at a path that does not exist AND has
its `command -v logger` PATH fallback renamed, so no test can reach a real
syslog sink however open its PATH is. `sg_report()` emits valid JSON under
the production `walk-blocker` tag and journald stamps its own `_UID`, so a
test-emitted record would be indistinguishable from a real one.

Root is faked via a stub `id` on PATH -- not an env var override, which
would be exactly the kind of unprivileged bypass surface this installer
exists to close off (ADR-0004).
"""

import json
import os
import pathlib
import shutil
import subprocess

from conftest import ROOT, make_policy, make_site, recording_logger_stub, render_to
from walk_blocker import __version__, stamp
from walk_blocker.render import shim as render_shim_module

INSTALL_SH = os.path.join(ROOT, "node", "shim", "install.sh")

# The shell these tests drive install.sh with. `/bin/sh` is dash on the
# enterprise Debian-family node this was written for and bash on many
# workstations, so running `sh` here would test a different shell from the
# one production uses -- and install.sh is full of constructs where they
# differ: `set -e` on a failing AND-OR list, and an EXIT trap that calls
# `exit`. Falls back to `sh` where dash is absent, and a test makes that
# fallback visible instead of silent.
SH = shutil.which("dash") or shutil.which("sh") or "sh"

NO_LOGGER = "/nonexistent/logger"

# The name the COPY looks for when it falls back to a PATH lookup. Production
# looks for `logger`; a copy looks for this, so no test can reach a real
# syslog sink however open its PATH is. Neutralizing the trusted absolute
# path alone is not enough: sg_report falls back to `command -v logger`, so
# an invocation handed an unrestricted PATH would still find the real one.
TEST_LOGGER = "walk-blocker-test-logger"

AUDIT_FILENAME = "walk-blocker-audit.jsonl"

# The fixture mount table the reconcile's mount report reads, in
# /proc/mounts field order. `/home` and `/opt/site-tools` are covered by the
# fixture policy's overrides; `/archive` is a remote mount no override
# covers; `/` and `/run` are local and cheap by default.
FIXTURE_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
fast /home wekafs rw,relatime 0 0
nas:/archive /archive nfs4 rw,relatime 0 0
nas:/tools /opt/site-tools nfs4 rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
"""

# The fixture policy: two listed types, the proxy on, `/home` loosened to
# depth 4 and a small remote export made cheap -- the shape ADR-0016
# describes, with fictional values.
FIXTURE_POLICY = make_policy(mounts={"/home": ("expensive", 4),
                                     "/opt/site-tools": ("cheap", None)})

# A stock system bashrc's interactivity guard, which is why the block has to
# go above it.
STOCK_BASHRC = """\
# System-wide .bashrc file for interactive bash(1) shells.

# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac

export EDITOR=vim
"""

# What install.sh itself needs to run. Symlinked into the sandbox PATH one by
# one rather than by adding /usr/bin, which would also supply the real find,
# grep and du and make "only what exists gets shimmed" untestable. bash, zsh
# and fish are never the real binaries (see the stubs below); id is here for
# is_root(), but tests that care about the root/not-root distinction override
# it with fake_uid.
UTILITIES = ("sh", "dirname", "pwd", "mkdir", "rm", "rmdir", "ln", "mv",
             "cat", "touch", "awk", "id", "install", "mktemp", "chmod", "sed",
             "stat")

# Real bash's SSH_SOURCE_BASHRC feature reads a HARDCODED path -- never a file
# named by configuration -- so it cannot be pointed at a sandboxed hook file.
# These stubs emulate the two documented outcomes (ADR-0008) instead of
# exercising the real binary: one sources $WALK_BLOCKER_TEST_BASHRC under the
# same SHLVL/SSH_CLIENT gate real bash uses, the other never sources anything.
# The WALK_BLOCKER_TEST_* names are read by these STUBS only; install.sh
# never sees them.
BASH_STUB_SOURCES_HOOK = """\
#!/bin/sh
if [ "$1" = "-c" ]; then
    shift
    if [ "${SHLVL:-1}" -lt 2 ] && [ -n "${SSH_CLIENT:-}" ] \\
       && [ -n "${WALK_BLOCKER_TEST_BASHRC:-}" ] && [ -f "$WALK_BLOCKER_TEST_BASHRC" ]; then
        . "$WALK_BLOCKER_TEST_BASHRC"
    fi
    eval "$1"
else
    exec /bin/sh "$@"
fi
"""

BASH_STUB_IGNORES_HOOK = """\
#!/bin/sh
shift
exec /bin/sh -c "$@"
"""

# Real zsh's zshenv is read UNCONDITIONALLY (ADR-0008), so unlike the bash
# stubs there is no SHLVL/SSH_CLIENT gate to emulate -- just a hardcoded path
# ($ZDOTDIR aside) that cannot be pointed at a sandboxed file.
ZSH_STUB_SOURCES_HOOK = """\
#!/bin/sh
if [ "$1" = "-c" ]; then
    shift
    if [ -n "${WALK_BLOCKER_TEST_ZSHENV:-}" ] && [ -f "$WALK_BLOCKER_TEST_ZSHENV" ]; then
        . "$WALK_BLOCKER_TEST_ZSHENV"
    fi
    eval "$1"
else
    exec /bin/sh "$@"
fi
"""

ZSH_STUB_IGNORES_HOOK = """\
#!/bin/sh
shift
exec /bin/sh -c "$@"
"""

# Real fish's conf.d is read UNCONDITIONALLY too, from a hardcoded sysconfdir.
# The stub cannot `.` the generated file the way the two above do: their
# content is plain POSIX sh, while fish_conf_block() emits fish syntax
# (`set -gx`, `if not contains ... end`) that dash dies on immediately. So
# this extracts the one line the generated content always carries
# (`set -gx WALK_BLOCKER_SHIM_DIR '<dir>'`) and prepends it directly,
# simulating fish's OUTCOME rather than interpreting its grammar.
FISH_STUB_SOURCES_HOOK = """\
#!/bin/sh
if [ "$1" = "-c" ]; then
    shift
    if [ -n "${WALK_BLOCKER_TEST_FISHCONF:-}" ] && [ -f "$WALK_BLOCKER_TEST_FISHCONF" ]; then
        _wb_dir=$(sed -n "s/^set -gx WALK_BLOCKER_SHIM_DIR '\\(.*\\)'\\$/\\1/p" "$WALK_BLOCKER_TEST_FISHCONF")
        [ -n "$_wb_dir" ] && PATH="$_wb_dir:$PATH"
    fi
    eval "$1"
else
    exec /bin/sh "$@"
fi
"""

FISH_STUB_IGNORES_HOOK = """\
#!/bin/sh
shift
exec /bin/sh -c "$@"
"""

ID_STUB = '#!/bin/sh\nif [ "$1" = "-u" ]; then echo %d; else exit 1; fi\n'
STAT_ROOT_755 = "#!/bin/sh\nprintf '0 755\\n'\n"
TOOL_STUB = "#!/bin/sh\nexit 0\n"


def _which(name):
    for directory in ("/usr/bin", "/bin", "/usr/local/bin"):
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return None


def write_stub(path, body, mode=0o755):
    path = pathlib.Path(str(path))
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text(body)
    path.chmod(mode)
    return path


class Layout(object):
    """Where one stamped installer's site lives under tmp_path. Every
    attribute is a `pathlib.Path` except `values`, the stamp mapping."""

    def __init__(self, tmp_path, prefix=None, bashrc=None, zshenv=None,
                 fishconf=None, spool=None, toolbin=None, mount_table=None):
        tmp_path = pathlib.Path(str(tmp_path))
        self.tmp_path = tmp_path
        self.prefix = pathlib.Path(str(prefix or tmp_path / "prefix"))
        self.bashrc = pathlib.Path(str(bashrc or tmp_path / "bashrc"))
        self.zshenv = pathlib.Path(str(zshenv or tmp_path / "zshenv"))
        self.fishconf = pathlib.Path(str(fishconf or tmp_path / "fish-conf.fish"))
        self.spool = pathlib.Path(str(spool or tmp_path / "var-log"))
        self.toolbin = pathlib.Path(str(toolbin or tmp_path / "usrbin"))
        self.mount_table = pathlib.Path(str(mount_table or tmp_path / "mounts"))
        self.audit = self.spool / AUDIT_FILENAME
        self.bin = self.prefix / "bin"
        self.shim_dir = self.prefix / "shim"
        self.script = None
        self.values = None

    def shims(self):
        """The wrapped-tool links in $PREFIX/bin, without walk-job.

        walk-job is linked there too and is deliberately NOT a shim: it is
        the alternative every refusal advertises, linked unconditionally
        because nothing else on the node provides it. Tests about which TOOLS
        are wrapped say so by excluding it here rather than by listing it as
        an expected shim, so a real coverage change still shows up."""
        return set(os.listdir(str(self.bin))) - {"walk-job"}


def site_values(layout, **overrides):
    """The `values` mapping `stamp_text` is handed: every source the
    installer's markers name, for the fictional site laid out under
    `layout`. `overrides` are dotted site keys (`"hooks.zsh.gate"`), or
    `"VERSION"`."""
    values = {
        "VERSION": __version__,
        "site.toml:install.prefix": str(layout.prefix),
        "site.toml:install.spool_dir": str(layout.spool),
        "site.toml:install.audit_filename": AUDIT_FILENAME,
        "site.toml:install.tool_search_path": [str(layout.toolbin)],
        "site.toml:hooks.bash.file": str(layout.bashrc),
        "site.toml:hooks.bash.package": "bash",
        "site.toml:hooks.bash.enabled": True,
        "site.toml:hooks.bash.gate": "required",
        "site.toml:hooks.zsh.file": str(layout.zshenv),
        "site.toml:hooks.zsh.package": "zsh",
        "site.toml:hooks.zsh.enabled": True,
        "site.toml:hooks.zsh.gate": "required",
        "site.toml:hooks.fish.file": str(layout.fishconf),
        "site.toml:hooks.fish.enabled": True,
        "site.toml:hooks.fish.gate": "best-effort",
        "site.toml:trusted_binaries.logger": NO_LOGGER,
        "site.toml:filesystems.mount_table": str(layout.mount_table),
        "site.toml:filesystems.remote_fstypes": list(FIXTURE_POLICY.remote_fstypes),
        "site.toml:filesystems.remote_proxy": FIXTURE_POLICY.remote_proxy,
        # The same function the build's derived value comes from, so the
        # installer's table is rendered exactly as the shim's is.
        "site.toml:derived.mount_overrides": render_shim_module.mount_overrides(FIXTURE_POLICY),
    }
    for key, value in overrides.items():
        full = key if key == "VERSION" else "site.toml:" + key
        assert full in values, "no marker in install.sh reads %s" % full
        values[full] = value
    return values


def stamp_install_text(values):
    """The installer's text, stamped for `values`, with its logger PATH
    fallback renamed so a copy can never reach a real sink."""
    text = stamp.stamp_text(open(INSTALL_SH).read(), values, "sh")
    fallback = "command -v logger"
    assert text.count(fallback) == 1, "the logger fallback moved or multiplied"
    text = text.replace(fallback, "command -v %s" % TEST_LOGGER)
    assert fallback not in text, "a copy can still reach a real logger"
    return text


def stamped_install(tmp_path, dest=None, layout=None, mount_table_text=None,
                    policy=None, **site_overrides):
    """A stamped `install.sh` at `dest` (default `$PREFIX/shim`) with a
    rendered `guard.sh` and `wrapped_names.sh` beside it, for a fictional
    site under `tmp_path`. Returns the `Layout`, with `.script` set.

    The ONLY sanctioned way to put the installer somewhere a test can run
    it: this is where the audit sink is neutralized."""
    layout = layout or Layout(tmp_path)
    dest = pathlib.Path(str(dest or layout.shim_dir))
    dest.mkdir(parents=True, exist_ok=True)
    layout.values = site_values(layout, **site_overrides)
    text = stamp_install_text(layout.values)
    script = dest / "install.sh"
    script.write_text(text)
    script.chmod(0o755)
    assert "\nSG_LOGGER='%s'" % NO_LOGGER in text
    if not layout.mount_table.exists():
        layout.mount_table.write_text(
            FIXTURE_MOUNTS if mount_table_text is None else mount_table_text)
    render_to(str(dest), policy or FIXTURE_POLICY, make_site(str(layout.mount_table)))
    layout.script = script
    return layout


def stage_walk_job(prefix):
    """A `walk-job` at $PREFIX/walk-job, the way deploy.py puts it there.

    A stand-in, not the real tool: install.sh checks the file's type, owner
    and mode before linking $PREFIX/bin/walk-job at it, and never reads it.
    A prefix without it is refused -- which is the point, since a refusal
    that advertises walk-job needs the file to exist."""
    prefix = pathlib.Path(str(prefix))
    prefix.mkdir(parents=True, exist_ok=True)
    return write_stub(prefix / "walk-job", TOOL_STUB)


def stage_readme(prefix):
    """README.md at $PREFIX/README.md, the way deploy.py puts it there. Read,
    never executed, so not in require_root_owned_payload()'s list -- but the
    hook blocks point every user at it, and a test asserts it resolves."""
    prefix = pathlib.Path(str(prefix))
    prefix.mkdir(parents=True, exist_ok=True)
    target = prefix / "README.md"
    shutil.copyfile(os.path.join(ROOT, "README.md"), str(target))
    target.chmod(0o644)
    return target


def populate_bin(fake_bin, tools=("find", "grep", "du"), fake_uid=None,
                 stat_body=STAT_ROOT_755, bash=BASH_STUB_SOURCES_HOOK,
                 zsh=ZSH_STUB_SOURCES_HOOK, fish=FISH_STUB_SOURCES_HOOK,
                 logger_log=None):
    """A closed PATH: the utilities install.sh needs, the shell stubs, the
    stub tools, and -- optionally -- a fake `id`, a fake `stat` and a
    recording logger under TEST_LOGGER. `stat_body=None` keeps the real
    stat, so the walk sees the tmp tree's true ownership."""
    fake_bin = pathlib.Path(str(fake_bin))
    if fake_bin.exists():
        for stale in fake_bin.iterdir():
            stale.unlink()
    else:
        fake_bin.mkdir(parents=True)
    for name in UTILITIES:
        if name == "id" and fake_uid is not None:
            continue
        if name == "stat" and stat_body is not None:
            continue
        real = _which(name)
        if real and name not in tools:
            os.symlink(real, str(fake_bin / name))
    if stat_body is not None:
        # require_root_owned_payload() walks $HERE's ancestors and demands uid
        # 0 with no group/other write. Every path in this tmp tree is owned by
        # the test user, which is exactly the shape the check exists to
        # refuse, so orchestration tests get an answer standing in for the
        # post-deploy state. The check has its own tests, run with the real
        # stat against a really user-owned tree.
        write_stub(fake_bin / "stat", stat_body)
    if bash is not None:
        write_stub(fake_bin / "bash", bash)
    if zsh is not None:
        write_stub(fake_bin / "zsh", zsh)
    if fish is not None:
        write_stub(fake_bin / "fish", fish)
    if fake_uid is not None:
        write_stub(fake_bin / "id", ID_STUB % fake_uid)
    for tool in tools:
        write_stub(fake_bin / tool, TOOL_STUB)
    if logger_log is not None:
        write_stub(fake_bin / TEST_LOGGER, recording_logger_stub(logger_log))
    return fake_bin


def next_copy_dir(layout):
    """A fresh `copyN` under tmp_path: where a run that is NOT approved gets
    its stamped copy, standing in for a checkout (see run_install)."""
    return layout.tmp_path / ("copy%d" % len(list(layout.tmp_path.glob("copy*"))))


def sandbox_env(layout, fake_bin, **extra):
    env = {"HOME": str(layout.tmp_path / "home"), "PATH": str(fake_bin),
           "WALK_BLOCKER_TEST_BASHRC": str(layout.bashrc),
           "WALK_BLOCKER_TEST_ZSHENV": str(layout.zshenv),
           "WALK_BLOCKER_TEST_FISHCONF": str(layout.fishconf)}
    env.update(extra)
    return env


def run_install(tmp_path, args, tools=("find", "grep", "du"),
                bash_ignores_bashrc=False, zsh_ignores_zshenv=False,
                fish_ignores_conf=False, fish_absent=False, zsh_absent=False,
                fake_uid=None, layout=None, script=None, real_stat=False,
                stat_body=None, timeout=60, walk_job=True, env=None,
                shell=None, cwd=None, **site_overrides):
    """Stamp, stage and run the installer once. Returns `(result, layout)`.

    An APPROVED install must run from $PREFIX/shim, because link_farm points
    every shim at "$HERE/guard.sh" -- run out of a checkout it puts a
    user-owned file on every other user's PATH. So an approved run stamps
    the copy where deploy.py would stage it; every other run gets a copy
    elsewhere under tmp_path, standing in for a checkout. `script=` runs a
    copy the test staged itself."""
    layout = layout or Layout(tmp_path)
    populate_bin(
        layout.toolbin, tools=tools, fake_uid=fake_uid,
        stat_body=None if real_stat else (stat_body or STAT_ROOT_755),
        bash=BASH_STUB_IGNORES_HOOK if bash_ignores_bashrc else BASH_STUB_SOURCES_HOOK,
        zsh=None if zsh_absent else (
            ZSH_STUB_IGNORES_HOOK if zsh_ignores_zshenv else ZSH_STUB_SOURCES_HOOK),
        fish=None if fish_absent else (
            FISH_STUB_IGNORES_HOOK if fish_ignores_conf else FISH_STUB_SOURCES_HOOK))

    # Before the payload check, which reads $PREFIX/walk-job.
    if walk_job:
        stage_walk_job(layout.prefix)
    stage_readme(layout.prefix)

    if script is None:
        # A WRITING --system run must come from the deployed copy;
        # require_deployed_copy() refuses it from anywhere else. Keyed
        # on "--system and not --dry-run", which is what the approval
        # flag used to stand in for -- `--uninstall` and `--relink`
        # keep taking a fresh copy dir exactly as before.
        dest = (layout.shim_dir
                if "--system" in args and "--dry-run" not in args
                else next_copy_dir(layout))
        stamped_install(tmp_path, dest=dest, layout=layout, **site_overrides)
        script = layout.script
    else:
        layout.script = pathlib.Path(str(script))

    # `timeout` is not decoration: a test asserting that install.sh refuses
    # BEFORE sourcing something has to be able to fail rather than hang. A
    # fifo sourced as root blocks forever, so without a bound the suite would
    # stop instead of reporting.
    result = subprocess.run(
        [shell or SH, str(script)] + list(args),
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
        env=env if env is not None else sandbox_env(layout, layout.toolbin))
    return result, layout


def relink_with_a_recording_logger(tmp_path, layout, tools=("find", "grep"),
                                   fake_uid=None, script=None):
    """Run `--relink` with a logger stub on a closed PATH. Returns
    `(result, [records])`; the stub is the only observable sink, since the
    stamped copy points the trusted absolute path at nothing.

    A root relink is refused unless it runs from the DEPLOYED copy, so a
    fake_uid=0 caller passes the installed script here."""
    if script is None:
        stamped_install(tmp_path, dest=next_copy_dir(layout), layout=layout)
        script = layout.script
    bin_dir = layout.toolbin
    log = layout.tmp_path / "logger-calls.txt"
    if not log.exists():
        log.write_text("")
    stat_body = STAT_ROOT_755
    if fake_uid is not None:
        # `stat` needs BOTH halves here, which the flat "0 755" stub cannot
        # give. The ancestor walk must look root-owned and tight, exactly as
        # elsewhere -- but assert_audit_dir compares the audit directory's
        # mode before and after, so for THAT one path the real mode has to
        # come through or the comparison can never see a change.
        stat_body = (
            '#!/bin/sh\n'
            'if [ "$1" = "-c" ] && [ "$2" = "%u %a" ]; then\n'
            '    if [ "$3" = "' + str(layout.spool) + '" ]; then\n'
            '        m=$(' + _which("stat") + ' -c %a "$3" 2>/dev/null) || exit 1\n'
            '        printf \'0 %s\\n\' "$m"\n'
            '        exit 0\n'
            '    fi\n'
            "    printf '0 755\\n'\n"
            '    exit 0\n'
            'fi\n'
            'exec ' + _which("stat") + ' "$@"\n')
    populate_bin(bin_dir, tools=tools, fake_uid=fake_uid, stat_body=stat_body,
                 logger_log=log)
    before = len(log.read_text().splitlines())

    result = subprocess.run(
        [SH, str(script), "--relink"],
        capture_output=True, text=True, timeout=60,
        env=sandbox_env(layout, bin_dir))
    records = [json.loads(line.split("-- ", 1)[1])
               for line in log.read_text().splitlines()[before:]]
    return result, records
