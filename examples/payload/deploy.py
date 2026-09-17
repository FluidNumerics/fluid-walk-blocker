#!/usr/bin/env python3
"""Deploy walk-blocker from a built payload onto the node it was built for.

    python3 deploy.py --system           PRINT the root commands and exit
    python3 deploy.py --system --i-have-approval
                                         as root, actually install
    python3 deploy.py --uninstall        as root, reverse a --system install
    python3 deploy.py --version

This file ships at the root of the payload `walk-blocker build` emits and
runs FROM there: the directory it sits in IS the payload. Stdlib-only,
Python 3.9, no PEP 723 header (ADR-0015): the node has no `uv`, and this is
the one command handed to an authorized root operator, so it has to work
with the interpreter that is actually there.

There are NO path flags (ADR-0005). Every location this script writes as
root is a literal stamped in by the build from site.toml (ADR-0013): the
lines marked `# GENERATED from` below. A different location is a site.toml
change, a rebuild and a redeploy -- a diff someone read -- never an
argument. argparse rejects `--prefix` outright.

Three things about this deployment are design, not habit:

1.  The payload lives under `[install].prefix` and its audit trail under
    `[install].spool_dir`, both on LOCAL disk, never under a home directory.
    A home directory may be on the filesystem under investigation, and a
    monitor that lives on what it monitors is unavailable exactly when it is
    needed.

2.  The timer slot is `[timer].on_calendar`, chosen at the site against the
    LIVE schedule of the target node -- root's `systemctl list-timers`,
    every account's `systemctl --user list-timers`, and the site's cron --
    never copied from a doc or another site. A per-minute collector leaves
    the second field as the only separation, which is why the slot carries
    one. Jitter and coalescing (`RandomizedDelaySec`, `AccuracySec`) are
    site config for the same reason: both would walk a chosen slot back
    into its neighbours.

3.  The unit's `ExecStartPre` (Layer 1's reconcile) is `-`-prefixed AND
    bounded by `timeout --kill-after`. Layer 2 exists because Layer 1 is
    bypassable, so Layer 1's upkeep must never be able to take Layer 2
    down -- neither by refusing (the `-`) nor by hanging (the bound).

Root-owned, gated on real authentication (the operator is already root --
checked with `os.geteuid()`, not reimplemented) plus explicit authorization
(`--i-have-approval`). Reversing a control is the safer direction, so
`--uninstall` needs root alone (ADR-0004). This repository's own agent
sessions never pass `--i-have-approval` or otherwise act as root.

The payload is read ONCE, into a root-only snapshot, before anything that
can block; the copies read the snapshot. The source directory itself stays
owned by whoever unpacked it -- that is the workflow, and refusing it was
considered and rejected (ADR-0006).

`--system` and `--system --i-have-approval` make the SAME checks, from the
same `preflight()`: the arguments are the compiled literals, the six
root-write locations sit in a trusted chain (and the hook files are plain
root-owned regular files), the audit directory is usable and not writable
beyond root, the prefix is reachable by the users Layer 1 exists for, the
prefix is not somebody else's populated directory, and the parent the
payload snapshot is staged under is a directory in a trusted chain. The
contract is that reading the preview is enough -- the approved command must
not refuse what the preview accepted. Where the preview runs unprivileged
and may not make one of those stats it says "could not be checked as this
user" rather than reporting it clean; run as root, that same answer is a
refusal. A dry run stages nothing, so the staging parent is the one check
it does not make -- in the preview and in the install alike.
"""

import argparse
import atexit
import collections
import errno
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

# Compiled site values (ADR-0013). Every line marked GENERATED is rewritten
# by `walk-blocker build`; the values in the tree are sentinels, and a
# payload carries the site's. `walk-blocker build --check` reports a stale
# or missing one. Nothing on the node reads configuration.
__version__ = '0.1.0'  # GENERATED from VERSION
# One payload, one number: installer, reaper, shim and walk-job ship together
# and carry one version, because they are deployed as one and a per-file
# version would invite mixing them. A literal, NOT read at run time: this
# file is copied onto a node with nothing beside it to read the version from.

# The payload IS the directory this file is in.
REPO = os.path.dirname(os.path.abspath(__file__))

# `[install]` -- all on local disk, never on the filesystem under
# investigation. Every one of these is a root-write sink. They were CLI
# flags in the predecessor, each grew its own validation as review found
# what an arbitrary value could do, and `--unit-dir` settled it: a flag
# whose every non-default value can only fail or do damage is not a flag
# with a validation gap (ADR-0005). What that removed is ARGUMENT-shape
# validation, which the schema now asserts at build. What it did not touch
# is FILESYSTEM-state validation -- whether the prefix's parent is
# user-writable on this node, whether a hook file is a symlink -- which no
# constant-folding can answer. Those checks all remain below.
#
# The tests move these constants; that is the whole seam, and it adds no
# CLI surface.
DEFAULT_PREFIX = '/usr/local/lib/walk-blocker'  # GENERATED from site.toml:install.prefix
DEFAULT_UNIT_DIR = '/etc/systemd/system'  # GENERATED from site.toml:install.unit_dir
DEFAULT_SPOOL_DIR = '/var/log/walk-blocker'  # GENERATED from site.toml:install.spool_dir
DEFAULT_AUDIT_FILENAME = 'searchguard-audit.jsonl'  # GENERATED from site.toml:install.audit_filename

# Where stage_payload() puts its snapshot. NOT tempfile's default, which
# comes from TMPDIR: an environment variable would decide the parent, so a
# TMPDIR pointing anywhere writable puts the root-owned 0700 snapshot under a
# parent whose owner can rename or replace it mid-copy. /tmp and /var/tmp are
# 1777 on an ordinary node, so "others can write the parent" is the common
# case, not the misconfigured one. Checked at use rather than assumed, and
# refused rather than fallen back to, since every fallback candidate is the
# thing being avoided.
STAGING_PARENT = '/run'  # GENERATED from site.toml:install.staging_parent

# `[hooks.<shell>]` (ADR-0008). Each shell's system startup file is a
# root-write sink -- install.sh reads it, rewrites it 0644, and has the real
# shell source it AS ROOT to prove the hook fires -- so every one of them
# gets the same filesystem-state checks whether or not it is enabled: a
# disabled hook's file is still validated as a path, and never written or
# stripped. Which shells are enabled, and which are required, is a census
# result at the site, compiled in here and not decided by this file.
DEFAULT_BASHRC_FILE = '/etc/bash.bashrc'  # GENERATED from site.toml:hooks.bash.file
HOOK_ENABLED_BASH = True  # GENERATED from site.toml:hooks.bash.enabled
DEFAULT_ZSHENV_FILE = '/etc/zsh/zshenv'  # GENERATED from site.toml:hooks.zsh.file
HOOK_ENABLED_ZSH = True  # GENERATED from site.toml:hooks.zsh.enabled
DEFAULT_FISH_CONF_FILE = '/etc/fish/conf.d/walk-blocker.fish'  # GENERATED from site.toml:hooks.fish.file
HOOK_ENABLED_FISH = True  # GENERATED from site.toml:hooks.fish.enabled

# `[timer]`. The slot is chosen against the live schedule of the target node
# (see the module docstring); the rest are the settings that would undo that
# choice if left to systemd's defaults, so they are decided at the site too.
TIMER_SLOT = '*:07,17,27,37,47,57:30'  # GENERATED from site.toml:timer.on_calendar
TIMER_RANDOMIZED_DELAY_SEC = 0  # GENERATED from site.toml:timer.randomized_delay_sec
TIMER_ACCURACY_SEC = '1s'  # GENERATED from site.toml:timer.accuracy_sec
TIMER_PERSISTENT = False  # GENERATED from site.toml:timer.persistent
# The service's start budget, and the two halves of the reconcile's bound.
# `timeout_start_sec` must clear the reconcile bound plus the reaper's own
# worst case (its kill budget); `walk-blocker validate` computes that floor
# at build, so the unit never cuts a poll off mid-kill.
TIMEOUT_START_SEC = 240  # GENERATED from site.toml:timer.timeout_start_sec
RELINK_TIMEOUT_S = 30  # GENERATED from site.toml:timer.relink_timeout_s
RELINK_KILL_AFTER_S = 10  # GENERATED from site.toml:timer.relink_kill_after_s

# `[trusted_binaries]`: absolute paths for everything the unit executes as
# root, so nothing resolves through a PATH a user controls.
TRUSTED_TIMEOUT = '/usr/bin/timeout'  # GENERATED from site.toml:trusted_binaries.timeout
TRUSTED_SH = '/bin/sh'  # GENERATED from site.toml:trusted_binaries.sh
TRUSTED_PYTHON3 = '/usr/bin/python3'  # GENERATED from site.toml:trusted_binaries.python3

# `[site]`: how the site names this node in the unit descriptions.
DISPLAY_NAME = 'Example HPC login node'  # GENERATED from site.toml:site.display_name

SERVICE_UNIT = "walk-blocker.service"
TIMER_UNIT = "walk-blocker.timer"

# Exactly what this installs under the prefix, and therefore exactly what it
# is entitled to chown, chmod and inspect. Anything recursive is aimed at
# these entries, never at the prefix directory itself: the prefix is a
# literal, which answers the operator-slip half of `chown -R root:root /`,
# but the prefix may already hold files this install did not create, and a
# recursive mode change over it would take them too.
PAYLOAD_MARKER = ".walk-blocker-payload"
INSTALLED_ENTRIES = ("reaper.py", "search_rules.py", "survey.py", "walk-job",
                     "shim", "docs", "README.md", "site.toml", "site.lock.json",
                     PAYLOAD_MARKER)

# Everything system_execute() copies out of the payload, as
# (payload-relative path, is-a-directory, installed mode). Listed rather
# than globbed so a stray file in the payload directory is not deployed, and
# so the snapshot and the install copy the same set by construction.
#
#   reaper.py       Layer 2, run by the unit
#   search_rules.py the rule table the reaper imports as a sibling
#   survey.py       the out-of-band mount survey (ADR-0016)
#   walk-job        the alternative every refusal names: install.sh links it
#                   into $PREFIX/bin, so an advertised alternative that is
#                   not on the node is a refusal with nothing behind it
#   shim/           guard.sh, install.sh, wrapped_names.sh -- the unit runs
#                   install.sh as root on every poll
#   docs/, README.md   the hook blocks point every user at $PREFIX/README.md
#                   to explain why their PATH changed, and the README's
#                   reading order is a list of files under docs/
#   site.toml, site.lock.json   the record of what was built, never an
#                   input (ADR-0013): `cat` them to learn what is deployed
PAYLOAD_SOURCES = (
    ("reaper.py", False, "0755"),
    ("search_rules.py", False, "0644"),
    ("survey.py", False, "0644"),
    # 0755, like reaper.py: install.sh links $PREFIX/bin/walk-job at this and
    # the link is on every user's PATH, so it has to be executable BY them.
    ("walk-job", False, "0755"),
    ("README.md", False, "0644"),
    ("site.toml", False, "0644"),
    ("site.lock.json", False, "0644"),
    ("shim", True, None),
    ("docs", True, None),
)

# The hook files, with the shell each belongs to and the module constant
# that says whether the site enabled it. Read through hook_table() rather
# than captured here, so a test that moves a constant is reflected.
_HOOK_ATTRS = (
    ("bashrc_file", "bash", "HOOK_ENABLED_BASH"),
    ("zshenv_file", "zsh", "HOOK_ENABLED_ZSH"),
    ("fish_conf_file", "fish", "HOOK_ENABLED_FISH"),
)


def hook_table():
    """(attr, shell, enabled) per hook shell, from the live constants --
    read through the module dict, so a test that moves a flag is seen."""
    live = globals()
    return tuple((attr, shell, bool(live[flag])) for attr, shell, flag in _HOOK_ATTRS)


def enabled_hook_files(args):
    """The hook files install.sh will actually write or strip on this site,
    for messages that tell an operator where to look."""
    return [getattr(args, attr) for attr, _shell, enabled in hook_table()
            if enabled]


def default_paths():
    """The six root-write locations as an args-shaped mapping.

    Read through a function rather than captured at import, so a test that
    moves a constant is reflected here.
    """
    return {
        "prefix": DEFAULT_PREFIX,
        "unit_dir": DEFAULT_UNIT_DIR,
        "spool_dir": DEFAULT_SPOOL_DIR,
        "bashrc_file": DEFAULT_BASHRC_FILE,
        "zshenv_file": DEFAULT_ZSHENV_FILE,
        "fish_conf_file": DEFAULT_FISH_CONF_FILE,
    }


def not_a_default(args):
    """Which of the six paths in `args` is not the compiled literal, or None.

    The last line of defence rather than the first: main() offers no flags,
    so reaching this needs a caller inside the module. It stays because "the
    CLI does not accept it" and "the code cannot do it" are different
    guarantees, and the second is the one ADR-0005 claims.
    """
    for attr, expected in sorted(default_paths().items()):
        got = getattr(args, attr)
        if canonical_prefix(got) != canonical_prefix(expected):
            return ("deploy.py: refusing %s=%s: this installer takes no path "
                    "arguments and writes\n  only to %s, the literal compiled "
                    "from site.toml. See ADR-0005 and ADR-0013.\n"
                    % (attr, got, expected))
    return None


def audit_path(spool):
    """Layer 1's trail: the compiled filename under the spool. install.sh
    joins the same two literals, so the two files cannot disagree."""
    return os.path.join(spool, DEFAULT_AUDIT_FILENAME)


UNIT_SERVICE = """\
[Unit]
Description=walk-blocker on {display_name}: report unbounded filesystem walks
Documentation=file://{prefix}/README.md

[Service]
Type=oneshot
# Reconcile Layer 1 first, so a tool installed today is wrapped today, a
# tool removed today stops being shadowed, a hook block a package upgrade
# took is reported, and the audit directory's mode is re-asserted.
#
# `-` and a bounded `timeout`, and neither is decoration. systemd runs
# ExecStart only after every ExecStartPre WITHOUT a `-` exits successfully,
# so without the `-` any of the refusals reachable from install.sh's relink
# arm -- an untrusted prefix chain, a non-root payload, a symlinked
# wrapped_names.sh -- stopped the reaper from running at all. That is an
# availability inversion: the layer that exists BECAUSE Layer 1 is
# bypassable was made to depend on Layer 1's symlink bookkeeping, and the
# reaper's absence looks exactly like "no findings".
#
# The `-` ignores a non-zero EXIT and not a hang, so the bound is explicit --
# and it is `--kill-after`, not a bare timeout: plain `timeout N cmd` sends
# TERM at the deadline and then waits for the child to actually exit, so a
# relink not currently able to act on TERM would still hang past N. Neither
# can save a relink wedged in an uninterruptible syscall; the mitigating
# fact is that install.sh's own I/O is kept off the filesystem under
# investigation by construction, since every path it touches is local.
#
# The refusal is not surfaced as a unit failure because that channel is
# spoken for: ExecStart's own exit 1 means a new actionable finding.
# install.sh still exits 3 by hand and reports the refusal to
# `journalctl -t walk-blocker`, so ignoring it here does not make it silent.
ExecStartPre=-{timeout} --kill-after={kill_after} {relink_timeout} {sh} {prefix}/shim/install.sh --relink
# NOT `-` prefixed, and that asymmetry is the point: the reaper's own exit
# status has to reach the unit.
ExecStart={python3} {prefix}/reaper.py --report --spool {spool}
# FOUR exit codes, and three of them fail the unit:
#
#   0  nothing new, or nothing new that anyone could act on
#   1  a new ACTIONABLE finding
#   2  BLIND: no user slice, or /proc unreadable. The tool cannot see.
#   3  under --kill only: a kill was attempted and the process is not
#      known to be gone. Recurs every poll it recurs; never latched.
#
# A non-zero exit already fails a Type=oneshot unit, and `SuccessExitStatus=`
# is ADDITIVE to a success set that always contains 0 -- so the next line
# restates the default. Kept because an alerting contract is worth saying
# out loud; it is not the lever, and exempting a code would mean listing it.
SuccessExitStatus=0
# Explicit, not systemd's default, because this unit is the one that will
# eventually run `--kill`, and that path's worst case is the reaper's kill
# budget plus the reconcile bound above. The site's value is checked against
# that floor at build (`walk-blocker validate`).
TimeoutStartSec={timeout_start}
"""

UNIT_TIMER = """\
[Unit]
Description=walk-blocker poll on {display_name}

[Timer]
# Chosen at the site against the node's live schedule (see deploy.py).
OnCalendar={slot}
# Jitter would walk the chosen slot back into its neighbours, and a
# deterministic timer makes the audit trail correlatable: "did the reaper
# run during that event?" is a question a smeared timer cannot answer.
RandomizedDelaySec={randomized_delay}
# AccuracySec exists so systemd can COALESCE a wakeup with its neighbours;
# on a node where the point of the slot is to NOT be simultaneous with the
# neighbours, the default would undo the choice above.
AccuracySec={accuracy}
Persistent={persistent}

[Install]
WantedBy=timers.target
"""


def render_units(prefix, spool):
    """(service text, timer text) from the compiled constants. ONE function
    for both the preview and the install, so the preview cannot describe a
    unit the install then writes differently."""
    service = UNIT_SERVICE.format(
        display_name=DISPLAY_NAME, prefix=prefix, spool=spool,
        timeout=TRUSTED_TIMEOUT, kill_after=RELINK_KILL_AFTER_S,
        relink_timeout=RELINK_TIMEOUT_S, sh=TRUSTED_SH, python3=TRUSTED_PYTHON3,
        timeout_start=TIMEOUT_START_SEC)
    timer = UNIT_TIMER.format(
        display_name=DISPLAY_NAME, slot=TIMER_SLOT,
        randomized_delay=TIMER_RANDOMIZED_DELAY_SEC, accuracy=TIMER_ACCURACY_SEC,
        persistent="true" if TIMER_PERSISTENT else "false")
    return service, timer


def _is_root():
    return os.geteuid() == 0


# The ways `unowned_by()` can refuse. The CODE is what a caller switches on;
# the reason is prose for the operator and nothing reads it. Splitting them
# is the point: before this, `_UNOWNED_CLASSES` matched the English, so
# rewording a message here moved a path into a different blocker class --
# and a different remedy -- in a function that never mentions it.
UNOWNED_UNINSPECTABLE = "uninspectable"
UNOWNED_FOREIGN_UID = "foreign_uid"
UNOWNED_SELF_SYMLINK = "self_symlink"
UNOWNED_ESCAPING_SYMLINK = "escaping_symlink"
UNOWNED_UNRESOLVABLE_SYMLINK = "unresolvable_symlink"
UNOWNED_PERMISSIVE_MODE = "permissive_mode"
UNOWNED_SETUID = "setuid_bit"

# Every code `unowned_by()` can emit. `_UNOWNED_CLASSES` must cover all of
# them; a test asserts it, and `_unowned()` refuses one that is missing.
UNOWNED_CODES = (
    UNOWNED_UNINSPECTABLE,
    UNOWNED_FOREIGN_UID,
    UNOWNED_SELF_SYMLINK,
    UNOWNED_ESCAPING_SYMLINK,
    UNOWNED_UNRESOLVABLE_SYMLINK,
    UNOWNED_PERMISSIVE_MODE,
    UNOWNED_SETUID,
)

# A named 3-tuple, so `sorted(set(...))` still works and a caller can say
# `offender.reason` instead of indexing into prose.
Unowned = collections.namedtuple("Unowned", "path code reason")


def _unowned(path, code, reason):
    """One constructor for every offender, so a code with no blocker class
    cannot reach a caller. Raising here is deliberate: an unclassifiable
    refusal is a bug in this file, not a state of the filesystem, and the
    install is refusing either way."""
    if code not in _UNOWNED_CLASSES:
        raise AssertionError(
            "unowned_by(): %r has no entry in _UNOWNED_CLASSES" % (code,))
    return Unowned(path, code, reason)


def unowned_by(root, uid=0):
    """Paths under `root` not owned by `uid`, or otherwise still reachable.

    ADR-0004 rests on one property: the deployed artifacts are out of reach
    of the account being monitored. `cp -a` preserving the source's
    ownership silently took that property away -- and one of the files it
    copies, shim/install.sh, is what the systemd unit runs AS ROOT on every
    poll. So the property is checked rather than assumed, the same way
    install.sh verifies the hook fires rather than reporting on having
    written it. Returns the offenders, sorted, not a bool: the operator has
    to be told which path.

    Three ways a check like this fails to mean anything:

    * **Failing open.** `os.walk` swallows a directory it cannot list unless
      asked. A tree it could not inspect must never come back clean.
    * **Symlinks.** `cp -a` copies a symlink as a symlink, `chown -R` uses
      lchown so only the link is reassigned, and `chmod -R` skips symlinks.
      A link whose target resolves outside `root` is an offender; systemd
      executes the target.
    * **The target of an in-tree link** still has to satisfy the same test,
      which it does: the walk reaches it separately.

    Each offender is an `Unowned(path, code, reason)`: callers classify on
    the code, and print the reason.
    """
    bad = []
    real_root = os.path.realpath(root)
    if not os.path.isdir(root) or os.path.islink(root):
        # `os.walk` yields nothing for a file, so a file passed silently --
        # which would have exempted every entry that goes in as a file.
        try:
            info = os.lstat(root)
        except OSError as exc:
            return [_unowned(root, UNOWNED_UNINSPECTABLE,
                             "could not be inspected: %s" % exc.strerror)]
        if info.st_uid != uid:
            return [_unowned(root, UNOWNED_FOREIGN_UID,
                             "owned by uid %d" % info.st_uid)]
        if stat.S_ISLNK(info.st_mode):
            return [_unowned(root, UNOWNED_SELF_SYMLINK,
                             "the installed entry is itself a symlink -> %s"
                             % os.path.realpath(root))]
        if info.st_mode & 0o022:
            return [_unowned(root, UNOWNED_PERMISSIVE_MODE,
                             "mode %04o is writable beyond its owner"
                             % (info.st_mode & 0o7777))]
        return []

    def note_walk_error(exc):
        # A directory that cannot be listed is an unverifiable subtree, not
        # an empty one.
        bad.append(_unowned(getattr(exc, "filename", root) or root,
                            UNOWNED_UNINSPECTABLE,
                            "could not be inspected: %s" % exc.strerror))

    for base, dirnames, filenames in os.walk(root, onerror=note_walk_error):
        for name in [""] + dirnames + filenames:
            path = os.path.join(base, name) if name else base
            try:
                info = os.lstat(path)
            except OSError as exc:
                bad.append(_unowned(path, UNOWNED_UNINSPECTABLE,
                                    "could not be inspected: %s"
                                    % exc.strerror))
                continue
            if info.st_uid != uid:
                bad.append(_unowned(path, UNOWNED_FOREIGN_UID,
                                    "owned by uid %d" % info.st_uid))
                continue
            if stat.S_ISLNK(info.st_mode):
                # The link's own mode is meaningless (lrwxrwxrwx always), and
                # chmod -R never touched it. What matters is where it points.
                try:
                    target = os.path.realpath(path)
                except OSError as exc:
                    bad.append(_unowned(path, UNOWNED_UNRESOLVABLE_SYMLINK,
                                        "target could not be resolved: %s"
                                        % exc.strerror))
                    continue
                if target != real_root and not target.startswith(
                        real_root + os.sep):
                    bad.append(_unowned(path, UNOWNED_ESCAPING_SYMLINK,
                                        "symlink escapes the prefix -> %s"
                                        % target))
                continue
            if info.st_mode & 0o022:
                bad.append(_unowned(path, UNOWNED_PERMISSIVE_MODE,
                                    "mode %04o is writable beyond its owner"
                                    % (info.st_mode & 0o7777)))
            elif info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                # The copy no longer preserves ownership, so a setuid bit
                # carried over from the source would sit on a ROOT-owned
                # inode. Inert on the scripts this installs, but not a
                # property to leave unasserted in a tree root executes from.
                bad.append(_unowned(path, UNOWNED_SETUID,
                                    "mode %04o is setuid or setgid"
                                    % (info.st_mode & 0o7777)))
    return sorted(set(bad))


def run(cmd, check=True, capture=True, dry_run=False, env=None):
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        print("would run: %s" % printable)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    result = subprocess.run(cmd, capture_output=capture, text=True, env=env)
    if check and result.returncode != 0:
        sys.stderr.write("failed: %s\n%s\n" % (printable, result.stderr))
        raise SystemExit(result.returncode)
    return result


def system_preview(args, env=None):
    # A LOCAL canonical spelling, not a rewrite of `args`: preflight() runs
    # not_a_default() on the values as given, exactly as the install does,
    # and a preview that canonicalized them first would report a value the
    # caller never passed.
    prefix = canonical_prefix(args.prefix)
    # No per-value notes here: the preview used to warn about arguments an
    # operator could pass. The preview and the install now read the same
    # literals, which is a stronger guarantee than agreeing about a value
    # each was handed separately.
    print(__doc__.strip())
    print()
    result = run([TRUSTED_SH, os.path.join(REPO, "shim", "install.sh"), "--system"],
                 check=False, env=env)
    # A failing child preview is a real answer, not noise. install.sh checks
    # FILESYSTEM state that a literal cannot make true by construction --
    # whether a hook file is a symlink on this node, for one -- so its
    # refusal means the install would refuse too. Swallowing its stderr and
    # returning 0 here would print a copy-pastable approved command
    # underneath a blocker.
    if result.returncode != 0:
        sys.stderr.write(result.stderr or "")
        sys.stderr.write(
            "deploy.py: the installer's own preview refused (exit %d), so "
            "--i-have-approval\n  would refuse too. Fix the condition above "
            "before deploying; no command is\n  advertised here because none "
            "would work.\n" % result.returncode)
        return 6
    # install.sh's standalone `--system --i-have-approval` command is correct
    # advice when install.sh is run by hand and WRONG to relay here: run
    # directly it copies nothing, re-owns nothing, installs no Layer 2, and
    # links every shim back at this directory -- so its owner can later edit
    # code every account executes. Only deploy.py's own command is
    # advertised. The filter matches a COMMAND-shaped line, not any prose
    # mentioning both; the looser test once cut a sentence in half.
    for line in (result.stdout or "").splitlines():
        stripped = line.lstrip("# ").strip()
        if (stripped.startswith(("sh ", "sh\t"))
                and "install.sh" in stripped
                and "--i-have-approval" in stripped):
            print("#   (install.sh's standalone command is omitted here: run")
            print("#   directly it skips the copy, the chown and Layer 2, and")
            print("#   points the shims at this directory. Use deploy.py.)")
            continue
        print(line)
    spool = canonical_prefix(args.spool_dir)
    print("# The payload is copied to %s, then reasserted as root-owned --" % prefix)
    print("# per installed entry, never recursively over the prefix itself:")
    for cmd in ownership_commands(prefix):
        print("#   %s" % " ".join(shlex.quote(c) for c in cmd))
    print("# The copies pass --no-preserve=ownership, so every destination inode")
    print("# is root-owned from creation rather than inheriting this directory's")
    print("# owner; the chown above is a reassertion, not the first line of")
    print("# defence. The install then refuses to write a unit or enable a timer")
    print("# over a payload it could not make root-owned -- the systemd unit")
    print("# runs shim/install.sh as root on every poll. See ADR-0004.")
    print()
    print("# The audit directory is created root-owned and world-READABLE,")
    print("# before install.sh runs and again on every poll:")
    print("#   install -d -m 0755 %s" % shlex.quote(spool))
    print("# 0755, not 0750: ADR-0004 requires the trail not be WRITABLE by a")
    print("# monitored account, which root ownership and no group/other w")
    print("# carry. Unreadable was never the decision, and it would hide the")
    print("# trail from the account that has to decide --kill (ADR-0012).")
    repairs = spool_mode_repairs(spool)
    if repairs:
        print("# Spool files this install will tighten -- a DEFAULT ACL on the")
        print("# audit directory suppresses the umask at file creation, so a")
        print("# file written there can land group-writable no matter what")
        print("# umask the writer ran under:")
        for path, mode in repairs:
            print("#   chmod go-w %s   # currently %04o"
                  % (shlex.quote(path), mode))
        print()
    print("# And the root-run reaper, as a system timer under %s. The two" % args.unit_dir)
    print("# units below are rendered from the same constants the install writes:")
    service, timer = render_units(prefix, spool)
    for name, text in ((SERVICE_UNIT, service), (TIMER_UNIT, timer)):
        print("# --- %s ---" % os.path.join(args.unit_dir, name))
        print(text.rstrip("\n"))
    print()
    # Every check the install runs, from the same function -- so a preview
    # cannot advertise a command that is going to refuse. install.sh's own
    # refusal is already handled above on exactly this reasoning, and this
    # is the same reasoning applied to deploy.py's own checks: four of them
    # lived only in the install, and each let a misconfigured node preview
    # clean and then fail the install after the timer had been stopped --
    # the staging parent latest of all, between preflight() and the first
    # systemctl.
    #
    # `privileged` is asked, not assumed: this command is documented to be
    # run as root first, and is also perfectly runnable by the operator as
    # themselves. A root preview can see everything the install will, so for
    # it "could not check" is the refusal it is for the install.
    rc, checks = preflight(args, privileged=_is_root())
    if rc != 0:
        sys.stderr.write(
            "deploy.py: --i-have-approval would refuse too, so no command is "
            "advertised here.\n")
        return rc

    # Never silently: a check nobody could make is not a check that passed,
    # and the whole contract of this preview is that reading it is enough.
    unknown = [check for check in checks if check.state == CHECK_UNKNOWN]
    if unknown:
        print()
        print("# NOT CHECKED. This preview is running as a user who may not")
        print("# inspect these paths, so the following were not made -- which")
        print("# is not the same as made and passed. Re-run the preview as")
        print("# root to make them before approving:")
        for check in unknown:
            print("#   %s (%s): could not be checked as this user -- %s"
                  % (check.subject, check.name, check.reason))
        print()

    print("# Run as root with --i-have-approval to actually install:")
    # No path flags to forward, and that is the fix rather than a
    # simplification of it: keeping a forwarded flag list in sync with the
    # install was the bug. The payload path is still quoted, since a
    # directory with a space in it makes the line unrunnable.
    cmd = [TRUSTED_PYTHON3, os.path.join(REPO, "deploy.py"),
           "--system", "--i-have-approval"]
    print("#   %s" % " ".join(shlex.quote(c) for c in cmd))
    return 0


def canonical_prefix(prefix):
    """Absolute, single-leading-slash, normalized form of a path.

    `os.path.normpath` preserves EXACTLY two leading slashes -- POSIX lets an
    implementation treat `//foo` as special -- so `//var/tmp` normalizes to
    itself and compares unequal to `/var/tmp`. That was enough to walk past
    every check below: the chain walker builds its path from a single `/`,
    so the prefix never matched its own target string and the sticky
    exemption meant for ancestors was granted to the prefix itself.
    `search_rules.clean_path` collapses these for the same reason.
    Canonicalize ONCE, early, and let every derived path inherit it.
    """
    return re.sub(r"/{2,}", "/",
                  os.path.normpath(os.path.abspath(prefix))) or "/"


def _untrusted(path, trusted_uids=(0,), sticky_is_enough=True):
    """Why `path` is not a trusted directory for root to execute out of, or None.

    `sticky_is_enough` is the difference between an ancestor and the prefix
    itself. On an ancestor the sticky bit is sufficient: others may create
    entries in /tmp but cannot rename or delete the one we care about. On
    the PREFIX it is not, because the danger there is others creating
    entries -- any user could plant a payload marker to authorize a root
    `rm -rf` of a sibling.
    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        return "could not be inspected: %s" % exc.strerror
    if stat.S_ISLNK(info.st_mode):
        return "is a symlink, so its target can be changed underneath us"
    if info.st_uid not in trusted_uids:
        return "owned by uid %d" % info.st_uid
    mode = info.st_mode & 0o7777
    if info.st_mode & 0o022:
        if sticky_is_enough and (info.st_mode & stat.S_ISVTX):
            return None
        if info.st_mode & stat.S_ISVTX:
            # Factual only. Why sticky is not enough depends on the role this
            # path plays, so the caller appends that.
            return ("mode %04o is writable by group or other, sticky bit "
                    "notwithstanding" % mode)
        return ("mode %04o is writable by group or other, so its entries can "
                "be replaced" % mode)
    return None


def untrusted_prefix_chain(prefix, trusted_uids=(0,)):
    """Every directory from `/` down to `prefix` that root should not trust.

    Checking the payload is not enough: if any ancestor of the prefix is
    writable by someone else, they can swap the whole directory out AFTER the
    ownership check passes and before -- or between -- the timer's
    `ExecStartPre`, which runs shim/install.sh as root. Ownership of the leaf
    says nothing about who can rename the path to it.

    Components that do not exist yet are fine: this install creates them, and
    their parent has already been judged. The sticky bit exempts a shared
    directory like /tmp, which is what it is for.

    `trusted_uids` is the seam, not a uid substitution: real ancestors like
    `/` are legitimately root-owned, so swapping root for the test user the
    way `unowned_by`'s tests do would flag the whole chain. Tests pass "root
    or me"; production passes the default, root alone.
    """
    bad = []
    target = canonical_prefix(prefix)
    walked = os.sep
    deepest_existing = None
    for part in target.split(os.sep):
        if part:
            walked = os.path.join(walked, part)
        # lexists, not exists: exists() FOLLOWS symlinks, so a dangling link
        # component read as "missing" and never reached _untrusted()'s
        # symlink rejection. A root-owned link into a monitored user's
        # directory is a link whose target that user can create after this
        # check -- and `install -d` plus the copies follow it straight out
        # of the trusted prefix.
        if not os.path.lexists(walked):
            # The rest does not exist yet, so this install creates it. The
            # sticky bit does NOT make that safe: it protects existing
            # entries and does not reserve a missing NAME, so another user
            # can create `walked` between this check and `install -d` -- as
            # a symlink, at which point the copies write outside the prefix
            # entirely. A to-be-created prefix therefore demands a parent
            # nobody else can write at all.
            if deepest_existing is not None:
                why = _untrusted(deepest_existing, trusted_uids,
                                 sticky_is_enough=False)
                if why:
                    bad.append((deepest_existing,
                                "%s, and %s does not exist yet, so the name "
                                "can be taken before it is created"
                                % (why, walked)))
            break
        why = _untrusted(walked, trusted_uids,
                         sticky_is_enough=(walked != target))
        if why:
            # Keyed off the message, not a second stat: `walked` may be a
            # dangling symlink now that lexists() lets those through, and
            # os.stat would follow it and raise.
            if walked == target and "sticky bit notwithstanding" in why:
                why += (" -- and this is the prefix itself, where the risk is "
                        "others CREATING entries in it")
            bad.append((walked, why))
        deepest_existing = walked
    return bad


def ownership_commands(prefix):
    """The recursive commands the install issues, in the order it issues them.

    One source for both the preview and the execution. They drifted once in
    the predecessor: the preview printed `chown -R root:root $prefix` while
    the execution had been scoped to INSTALLED_ENTRIES, so the preview
    advertised exactly the foot-gun the implementation avoids.
    """
    commands = []
    for entry in INSTALLED_ENTRIES:
        target = os.path.join(prefix, entry)
        commands.append(["chown", "-R", "root:root", target])
        # `a-s` before the rest: mode bits copied from the source can include
        # setuid, and on a now-root-owned inode that is setuid ROOT. Inert on
        # the scripts this installs -- Linux ignores it on interpreted files
        # -- but cheaper to remove than to reason about.
        commands.append(["chmod", "-R", "a-s", target])
        # `a+rX,go-w` SETS the mode rather than only removing bits. `go-w`
        # alone preserved whatever the source had: a payload unpacked under
        # `umask 077` arrives at 0700, unowned_by() accepts it (root-owned,
        # not group/other writable), the hook verifies pass because they run
        # as root -- and then every user has a shim on PATH that none of
        # them can execute. `+X` adds execute only where it already exists
        # or on directories, so guard.sh stays executable and
        # search_rules.py does not become one.
        commands.append(["chmod", "-R", "a+rX,go-w", target])
    return commands


def untraversable_for_users(prefix):
    """Ancestors of `prefix` that an ordinary user cannot traverse.

    Trusted is not the same as reachable. A prefix under root's home is
    root-owned, not group/other-writable, and passes every check in
    validate_root_write_paths() -- and the root-run hook verifies pass too,
    because root can traverse it. Ordinary users cannot, so their shells put
    an unreachable directory on PATH and Layer 1 is silently absent for
    everyone it exists for. The same shape as a 0700 payload, one level up.

    Applies to the prefix and to the SPOOL's chain: ADR-0012 records that
    the audit trail must be readable by the people who have to decide
    `--kill`. The unit directory stays out: it is the distribution's 0755
    and this installer does not own it.
    """
    bad = []
    walked = os.sep
    for part in canonical_prefix(prefix).split(os.sep):
        if part:
            walked = os.path.join(walked, part)
        if not os.path.lexists(walked):
            break
        try:
            mode = os.stat(walked).st_mode
        except OSError as exc:
            bad.append((walked, "could not be inspected: %s" % exc.strerror))
            continue
        if not mode & 0o001:
            bad.append((walked, "mode %04o has no o+x, so no ordinary user "
                                "can traverse it" % (mode & 0o7777)))
    return bad


# What `unowned_by()` can report, mapped to a class that has a TRUE head and
# a remedy that works. Keyed on the CODE, never on the prose: two codes can
# share a class (both symlink shapes want the same remedy) but a rewritten
# sentence must not move one. A code with no entry is a bug `_unowned()`
# raises on; a code from somewhere else falls to "unclassified", which
# still refuses.
_UNOWNED_CLASSES = {
    UNOWNED_FOREIGN_UID: "ownership",
    UNOWNED_PERMISSIVE_MODE: "permissive",
    UNOWNED_SETUID: "setuid",
    UNOWNED_SELF_SYMLINK: "symlink",
    UNOWNED_ESCAPING_SYMLINK: "symlink",
    UNOWNED_UNINSPECTABLE: "unreadable",
    UNOWNED_UNRESOLVABLE_SYMLINK: "unreadable",
}


def _classify_unowned(code):
    return _UNOWNED_CLASSES.get(code, "unclassified")


def installer_owned_spool_files(spool):
    """The files in the spool that THIS INSTALLER writes, or whose writer it
    installs. Their mode is the installer's to assert, exactly as the spool
    directory's own mode is -- and for the identical reason
    audit_dir_blockers() excludes the spool's own mode from the traversal
    check: refusing to install because one of these is wrong turns the state
    this code exists to correct into a refusal to correct it.

    A spool carrying a DEFAULT ACL suppresses the umask at file creation, so
    a file written there can land group-writable whatever umask the writer
    ran under. reaper.py chmods both files it writes, so this is the second
    line of defence, not the only one -- but the ACL applies to any file
    created there, including one written by a build older than that fix. The
    `.tmp` and the `.1` rotation are on the list for that reason: no call
    writes either name deliberately, and either can be orphaned at the wrong
    mode by an older build.
    """
    if not spool or spool == os.sep:
        return ()
    return tuple(os.path.join(spool, name) for name in (
        DEFAULT_AUDIT_FILENAME,       # Layer 1's trail
        "reaper-state.json",          # Layer 2's latch
        "reaper-state.json.tmp",      # ...and the latch's write-and-rename
        "reaper-audit.jsonl",         # Layer 2's trail
        "reaper-audit.jsonl.1",       # ...and its one rotation
    ))


def spool_mode_repairs(spool):
    """(path, mode) for installer-owned spool files that are too permissive.

    ONE function, called by audit_dir_blockers() to decide what not to refuse
    over and by both install and preview to decide what to advertise -- so a
    file cannot be a blocker in the preview and a chmod in the install. Not
    a keyword argument on audit_dir_blockers(): a caller that forgets the
    keyword gets the regression back silently.

    Too permissive ONLY. A file that is too restrictive is not repaired here
    and not refused either -- the same asymmetry the directory has.
    """
    repairs = []
    for path in installer_owned_spool_files(spool):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        # lstat, and skip a symlink: chmod would follow it and change the
        # mode of whatever it points at. That case stays a blocker, and
        # `symlink` is already its own refusal class with its own remedy.
        if stat.S_ISLNK(info.st_mode):
            continue
        mode = stat.S_IMODE(info.st_mode)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            repairs.append((path, mode))
    return repairs


# `stat`'s type predicates, in the order a spool is plausibly wrong: named
# so the refusal says what the thing IS rather than only what it is not.
_FILE_KINDS = (
    (stat.S_ISREG, "a regular file"),
    (stat.S_ISFIFO, "a fifo"),
    (stat.S_ISSOCK, "a socket"),
    (stat.S_ISCHR, "a character device"),
    (stat.S_ISBLK, "a block device"),
)


def _not_a_directory_reason(path):
    """Why `path` cannot serve as the audit directory, given that it exists
    and is not one. lstat, so a symlink is described as the link it is
    rather than as whatever it happens to resolve to."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        return "could not be inspected: %s" % exc.strerror
    if stat.S_ISLNK(info.st_mode):
        target = os.path.realpath(path)
        if not os.path.exists(path):
            # Not "it would create the target": measured, GNU coreutils
            # 8.32, `install -d` on a dangling link exits 1 with "cannot
            # change permissions of ..." and creates nothing. The blocker is
            # the same; what it costs is the install, mid-run.
            return ("is a dangling symlink -> %s, and the install's "
                    "`install -d` fails on it, which would abort the "
                    "install after the timer had been stopped" % target)
        return "is a symlink -> %s, which is not a directory" % target
    for predicate, name in _FILE_KINDS:
        if predicate(info.st_mode):
            return "is %s, not a directory" % name
    return "is not a directory (mode %06o)" % info.st_mode


def audit_dir_blockers(spool):
    """Filesystem state that would make the audit trail unusable, or unsafe.

    Shared by the preview and the install ON PURPOSE: a check that lives in
    only one of the two re-creates the preview describing a command the
    install then refuses, and two checks that agree today are not a
    guarantee -- one function both call is.

    Returns (cls, path, reason) triples, empty when there is nothing wrong.
    `cls` is what write_audit_dir_refusal() selects wording and remedy from
    -- carried in the data rather than re-derived from the path, because
    `unowned_by()` walks UNDER its root and can name a child of the spool.

    THE MODE IS A REFUSAL GROUND IN ONE DIRECTION ONLY (ADR-0012):

    * Too RESTRICTIVE -- 0750 -- is not a blocker. `install -d -m 0755`
      asserts the mode and the relink re-asserts it every poll, so finding
      it wrong is finding the thing about to be fixed. That is why the
      traversal check runs over the spool's ANCESTORS and not the spool: an
      ancestor is somebody else's directory and nothing here will fix it,
      while the spool's own mode is this installer's to set.
    * Too PERMISSIVE -- group- or other-writable, or setuid/setgid -- IS a
      blocker, because ADR-0004's property is that the trail is not writable
      by the account being monitored, and a 0777 directory plainly is.

    One class per reason `unowned_by()` can give, because they do not share
    a remedy: a `chown` does nothing for a mode, and a `chmod` does nothing
    for a symlink that leaves the tree.
    """
    if not spool or spool == os.sep:
        # Early, and NOT falling through: untraversable_for_users('') and
        # unowned_by('/') would each answer a question nobody asked.
        return [("degenerate", spool, "is not a usable spool for the "
                                      "reaper's state files")]
    # ANCESTORS ONLY -- see above. Canonical to canonical:
    # `untraversable_for_users()` emits components built from
    # `canonical_prefix()`, and a raw spelling of the same directory would
    # let the leaf survive the filter and bring the regression back
    # SILENTLY, because the symptom is a refusal that looks correct.
    spool_canon = canonical_prefix(spool)
    bad = [("traversal", path, reason)
           for path, reason in untraversable_for_users(spool)
           if path != spool_canon]
    # THE SPOOL'S OWN TYPE, before either arm below is asked what is in it.
    # Both arms look straight past a spool that exists as a non-directory:
    # the traversal arm drops the leaf on purpose (its mode is this
    # installer's to assert) and the ownership arm never runs unless the
    # leaf is a directory. So a spool that is a regular file, a fifo, or a
    # link to either previewed CLEAN, and `install -d` in install.sh's
    # assert_audit_dir then failed mid-install -- after the timer had
    # already been disabled and stopped.
    #
    # A dangling symlink is in this class deliberately rather than treated
    # as absent: `install -d` follows it, so it would create the TARGET,
    # somewhere this installer never judged and the reaper's trail is not
    # where the units say it is. A symlink to a real DIRECTORY is not here,
    # because `unowned_by()` below already reports it as the `symlink`
    # class, with its own head and its own reason.
    if os.path.lexists(spool_canon) and not os.path.isdir(spool_canon):
        bad.append(("not-a-directory", spool_canon,
                    _not_a_directory_reason(spool_canon)))
    if os.path.isdir(spool):
        # A file this installer is about to chmod is not a reason to refuse
        # to run the installer. `permissive` only: every other class
        # (ownership, symlink, setuid, unreadable) still blocks on these
        # paths, because `chmod go-w` does not fix any of them.
        repairable = set(path for path, _mode in spool_mode_repairs(spool))
        for path, code, reason in unowned_by(spool):
            cls = _classify_unowned(code)
            if cls == "permissive" and path in repairable:
                continue
            bad.append((cls, path, reason))
    return bad


# Per class: the sentence that says what went wrong, and the command that
# fixes it. `None` means there is no one correct command -- printing a
# plausible-looking one anyway runs clean and fixes nothing.
_REFUSAL_HEADS = {
    "degenerate": "%r is not a usable spool for the reaper's state files",
    "not-a-directory": "it already exists and is not a directory, so "
                       "`install -d` would fail and abort the install "
                       "mid-way -- after the timer has already been "
                       "disabled and stopped",
    "traversal": "an ancestor of it cannot be traversed by an ordinary user, "
                 "so the audit trail would be unreadable by the account "
                 "that has to decide --kill",
    "ownership": "it is not root's alone, so its owner could replace the "
                 "audit trail",
    "permissive": "it is writable beyond root, so the account being "
                  "monitored could rewrite its own audit trail -- the one "
                  "property ADR-0004 rests on",
    "setuid": "it carries a setuid or setgid bit, which has no business on "
              "a trail root writes and everyone else only reads",
    "symlink": "it is reached through a symlink, so what root owns is the "
               "link and not what the reaper actually writes to -- neither "
               "chown nor chmod reaches through it",
    "unreadable": "it could not be inspected, and an unverifiable subtree is "
                  "not an empty one",
    "unclassified": "it was refused by the ownership check for a reason this "
                    "message does not recognise, which is itself worth "
                    "reporting rather than swallowing",
}

_REFUSAL_REMEDIES = {
    "ownership": "chown root:root",
    "permissive": "chmod go-w",
    # a-s, matching ownership_commands()'s `chmod -R a-s`: the remedy handed
    # to an operator should be the one the installer would have run.
    "setuid": "chmod a-s",
}


def write_audit_dir_refusal(spool, bad, out=sys.stderr):
    """The one message for audit_dir_blockers(), so both callers say it the
    same way. Per CLASS, not one sentence for all of them, and not one
    remedy: a single generic remedy names a command that does not touch the
    problem for most of the things `unowned_by()` can report."""
    seen = []
    for cls, _path, _reason in bad:
        if cls not in seen:
            seen.append(cls)
    for cls in seen:
        head = _REFUSAL_HEADS[cls]
        if cls == "degenerate":
            out.write("deploy.py: refusing spool_dir=%s: %s.\n"
                      % (spool, head % spool))
            continue
        out.write("deploy.py: refusing spool_dir=%s: %s:\n" % (spool, head))
        offenders = [(path, reason) for bcls, path, reason in bad
                     if bcls == cls]
        for path, reason in offenders:
            out.write("  %s: %s\n" % (path, reason))
        remedy = _REFUSAL_REMEDIES.get(cls)
        if remedy:
            # The OFFENDING paths, never the spool: unowned_by() walks under
            # its root, so an offender is often a file inside the directory
            # and acting on the directory alone would leave it as it was.
            out.write("  fix with: %s %s\n"
                      % (remedy,
                         " ".join(shlex.quote(p) for p, _ in offenders)))
        # Every other class prints no `fix with:` line, deliberately. For
        # `traversal` the offender is an ancestor this installer does not own
        # and which one to widen is the operator's call; for `symlink`,
        # `unreadable` and `unclassified` there is no command that would be
        # right. `not-a-directory` is the sharpest case: the only command
        # that would "fix" it is `rm`, aimed by this script at a path
        # somebody put a file at on purpose, and an installer that
        # fabricates that line has advertised deleting a stranger's data.


# Which of the six paths is a directory and which is a file. Only that
# distinction survives from the predecessor's five-column table: the shape
# checks it also carried (absolute, unit-embeddable, shell-safe) are now
# properties of the literals, asserted by the schema at build and by one
# test over the compiled values.
PATH_KINDS = (
    ("prefix",         "dir"),
    ("spool_dir",      "dir"),
    ("unit_dir",       "dir"),
    ("bashrc_file",    "file"),
    ("zshenv_file",    "file"),
    ("fish_conf_file", "file"),
)


def validate_root_write_paths(args, attrs=None):
    """Check the filesystem STATE of every root-write path in `args`.

    Shared by install and uninstall, which is the point: the predecessor's
    uninstall validated only the prefix, so a hook file reached
    `strip_block()` unchecked. That function READS the file and replaces it
    with a root-created 0644 regular file -- so a planted symlink had its
    target's contents copied into a world-readable file.

    Whether the prefix's parent is user-writable on this node, or a hook file
    is a symlink under a dotfile manager, is a fact about the machine at
    install time. A literal path is not a trusted path.

    Returns 0, or 6 to be returned by the caller.
    """
    for attr, kind in PATH_KINDS:
        if attrs is not None and attr not in attrs:
            continue
        path = canonical_prefix(getattr(args, attr))
        setattr(args, attr, path)

        target = path if kind == "dir" else os.path.dirname(path)
        chain = untrusted_prefix_chain(target)
        if chain:
            sys.stderr.write("deploy.py: refusing %s (%s): the path is not "
                             "trusted end to end.\n" % (attr, target))
            for bad_path, reason in chain:
                sys.stderr.write("  %s: %s\n" % (bad_path, reason))
            return 6

        # A trusted parent still leaves the final component free to be a
        # symlink, and for a FILE that is the leak this validator claims to
        # prevent: strip_block() reads through the link, then the block is
        # written back as a 0644 regular file -- publishing a mode-0600
        # target's contents to every user on the node. No attacker is
        # needed: a system rc under a dotfile manager is a symlink in the
        # ordinary case.
        if kind == "file":
            bad = irregular_target(path)
            if bad is not None:
                sys.stderr.write(
                    "deploy.py: refusing %s %s: %s. This file is read and then "
                    "rewritten 0644;\n  through a link that publishes the "
                    "target's contents.\n" % (attr, path, bad))
                return 6
            # ...and the leaf's OWNERSHIP, mirroring install.sh. This file is
            # read and rewritten, and install.sh's verify functions then have
            # the real shell source it AS ROOT to prove the hook fires -- so
            # a user-owned or group-writable one lets its owner choose what
            # runs during the deploy. The trust chain above covers the
            # directory; this covers the leaf. Only when it already exists;
            # one this install creates is root's by construction.
            if os.path.exists(path):
                offenders = unowned_by(path)
                if offenders:
                    sys.stderr.write(
                        "deploy.py: refusing %s %s: %s. It is sourced as "
                        "root to verify\n  the hook fires, so its owner "
                        "would be choosing what runs during the deploy.\n"
                        % (attr, path, offenders[0].reason))
                    return 6
    return 0


# --------------------------------------------------------------------------
# preflight: every filesystem-state check, made once, by both callers
# --------------------------------------------------------------------------

# A check's three answers. The third is what earns this a vocabulary rather
# than a bool. The PREVIEW runs as whoever reads it, and a stat that user is
# not permitted to make is not evidence that the path is fine: reported as
# `ok` it is exactly the defect ADR-0005's contract forbids -- read the
# preview, then run the approved command, and the approved command must not
# refuse what the preview accepted -- and reported as `blocked` it is a
# refusal invented out of the reader's own uid. It is neither, and the only
# honest thing to print is that nobody looked.
CHECK_OK = "ok"
CHECK_BLOCKED = "blocked"
CHECK_UNKNOWN = "unknown"


class Check(object):
    """One preflight check's answer, and the path it is about."""

    __slots__ = ("name", "subject", "state", "reason")

    def __init__(self, name, subject, state, reason=None):
        self.name = name
        self.subject = subject
        self.state = state
        self.reason = reason

    def __repr__(self):
        return "Check(%r, %r, %r, %r)" % (self.name, self.subject,
                                          self.state, self.reason)


def unstattable_as_me(path):
    """(component, reason) for the first component of `path` this process is
    not PERMITTED to inspect, or None.

    Not "does it exist" and not "is it fit": whether the answer is available
    to THIS uid at all. ENOENT is an answer -- every check below already
    models a component that does not exist yet -- and a real I/O error is a
    fault that must stay a blocker rather than be excused as unchecked. Only
    EACCES and EPERM mean "ask someone with more privilege".

    Component by component from `/`, because a directory without `x` hides
    its children from `stat` while remaining perfectly stattable itself: the
    deepest component this user can reach is where the answer runs out.
    lstat, so a symlink is not followed out of the chain being judged.
    """
    walked = os.sep
    for part in canonical_prefix(path).split(os.sep):
        if part:
            walked = os.path.join(walked, part)
        try:
            os.lstat(walked)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                return (walked, exc.strerror)
            return None
    return None


def prefix_is_foreign(prefix):
    """Whether `prefix` is a populated directory this install did not create.

    Raises OSError when it cannot be read, which the caller turns into
    `unknown` or a refusal: `os.listdir` needs `r` where `stat` needs
    nothing, so this is the one check whose permission answer only arrives
    when it is attempted.
    """
    if not os.path.isdir(prefix) or os.path.exists(
            os.path.join(prefix, PAYLOAD_MARKER)):
        return False
    return bool(os.listdir(prefix))


def write_unreachable_prefix_refusal(prefix, blocked, out=None):
    """Trusted is not reachable, said once so both callers say it the same
    way -- the whole point of the preflight being one function."""
    out = sys.stderr if out is None else out
    out.write(
        "deploy.py: refusing prefix=%s: the users Layer 1 is for cannot "
        "reach it.\n" % prefix)
    for path, reason in blocked:
        out.write("  %s: %s\n" % (path, reason))
    out.write(
        "  install.sh's hook verifies would still pass, because they run\n"
        "  as root -- so this would install cleanly and leave Layer 1\n"
        "  absent for every monitored account.\n")


def write_foreign_prefix_refusal(prefix, out=None):
    """Somebody else's directory, said once, for the same reason."""
    out = sys.stderr if out is None else out
    out.write(
        "deploy.py: refusing prefix=%s: it already has contents and no %s "
        "marker,\n  so it is not a directory this install created. "
        "Installing would `rm -rf` and chown paths that\n  belong to "
        "something else. Use a dedicated directory.\n"
        % (prefix, PAYLOAD_MARKER))


def write_untrusted_staging_refusal(parent, chain, out=None):
    """Said once, so `stage_payload()` and the preflight say it the same
    way. The install still makes this check itself, right where it creates
    the directory; this is how the PREVIEW reaches the same sentence."""
    out = sys.stderr if out is None else out
    out.write(
        "deploy.py: refusing to stage the payload under %s: the path is "
        "not trusted\n  end to end, and a snapshot under a parent someone "
        "else can write is not a\n  snapshot.\n" % parent)
    for path, reason in chain:
        out.write("  %s: %s\n" % (path, reason))


def write_unusable_staging_refusal(parent, reason, out=None):
    """Nowhere to put the snapshot. `tempfile.mkdtemp(dir=...)` needs the
    parent to exist already and raises uncaught if it does not -- after
    every check has passed, which is the shape of refusal this preflight
    exists to move earlier. Refused rather than created: the parent's safety
    comes from something else having made it, and creating it here would be
    this script choosing the ownership and mode of the directory that is
    supposed to protect bytes nothing has verified yet."""
    out = sys.stderr if out is None else out
    out.write(
        "deploy.py: refusing to stage the payload under %s: it %s, so the "
        "root-only\n  snapshot the install makes before its first "
        "`systemctl` has nowhere to go.\n" % (parent, reason))


def write_unknown_refusal(unknowns, out=None):
    """A check root itself could not make. Returns 6, to be returned on.

    Unreachable in practice -- root is not subject to the permission bits
    `unstattable_as_me()` reads -- which is why it is written down rather
    than assumed away. The preview's honest "could not check as this user"
    has no counterpart with privilege: a check that could not be made is not
    a check that passed, and proceeding here would install over exactly the
    state nobody verified.
    """
    out = sys.stderr if out is None else out
    out.write(
        "deploy.py: refusing to install: a check could not be made even as "
        "root, and a\n  check that could not be made is not a check that "
        "passed:\n")
    for check in unknowns:
        out.write("  %s (%s): %s\n" % (check.subject, check.name,
                                       check.reason))
    return 6


def preflight(args, privileged, repair=None, out=None):
    """Every check `system_execute()` makes before its first `systemctl`, in
    the order it makes them -- run from ONE place, by both callers.

    `audit_dir_blockers()` makes the argument for one of these checks and it
    generalises to all of them: a check that lives in only one of the
    preview and the install re-creates the preview describing a command the
    install then refuses, and two checks that agree today are not a
    guarantee -- one function both call is. Four of these lived in only one
    caller, and each was its own report of the same symptom: an install that
    refuses after the timer has been disabled and stopped. The last of them,
    the staging parent, is also the only one a dry run does not make, because
    `stage_payload()` does not make it either -- a dry run creates no
    snapshot, so it has no parent to judge.

    `privileged` is a fact about the CALLER, not a mode. The install runs as
    root and can inspect everything; the preview runs as whoever reads it. A
    check whose stat this process may not make comes back `unknown`, which
    the preview REPORTS -- never as clean, and never as a blocker
    manufactured out of the reader's uid -- and which the install refuses.

    `repair` is the install's one asymmetry, named here rather than
    duplicated: it chmods its own spool files before judging them, because
    their mode is the state this code exists to correct, where the preview
    advertises the identical list from the identical function. It runs after
    the paths are judged and before the audit directory is, exactly where
    the install used to do it inline.

    Returns `(rc, checks)`: rc 0 to proceed, 6 to refuse, with any refusal
    already written to `out` by the same writer the other caller uses --
    which is what keeps the two head lines identical rather than similar.
    """
    # Resolved at CALL time, never frozen into a default: a default argument
    # captures the `sys.stderr` that existed at import, so a caller that
    # replaces the stream -- the suite does, and so does anything that wraps
    # this -- would have its refusals written somewhere it cannot see them.
    out = sys.stderr if out is None else out
    checks = []

    def unknown_so_far():
        return [check for check in checks if check.state == CHECK_UNKNOWN]

    # 0. The arguments, before anything reads a filesystem and before
    #    anything canonicalizes them, so a value is judged as given. Not a
    #    filesystem check -- it is ADR-0005's second guarantee, that the code
    #    will not write elsewhere even when a caller inside the module asks
    #    -- but it is a thing the install refuses on before its first
    #    command, so the preview refuses on it too.
    why = not_a_default(args)
    if why:
        out.write(why)
        checks.append(Check("arguments", None, CHECK_BLOCKED, why))
        return 6, checks
    checks.append(Check("arguments", None, CHECK_OK))

    # 1. The six root-write locations: a trusted chain end to end, and for
    #    the three hook FILES the leaf's type and ownership as well. Scoped
    #    to the paths this process can actually inspect, so an unprivileged
    #    preview still checks the five it can see rather than giving up on
    #    all six.
    inspectable = []
    for attr, _kind in PATH_KINDS:
        blind = unstattable_as_me(getattr(args, attr))
        if blind is None:
            inspectable.append(attr)
        else:
            checks.append(Check("paths", getattr(args, attr), CHECK_UNKNOWN,
                                "%s: %s" % blind))
    if privileged and unknown_so_far():
        return write_unknown_refusal(unknown_so_far(), out=out), checks
    if validate_root_write_paths(args, attrs=inspectable) != 0:
        checks.append(Check("paths", None, CHECK_BLOCKED))
        return 6, checks
    checks.append(Check("paths", None, CHECK_OK))

    # Canonical from here: validate_root_write_paths() rewrites each
    # attribute it was given, and canonical_prefix() is idempotent for the
    # rest, so this spelling is the one every check and message below uses.
    spool = canonical_prefix(args.spool_dir)
    prefix = canonical_prefix(args.prefix)

    # 2. The audit directory -- after the install has repaired the files
    #    whose mode is its own to assert.
    if repair is not None:
        repair(spool)
    blind = unstattable_as_me(spool)
    if blind is not None:
        checks.append(Check("spool", spool, CHECK_UNKNOWN, "%s: %s" % blind))
        if privileged:
            return write_unknown_refusal(unknown_so_far(), out=out), checks
    else:
        bad_spool = audit_dir_blockers(spool)
        if bad_spool:
            write_audit_dir_refusal(spool, bad_spool, out=out)
            checks.append(Check("spool", spool, CHECK_BLOCKED))
            return 6, checks
        checks.append(Check("spool", spool, CHECK_OK))

    # 3. The prefix: reachable by the ordinary users Layer 1 exists for...
    blind = unstattable_as_me(prefix)
    if blind is not None:
        for name in ("prefix_reach", "prefix_contents"):
            checks.append(Check(name, prefix, CHECK_UNKNOWN, "%s: %s" % blind))
        if privileged:
            return write_unknown_refusal(unknown_so_far(), out=out), checks
        return 0, checks
    blocked = untraversable_for_users(prefix)
    if blocked:
        write_unreachable_prefix_refusal(prefix, blocked, out=out)
        checks.append(Check("prefix_reach", prefix, CHECK_BLOCKED))
        return 6, checks
    checks.append(Check("prefix_reach", prefix, CHECK_OK))

    # 4. ...and not already somebody else's populated directory.
    try:
        foreign = prefix_is_foreign(prefix)
    except OSError as exc:
        checks.append(Check("prefix_contents", prefix, CHECK_UNKNOWN,
                            "%s: %s" % (prefix, exc.strerror)))
        if privileged:
            return write_unknown_refusal(unknown_so_far(), out=out), checks
        return 0, checks
    if foreign:
        write_foreign_prefix_refusal(prefix, out=out)
        checks.append(Check("prefix_contents", prefix, CHECK_BLOCKED))
        return 6, checks
    checks.append(Check("prefix_contents", prefix, CHECK_OK))

    # 5. The staging parent, LAST because that is where the install makes
    #    it: `stage_payload()` refuses on this chain between this function
    #    returning and the first `systemctl`, and it was the one
    #    pre-command refusal the preview could not reach. It keeps its own
    #    call, through the same writer -- belt and braces, the same order
    #    the spool repair argues for.
    #
    #    Skipped for a dry run, which is not a softening: `stage_payload()`
    #    returns before this check having created nothing, so a dry run
    #    refusing here would refuse to PLAN over a condition it never
    #    touches, and the plan it prints is the same either way.
    if not args.dry_run:
        blind = unstattable_as_me(STAGING_PARENT)
        if blind is not None:
            checks.append(Check("staging", STAGING_PARENT, CHECK_UNKNOWN,
                                "%s: %s" % blind))
            if privileged:
                return write_unknown_refusal(unknown_so_far(), out=out), checks
            return 0, checks
        # Missing is a BLOCKER, not an unknown: ENOENT is an answer
        # (`unstattable_as_me()` says so), and it is the errno
        # `tempfile.mkdtemp(dir=...)` would raise uncaught. Writability is
        # deliberately not asked -- root's cannot be tested without writing,
        # and the preview's own would be a blocker manufactured out of the
        # reader's uid, which is what the third state exists to avoid.
        if not os.path.isdir(STAGING_PARENT):
            why = ("is not a directory" if os.path.lexists(STAGING_PARENT)
                   else "does not exist")
            write_unusable_staging_refusal(STAGING_PARENT, why, out=out)
            checks.append(Check("staging", STAGING_PARENT, CHECK_BLOCKED, why))
            return 6, checks
        chain = untrusted_prefix_chain(STAGING_PARENT)
        if chain:
            write_untrusted_staging_refusal(STAGING_PARENT, chain, out=out)
            checks.append(Check("staging", STAGING_PARENT, CHECK_BLOCKED))
            return 6, checks
        checks.append(Check("staging", STAGING_PARENT, CHECK_OK))
    return 0, checks


def stage_payload(env=None, dry_run=False):
    """Snapshot the payload into a root-only directory and return its path.

    The destination is root-owned from creation and verified, but that
    authenticates the destination's METADATA, not the bytes that arrived --
    and the payload directory's owner can rewrite a source file after the
    operator started deploy.py and have the new bytes copied and then
    executed as root. Same late-read problem uninstall_helper() closes on
    the teardown side.

    Reading everything ONCE, as early as anything is read, narrows the
    window to the interval between Python loading deploy.py and this
    function FINISHING. Not microseconds, and not atomic: this spawns
    subprocesses per source and the directory copies are recursive walks,
    so an entry not yet reached can be rewritten while an earlier one is
    being read. What it buys is collapsing that window from the length of a
    `systemctl stop` down to one small copy, and making the executed bytes
    the copied bytes.

    Requiring a root-owned SOURCE tree to remove the window entirely was
    considered and REJECTED (ADR-0006): the payload is unpacked by a
    sysadmin into a folder that account owns, and the installation places
    the components under root control. The residual window is the same
    exposure the operator already accepts -- deploy.py's own bytes come out
    of the same user-owned directory, read by the same root process, one
    moment earlier. Do not re-open this as a hardening patch.

    0700 and owned by root, since it holds bytes not yet verified.
    """
    if dry_run:
        # A dry run must not create anything. It prints the commands it would
        # issue, so it reports the snapshot against REPO and the caller reads
        # from REPO too -- the plan is identical either way.
        return REPO
    # preflight() has already refused on this, through this same writer, so
    # in the install's own sequence this is the second look. Kept: the check
    # belongs where the root-only directory is created, and a guard held
    # only somewhere else is a guard that moves the next time the call order
    # does.
    chain = untrusted_prefix_chain(STAGING_PARENT)
    if chain:
        write_untrusted_staging_refusal(STAGING_PARENT, chain)
        raise SystemExit(6)
    staging = tempfile.mkdtemp(prefix="walk-blocker-stage.", dir=STAGING_PARENT)
    os.chmod(staging, 0o700)
    # atexit rather than a try/finally around the rest of system_execute():
    # every refusal there returns rather than raising, so a finally would
    # have to wrap the whole function, and this leaves the 0700 root-owned
    # snapshot cleaned up on every ordinary exit including SystemExit. A
    # kill -9 leaves it behind under STAGING_PARENT -- litter rather than a
    # disclosure: 0700 and root-owned, so nothing else can traverse it.
    atexit.register(shutil.rmtree, staging, True)
    # GENERATED into the snapshot, not copied out of REPO: the marker's
    # content is this build's version, and staging it here keeps the install
    # a flat sequence of `install` commands with no side-writes of its own
    # -- which is also what makes the whole sequence visible to --dry-run
    # and to the tests, both of which observe run().
    with open(os.path.join(staging, PAYLOAD_MARKER), "w") as fh:
        fh.write("%s\n" % __version__)
    for relative, is_dir, _mode in PAYLOAD_SOURCES:
        source = os.path.join(REPO, relative)
        target = os.path.join(staging, relative)
        run(["install", "-d", "-m", "0700", os.path.dirname(target)],
            dry_run=dry_run, env=env)
        if is_dir:
            run(["cp", "-a", "--no-preserve=ownership", source, target],
                dry_run=dry_run, env=env)
        else:
            run(["install", "-m", "0600", source, target],
                dry_run=dry_run, env=env)
    return staging


def uninstall_helper(prefix):
    """(path, None) for a helper safe to run as root, or (None, reason).

    The teardown runs the DEPLOYED copy of install.sh, which the install made
    root-owned and verified, and refuses rather than falling back to the
    payload directory -- a fallback is the very path being closed:
    deploy.py's bytes are fixed once Python has loaded it, while install.sh
    would be opened later and sources wrapped_names.sh later still, so the
    directory's owner can replace either in between. Both files are checked,
    because install.sh sources wrapped_names.sh into its own shell.
    """
    directory = os.path.join(prefix, "shim")
    helper = os.path.join(directory, "install.sh")
    for path in (helper, os.path.join(directory, "wrapped_names.sh")):
        bad = irregular_target(path)
        if bad is not None:
            return (None, "%s %s" % (path, bad))
        if not os.path.exists(path):
            return (None, "%s does not exist" % path)
        offenders = unowned_by(path)
        if offenders:
            return (None, "%s: %s" % (offenders[0].path,
                                      offenders[0].reason))
    chain = untrusted_prefix_chain(directory)
    if chain:
        return (None, "%s: %s" % chain[0])
    return (helper, None)


def irregular_target(path):
    """Why `path` is unfit to be rewritten in place, or None if it is fit.

    lstat, not stat: the point is to catch the link itself, including a
    dangling one, which `os.path.isfile` reports as absent and a later
    `open(..., "w")` would happily create as a regular file at the link's
    target instead of at `path`.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None  # Does not exist yet; it will be created as a regular file.
    except OSError as err:
        return "cannot be examined (%s)" % err.strerror
    if stat.S_ISLNK(info.st_mode):
        return "is a symlink"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    # NOT ownership: that goes through unowned_by() at the call site, which
    # is the seam the tests move. Keeping this type-only also keeps it usable
    # on a path that legitimately is not root-owned yet.
    return None


def write_unit(path, text):
    """Create or replace a unit file as 0644, refusing to follow a symlink.

    A plain `open(path, "w")` creates with 0666 & ~umask, so under a root
    shell with `umask 000` the unit systemd executes as root is one any user
    could edit. `fchmod` as well as the `os.open` mode, because O_CREAT's
    mode is ignored when the file already exists. And O_NOFOLLOW, so a unit
    pre-created as a symlink in a writable unit directory is an error rather
    than a silent truncation of whatever it points at.
    """
    # In place, not a fresh inode plus rename. O_TRUNC does not revoke a
    # write descriptor another user already holds -- but holding one needs
    # this directory to have been writable, which untrusted_prefix_chain()
    # refuses. Narrow enough not to buy a temp-file dance; recorded as
    # PARTIAL rather than silently over-built.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o644), "w") as handle:
        os.fchmod(handle.fileno(), 0o644)
        # Owner as well as mode. fchmod alone leaves an EXISTING unit file
        # with whatever owner it had, and an ordinary owner can chmod their
        # own 0644 file afterwards and edit a service systemd runs as root.
        #
        # EPERM is tolerated only for a non-root caller, which in practice
        # means a test: system_execute() refuses to run without root, and a
        # root process that cannot chown its own new file has a real problem
        # worth raising.
        try:
            os.fchown(handle.fileno(), 0, 0)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        handle.write(text)


def _units_are_down(env):
    """0, or 7 with the reason written, when either unit may still fire.

    Both `systemctl disable --now` and `stop` run with check=False because a
    first install has nothing to stop, so their status cannot distinguish
    that from a real failure. Nor can `is-active --quiet`'s exit code: a bus
    error is also non-zero, which would read as "inactive" and replace the
    payload under a live timer. The state WORD is read instead, and anything
    unrecognised aborts -- indeterminate is not inactive.

    Inactive is not disabled. A partial `disable --now` failure leaves the
    unit stopped with its enablement symlink intact -- armed again at the
    next boot, over whatever this run leaves behind -- while both is-active
    probes read `inactive`. `show --property=UnitFileState --value` rather
    than `is-enabled`: on some systemd versions `is-enabled` for a unit with
    no file prints nothing to stdout and puts the error on stderr, which
    read as "undetermined" on the ordinary first-install case. `show
    --value` always exits 0 and reports "" for a unit with no file, beside
    the same state words for one that exists. The returncode is judged
    first regardless: a broken bus connection ALSO produces empty stdout,
    and that is not the same fact as "no unit file".
    """
    for unit in (TIMER_UNIT, SERVICE_UNIT):
        probe = run(["systemctl", "is-active", unit], check=False, env=env)
        state = (probe.stdout or "").strip()
        if state in ("inactive", "failed", "unknown", "not-found"):
            continue
        sys.stderr.write(
            "deploy.py: cannot confirm %s is inactive (systemctl said %r); "
            "refusing to touch the payload underneath it.\n"
            % (unit, state or "<nothing>"))
        sys.stderr.write(
            "  Stop and disable it by hand and re-run. Its ExecStartPre runs\n"
            "  shim/install.sh as root, over whatever is on disk.\n")
        return 7
    enabled = run(["systemctl", "show", TIMER_UNIT,
                   "--property=UnitFileState", "--value"],
                  check=False, env=env)
    state = (enabled.stdout or "").strip()
    if enabled.returncode != 0 or state not in ("", "disabled", "static", "masked"):
        sys.stderr.write(
            "deploy.py: %s is stopped but still %s; refusing to touch the "
            "payload it is armed to run.\n"
            % (TIMER_UNIT, state or "in an undetermined enablement state"))
        sys.stderr.write(
            "  `systemctl disable --now %s` by hand and re-run. A\n"
            "  stopped-but-enabled timer restarts at the next boot.\n" % TIMER_UNIT)
        return 7
    return 0


def _remove_installed_entries(args, env):
    """Take back what this run copied: every INSTALLED_ENTRIES path under
    the prefix, and nothing else -- the same bound the recursive mutations
    keep. `check=False`, because an entry the failed step never reached is
    not a reason to stop a refusal half-way."""
    for entry in INSTALLED_ENTRIES:
        run(["rm", "-rf", os.path.join(args.prefix, entry)],
            check=False, dry_run=args.dry_run, env=env)


def _write_offenders(offenders):
    """The first ten `unowned_by()` offenders, one per line, on stderr."""
    for path, _code, why in sorted(set(offenders))[:10]:
        sys.stderr.write("  %s: %s\n" % (path, why))


def system_execute(args, env=None):
    if not _is_root():
        sys.stderr.write(
            "deploy.py: --system --i-have-approval must run as root\n")
        return 3

    def repair(spool):
        # Repair before judging. These are this installer's own files and
        # their mode is its to assert; the preview advertises the identical
        # list from the identical function. audit_dir_blockers() already
        # declines to refuse over them, so the order is belt and braces --
        # but doing it first means the check runs against the state the
        # operator will actually be left in.
        for path, _mode in spool_mode_repairs(spool):
            run(["chmod", "go-w", path], dry_run=args.dry_run, env=env)

    # Every check, from the function the preview calls: an install that
    # refuses something the preview accepted is a guardrail that fails
    # exactly when it is being installed, half-applied, on a shared node.
    rc, _checks = preflight(args, privileged=True, repair=repair)
    if rc != 0:
        return rc

    spool = args.spool_dir
    marker = os.path.join(args.prefix, PAYLOAD_MARKER)

    # Snapshot the payload HERE: after the checks, which are pure Python and
    # cannot block, and before the first `systemctl`, which can (ADR-0006).
    # Not earlier: every refusal above returns without issuing a single
    # command, and there are tests pinning that. Not later: placed just
    # before the copies it would leave the window as wide as a `systemctl
    # stop --now` takes.
    staging = stage_payload(env=env, dry_run=args.dry_run)
    source_root = REPO if args.dry_run else staging

    # Disarm first. On a redeploy the timer from the previous install is
    # still active, firing ExecStartPre on every slot -- so between the
    # `rm -rf` below and a refusal further down, root would execute a payload
    # this run has already rejected. DISABLE, not just stop: `stop` leaves
    # the enablement symlink in place, so a refusal further down would leave
    # the previous timer armed again at the next reboot. The enable at the
    # end re-arms only once validation has passed.
    run(["systemctl", "disable", "--now", TIMER_UNIT],
        check=False, dry_run=args.dry_run, env=env)
    run(["systemctl", "stop", SERVICE_UNIT],
        check=False, dry_run=args.dry_run, env=env)
    if not args.dry_run and _units_are_down(env) != 0:
        return 7

    # 0700 while the payload is being assembled, widened to 0755 only once
    # every check below has passed. `cp -a --no-preserve=ownership` preserves
    # SOURCE MODE BITS, so a setuid file from the source becomes a root-owned
    # setuid file on the destination the moment it is written. unowned_by()
    # detects that, but detection is not neutralization: it exists on disk
    # in between. An inaccessible prefix means no other account can reach it
    # during that window.
    run(["install", "-d", "-m", "0700", args.prefix],
        dry_run=args.dry_run, env=env)
    # The marker FIRST, and it carries the version: `cat
    # $PREFIX/.walk-blocker-payload` answers "what is installed here?" with
    # no privilege and no execution, and still answers on a payload too
    # broken to run its own --version -- which is only true while it is
    # installed before the code it describes.
    if args.dry_run:
        # The one payload entry GENERATED rather than copied, so there is no
        # REPO path to name here. Printing the content is the honest
        # substitute for a source path a dry run deliberately never creates.
        print("would write: %s (walk-blocker %s)" % (marker, __version__))
    else:
        run(["install", "-m", "0644",
             os.path.join(source_root, PAYLOAD_MARKER), marker], env=env)
    for relative, is_dir, mode in PAYLOAD_SOURCES:
        source = os.path.join(source_root, relative)
        target = os.path.join(args.prefix, relative)
        if is_dir:
            run(["rm", "-rf", target], check=False, dry_run=args.dry_run, env=env)
            # --no-preserve=ownership, so every destination inode is
            # root-owned from CREATION. Plain `cp -a` as root chowns each new
            # file to the source's owner, which opens a window the
            # reassertion below cannot close: an already-open descriptor
            # keeps its access across a later chown or chmod. The chown and
            # the check stay as the second and third lines of defence.
            run(["cp", "-a", "--no-preserve=ownership", source, target],
                dry_run=args.dry_run, env=env)
        else:
            run(["install", "-m", mode, source, target],
                dry_run=args.dry_run, env=env)

    # Before the recursive mutations, not after. GNU `chmod -R` DEREFERENCES
    # a symlink named as its command-line operand, so a payload whose `shim`
    # or `docs` is a link would have these commands rewrite modes throughout
    # the target instead. unowned_by() catches escaping links, but it runs
    # after this, and the prefix's 0700 does not contain a root operation
    # that walks out through the link.
    if not args.dry_run:
        linked = [entry for entry in INSTALLED_ENTRIES
                  if os.path.islink(os.path.join(args.prefix, entry))]
        if linked:
            sys.stderr.write(
                "deploy.py: refusing to install: these copied entries are "
                "symlinks, and a recursive chmod would follow them out of "
                "%s:\n" % args.prefix)
            for entry in linked:
                sys.stderr.write("  %s -> %s\n" % (
                    entry, os.path.realpath(os.path.join(args.prefix, entry))))
            _remove_installed_entries(args, env)
            return 5

    # Reassert root, then prove it before anything is wired up. Scoped to
    # the entries this installs, NOT to the prefix: the install knows exactly
    # what it put there, so it mutates exactly that.
    for cmd in ownership_commands(args.prefix):
        run(cmd, dry_run=args.dry_run, env=env)

    if not args.dry_run:
        # The whole prefix, not just the installed entries. Scoping the
        # MUTATIONS is what bounds the blast radius; scoping the CHECK as
        # well would stop it examining the prefix DIRECTORY, whose owner can
        # replace shim/ after the check and have the timer run it as root.
        # Reading has no blast radius, so it covers everything in there.
        offenders = unowned_by(args.prefix)
        if offenders:
            sys.stderr.write(
                "deploy.py: %s is not root-owned after install; refusing to "
                "wire it up.\n" % args.prefix)
            _write_offenders(offenders)
            # Remove what this run wrote, rather than leaving a rejected
            # payload on disk for someone to widen later. Bounded to the
            # entries this install creates.
            _remove_installed_entries(args, env)
            sys.stderr.write(
                "  Nothing was enabled, the units are left stopped AND\n"
                "  disabled, the payload this run wrote has been removed, and\n"
                "  %s stays 0700. See ADR-0004: artifacts the monitored\n"
                "  account can write are not a backstop against that account.\n"
                % args.prefix)
            return 5

    # Checks passed, so the payload may now be readable by the users it is
    # for. Everything above ran while the prefix was root-only.
    run(["chmod", "0755", args.prefix], dry_run=args.dry_run, env=env)

    # The audit directory, explicitly and BEFORE install.sh. Left to whichever
    # of install.sh or the reaper's first write got there first, its mode was
    # an artifact of ordering and umask; it is a recorded decision instead
    # (ADR-0012). Ordering is load-bearing: install.sh re-asserts the same
    # mode on every poll, so putting this after that call would make it dead
    # code the day anyone reordered.
    run(["install", "-d", "-m", "0755", spool],
        dry_run=args.dry_run, env=env)

    # No path flags: install.sh carries the same literals, stamped from the
    # same site.toml by the same build. From the DEPLOYED copy, which is the
    # only place install.sh will run an approved install from.
    run([TRUSTED_SH, os.path.join(args.prefix, "shim", "install.sh"),
         "--system", "--i-have-approval"],
        capture=False, dry_run=args.dry_run, env=env)

    # Re-assert AFTER install.sh, because it creates $prefix/bin -- the
    # directory that actually holds the shims -- and the earlier check ran
    # before that existed.
    if not args.dry_run:
        offenders = unowned_by(args.prefix)
        if offenders:
            sys.stderr.write(
                "deploy.py: %s failed the ownership check AFTER install.sh "
                "ran.\n" % args.prefix)
            _write_offenders(offenders)
            sys.stderr.write(
                "  No systemd unit was written and no timer enabled, but the\n"
                "  hook blocks (%s) and the shim farm ARE in place. Run\n"
                "  `python3 deploy.py --uninstall` to reverse them.\n"
                % ", ".join(enabled_hook_files(args)))
            return 9

    service, timer = render_units(args.prefix, spool)
    service_path = os.path.join(args.unit_dir, SERVICE_UNIT)
    timer_path = os.path.join(args.unit_dir, TIMER_UNIT)
    if args.dry_run:
        print("would write: %s" % service_path)
        print("would write: %s" % timer_path)
    else:
        # os.makedirs applies the caller's umask to EVERY component it
        # creates, and the chmod covers only the leaf; both are needed.
        # Only chmod what this call CREATES: an existing trusted directory
        # may be 0700 or 0750 deliberately and may hold unrelated files --
        # widening it would expose someone else's contents as a side effect
        # of deploying. Do not rewrite what you did not put there.
        created = not os.path.isdir(args.unit_dir)
        previous_umask = os.umask(0o022)
        try:
            os.makedirs(args.unit_dir, exist_ok=True)
        finally:
            os.umask(previous_umask)
        if created:
            os.chmod(args.unit_dir, 0o755)
        write_unit(service_path, service)
        write_unit(timer_path, timer)

    run(["systemctl", "daemon-reload"], dry_run=args.dry_run, env=env)
    run(["systemctl", "enable", "--now", TIMER_UNIT],
        dry_run=args.dry_run, env=env)

    print("\ninstalled, report-only. There are TWO audit trails, and the")
    print("evidence for --kill needs both:")
    print("  tail %s" % os.path.join(spool, "reaper-audit.jsonl"))
    print("      # Layer 2: what the reaper found, written as root")
    print("  journalctl -t walk-blocker -o json")
    print("      # Layer 1: escape-hatch overrides, and the reconcile's own")
    print("      # reports. An ordinary user cannot write %s,"
          % audit_path(spool))
    print("      # so these are NOT in it -- reading only the file would show")
    print("      # that column as a flat zero.")
    print("What is installed, without executing anything:")
    print("  cat %s" % marker)
    print("  cat %s" % os.path.join(args.prefix, "site.lock.json"))
    return 0


def system_uninstall(args, env=None):
    if not _is_root():
        sys.stderr.write("deploy.py: --uninstall must run as root\n")
        return 3

    # install.sh derives BIN=$PREFIX/bin and removes every shim link in it,
    # so a careless prefix once deleted system binaries as root. That whole
    # class is answered by the prefix being a literal.
    args.prefix = canonical_prefix(args.prefix)

    # Every hook file too, not just the prefix: this teardown reads or
    # removes all three and deletes unit files, so each needs the same
    # checks the install path applies. A disabled hook's file is validated
    # all the same -- the cost is a stat, and a hand-edited copy that
    # re-enables it would otherwise reach strip_block() unchecked. The spool
    # is excluded because uninstall does not touch it.
    if validate_root_write_paths(
            args, attrs=("prefix", "unit_dir", "bashrc_file",
                         "zshenv_file", "fish_conf_file")) != 0:
        return 6

    # Same rule as the install: a teardown pointed elsewhere would `rm -f`
    # and `systemctl disable` against paths this installer never wrote.
    why = not_a_default(args)
    if why:
        sys.stderr.write(why)
        return 6

    # And it has to be OUR install. Without the marker a lookalike directory
    # passes every check above, and install.sh then removes what sits in its
    # bin/.
    if not os.path.exists(os.path.join(args.prefix, PAYLOAD_MARKER)):
        sys.stderr.write(
            "deploy.py: refusing prefix=%s: no %s marker, so this is not a\n"
            "  directory walk-blocker installed. Uninstalling would `rm -f`\n"
            "  wrapped tool names out of %s/bin, which here would be\n"
            "  somebody else's binaries.\n"
            % (args.prefix, PAYLOAD_MARKER, args.prefix))
        return 6

    run(["systemctl", "disable", "--now", TIMER_UNIT],
        check=False, dry_run=args.dry_run, env=env)
    # Disabling the timer does not stop a service instance already running,
    # and that instance's ExecStartPre is `install.sh --relink` -- which would
    # recreate the shim links this uninstall is about to remove, after it
    # removes them, while still reporting success.
    run(["systemctl", "stop", SERVICE_UNIT],
        check=False, dry_run=args.dry_run, env=env)
    # BOTH units, exactly as the install path does: a live timer sitting
    # there ready to fire `install.sh --relink` would rebuild the farm this
    # function is about to remove.
    if not args.dry_run and _units_are_down(env) != 0:
        return 7
    # Statuses aggregated, not discarded. `rm -f` already succeeds for an
    # absent file, so a failure here means something real -- a read-only
    # filesystem, say -- and printing "removed" over it claims an outcome
    # this did not achieve.
    failures = []
    for name in (SERVICE_UNIT, TIMER_UNIT):
        target = os.path.join(args.unit_dir, name)
        if run(["rm", "-f", target], check=False,
               dry_run=args.dry_run, env=env).returncode != 0:
            failures.append("could not remove %s" % target)
    if run(["systemctl", "daemon-reload"], check=False,
           dry_run=args.dry_run, env=env).returncode != 0:
        failures.append("systemctl daemon-reload failed; systemd may still "
                        "have the removed units loaded")
    # The DEPLOYED helper, verified, and no fallback to this directory. The
    # marker check above has already established that the prefix is one
    # walk-blocker installed.
    helper, why = uninstall_helper(args.prefix)
    if helper is None:
        hooks = enabled_hook_files(args)
        sys.stderr.write(
            "deploy.py: refusing to run the teardown helper: %s\n" % why)
        sys.stderr.write(
            "  This would have to come from the payload directory instead, and\n"
            "  a file read this late can be replaced by its owner after you\n"
            "  started. The units are already stopped and disabled. To finish\n"
            "  by hand, as root: strip the block between the walk-blocker\n"
            "  markers in each shared hook file, remove the fish drop-in\n"
            "  outright (the whole file is walk-blocker's) -- the hook files\n"
            "  are %s -- then remove %s.\n"
            % (", ".join(hooks) or "none on this site",
               os.path.join(args.prefix, "bin")))
        return 5
    removal = run([TRUSTED_SH, helper, "--uninstall"],
                  capture=False, check=False, dry_run=args.dry_run, env=env)

    # check=False so that a partial removal still gets past the unit teardown
    # above -- but the status is REPORTED, not discarded. install.sh can fail
    # while stripping a hook block or unlinking the shims, and printing
    # "removed" over that tells an operator Layer 1 is gone when it may
    # still be on every user's PATH. Same defect as an audit log claiming a
    # kill it did not achieve.
    if removal.returncode != 0:
        sys.stderr.write(
            "deploy.py: the systemd units were removed, but "
            "shim/install.sh --uninstall exited %d.\n" % removal.returncode)
        sys.stderr.write(
            "  Layer 1 may still be installed: check the hook files (%s)\n"
            "  and for shims under %s/bin, before assuming the guard is gone.\n"
            % (", ".join(enabled_hook_files(args)) or "none on this site",
               args.prefix))
        return 8

    if failures:
        sys.stderr.write("deploy.py: the uninstall did not fully succeed:\n")
        for failure in failures:
            sys.stderr.write("  %s\n" % failure)
        return 8

    print("\nremoved. the audit trail under %s is left in place." % args.spool_dir)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Outside the required mode group on purpose: argparse's version action
    # exits during parsing, before the group is enforced, so `--version`
    # answers on its own. Not a path argument and not a mode -- ADR-0005 is
    # about where this installer can be POINTED, and this points nowhere.
    parser.add_argument("--version", action="version",
                        version="walk-blocker %s" % __version__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--system", action="store_true",
                      help="system-wide install (root-owned)")
    mode.add_argument("--uninstall", action="store_true",
                      help="reverse a --system install (root-owned)")
    parser.add_argument("--i-have-approval", action="store_true",
                        help="actually install rather than only preview "
                             "(also requires root)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # The six locations are deliberately NOT options. argparse rejects
    # `--prefix` with "unrecognized arguments" and exit 2, which is the
    # point: the guarantee is that this command cannot be pointed somewhere
    # else, not that it validates being pointed somewhere else.
    for attr, value in sorted(default_paths().items()):
        setattr(args, attr, value)

    if args.uninstall:
        return system_uninstall(args)
    if args.i_have_approval:
        return system_execute(args)
    return system_preview(args)


if __name__ == "__main__":
    sys.exit(main())
