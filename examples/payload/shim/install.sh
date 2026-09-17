#!/bin/sh
# Install (or remove) Layer 1: the PATH shim farm and the shell hooks that
# put it on PATH. POSIX sh; runs on the node.
#
# Run ONCE, system-wide, by an authorized root sysadmin -- normally invoked
# for you by `deploy.py --system --i-have-approval`, not by hand. Ordinary
# users never run this: it is not a per-account setup step, and nothing is
# required of an individual login shell beyond already existing on the node.
#
#   install.sh --system                PRINT what an install would do and exit
#   install.sh --system --i-have-approval
#                                      as root, actually install. Saves each
#                                      hook file's pre-install content to
#                                      <file>.walk-blocker.orig, once, the
#                                      first time this runs.
#   install.sh --relink                reconcile: the symlink farm, the hook
#                                      state, the audit directory's mode and
#                                      the mount table. The reaper's timer
#                                      runs this as root on every poll.
#   install.sh --uninstall             as root, reverse a --system install
#   install.sh --version
#   install.sh --help
#
# There are NO path flags. Every location this script writes as root is a
# literal stamped in by `walk-blocker build` from site.toml (ADR-0013): the
# lines marked `# GENERATED from` below. A different location is a
# site.toml change, a rebuild and a redeploy -- a diff someone read -- and
# never an argument. A test that needs other locations stamps a copy.
#
# Root-owned. Anything running as the monitored account -- including an
# agent, deliberately instructed or not -- must not be able to disable this
# with an ordinary unprivileged command. That ruled out a per-user install
# (ADR-0004).
#
# Layer 1 is ADVISORY (ADR-0001). An absolute path, a private PATH, a
# container, a batch script, a second-level shell all go around it. That is
# not a defect here; it is the reason Layer 2 exists.

set -eu

VERSION='0.1.0'  # GENERATED from VERSION
# One payload, one number -- see deploy.py. A literal because this script
# runs on the node with nothing to read it from.

sg_banner() {
    # The maze on the README. printf is a builtin, so this answers where
    # PATH is empty; no line carries a quote, a backslash or a percent.
    printf '%s\n' \
        '  ■═╦═════════╦═════╦═════╦═══╦═════════════╦═══════╗' \
        '  ◆·║  ·······║  ···║     ║   ║        ·····║       ║' \
        '  ║·║ ║·╔════·║ ║·║·║ ║ ══╝ ║ ║ ╔═╦═══╗·╔═╗·╚═══╗ ║ ║' \
        '  ║·║ ║·║·····║ ║·║·║ ║     ║   ║ ║···║·║ ║·····║ ║ ║' \
        '  ║·╚═╣·║·══╦═╝ ║·║·║ ╚═════╩═══╝ ║·║·║·║ ╚════·║ ║ ║' \
        '  ║···║·║···║   ║·║·║             ║·║···║·······║ ║ ║' \
        '  ╠══·║·╚═╗·╚═╦═╝·║·╠═════════════╣·╠═══╣·══╦═══╩═╝ ║' \
        '  ║···║···║···║···║·║FluidNumerics║·║   ║···║       ║' \
        '  ║·══╣ ║·╚═╗·║·╔═╝·║             ║·║ ══╬══·║ ║ ╔══ ║' \
        '  ║···║ ║···║···║···║ 𝐰𝐚𝐥𝐤𝐛𝐥𝐨𝐜𝐤𝐞𝐫 ║·║   ║···║ ║ ║   ║' \
        '  ╠══·╠═╩══·╠═══╣·══╣             ║·║ ║ ║·══╣ ║ ╚═╗ ║' \
        '  ║···║·····║   ║···║ stops slow  ║·║ ║ ║···║ ║   ║ ║' \
        '  ║·══╣·════╣ ══╩══·║ filesystem  ║·╚═╣ ╚═╗·╠═╩══ ║ ║' \
        '  ║···║·····║·······║ traversals  ║···║   ║·║     ║ ║' \
        '  ╠═╗·╚════·║·══╦═══╩═══╦═════════╝ ║·║ ║ ║·║ ════╩═╣' \
        '  ║ ║·······║···║·······║           ║·║ ║···║       ║' \
        '  ║ ╚═══╦═══╬══·║·╔═══╗·╚═══╦═══════╣·╚═╣·╔═╝ ════╗ ║' \
        '  ║     ║   ║···║·║   ║·····║·······║···║·║       ║ ║' \
        '  ║ ║ ══╝ ║ ║·══╝·║ ══╩════·║·╔════·╚══·║·╚═══════╝ ║' \
        '  ║ ║     ║  ·····║        ···║    ·····║···········◆' \
        '  ╚═╩═════╩═══════╩═══════════╩═════════╩═══════════■' \
        ''
}

sg_usage() {
    sg_banner
    printf '%s\n' \
        'usage: install.sh --system [--i-have-approval] | --relink | --uninstall | --version' \
        '' \
        '  --system              print what an install would do and exit' \
        '  --system --i-have-approval' \
        '                        as root, actually install' \
        '  --relink              reconcile the shim farm, hooks, audit directory and' \
        '                        mount table; the timer runs this as root every poll' \
        '  --uninstall           as root, reverse a --system install' \
        '  --version             the walk-blocker version this was built from' \
        '' \
        'There are no path flags: every location is stamped in from site.toml.'
}

MODE=''
APPROVED=0

while [ $# -gt 0 ]; do
    case $1 in
        --system|--uninstall|--relink) MODE=${1#--} ;;
        --i-have-approval) APPROVED=1 ;;
        -h|--help) sg_usage; exit 0 ;;
        # Exits here rather than setting a MODE: this has to answer on a node
        # where the install is broken, which is when it is asked.
        --version) sg_banner; printf 'walk-blocker %s\n' "$VERSION"; exit 0 ;;
        *) echo "install.sh: unknown argument $1" >&2; exit 64 ;;
    esac
    shift
done
[ -n "$MODE" ] || { echo "install.sh: need --system, --relink or --uninstall" >&2; exit 64; }

HERE=$(cd "$(dirname "$0")" && pwd)
# Generated next to this script by the build; sets SG_WRAPPED_NAMES.
# DELIBERATELY NOT SOURCED HERE. It used to be, at the top of the file, which
# put it ahead of every check -- and the systemd unit runs this script AS ROOT
# on every poll, so a swapped or user-owned wrapped_names.sh got root code
# execution before anything could reject it. Checking it later, as
# require_root_owned_payload() does, established nothing about a file that had
# already run. See load_wrapped_names(), called from each mode after that
# mode's validation.
SG_WRAPPED_NAMES=''

# `[install]` -- all on local disk, never on the filesystem under
# investigation: a guard that lives on what it monitors is unavailable exactly
# when it is needed.
DEFAULT_PREFIX='/usr/local/lib/walk-blocker'  # GENERATED from site.toml:install.prefix
PREFIX=$DEFAULT_PREFIX
BIN=$PREFIX/bin

# The audit trail, joined here from its two halves so `deploy.py` and the
# reaper, which carry the same two literals, cannot disagree with this file
# about where it is. Hoisted above `case $MODE` because --relink needs it too,
# and --relink is what runs on every poll.
SG_SPOOL_DIR='/var/log/walk-blocker'  # GENERATED from site.toml:install.spool_dir
SG_AUDIT_FILENAME='searchguard-audit.jsonl'  # GENERATED from site.toml:install.audit_filename
AUDIT=$SG_SPOOL_DIR/$SG_AUDIT_FILENAME

# link_farm() wraps a name only if it already resolves somewhere -- and
# "somewhere" used to mean the CALLER's $PATH, so what got linked depended on
# who ran the installer. An interactive operator and the systemd unit do not
# necessarily agree, and a tool that resolves for one but not the other would
# be linked and unlinked on alternating polls. The site names the directories
# to search (`[install].tool_search_path`, measured from the unit's own
# environment at the site); the caller's PATH is never consulted.
DEFAULT_TOOL_PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'  # GENERATED from site.toml:install.tool_search_path[:]
SG_TOOL_PATH=$DEFAULT_TOOL_PATH

BEGIN='# >>> walk-blocker >>>'
END='# <<< walk-blocker <<<'

# `[hooks.<shell>]` (ADR-0008). Each shell has the file its block is written
# to, the package that likely owns that file (so a report can name the
# likeliest remover), whether it is enabled at all, and a gate: `required`
# fails the install when the hook cannot be proven to fire; `best-effort` is
# written and checked only when the shell's binary resolves, and never gates.
# Which class a shell gets is a census result at the site, not a property of
# the shell, so it arrives here as a literal and is not decided by this file.
BASHRC_FILE='/etc/bash.bashrc'  # GENERATED from site.toml:hooks.bash.file
BASH_HOOK_PACKAGE='bash'  # GENERATED from site.toml:hooks.bash.package
BASH_HOOK_ENABLED='true'  # GENERATED from site.toml:hooks.bash.enabled
BASH_HOOK_GATE='required'  # GENERATED from site.toml:hooks.bash.gate
ZSHENV_FILE='/etc/zsh/zshenv'  # GENERATED from site.toml:hooks.zsh.file
ZSH_HOOK_PACKAGE='zsh'  # GENERATED from site.toml:hooks.zsh.package
ZSH_HOOK_ENABLED='true'  # GENERATED from site.toml:hooks.zsh.enabled
ZSH_HOOK_GATE='required'  # GENERATED from site.toml:hooks.zsh.gate
FISH_CONF_FILE='/etc/fish/conf.d/walk-blocker.fish'  # GENERATED from site.toml:hooks.fish.file
FISH_HOOK_ENABLED='true'  # GENERATED from site.toml:hooks.fish.enabled
FISH_HOOK_GATE='best-effort'  # GENERATED from site.toml:hooks.fish.gate

# The two classes, derived in sh from the three stamped tables above rather
# than stamped as lists of their own: the build already carries the per-shell
# values, and a second copy of the same fact is a second thing to drift.
SG_HOOKS_REQUIRED=''
SG_HOOKS_BEST_EFFORT=''
sg_class_hook() {
    # sg_class_hook SHELL ENABLED GATE. A disabled hook is in neither class:
    # never written, never verified, never reported.
    [ "$2" = true ] || return 0
    case $3 in
        required)    SG_HOOKS_REQUIRED="$SG_HOOKS_REQUIRED $1" ;;
        best-effort) SG_HOOKS_BEST_EFFORT="$SG_HOOKS_BEST_EFFORT $1" ;;
        *)
            # The schema refuses this at build; a hand-edited copy reaches it.
            echo "install.sh: hooks.$1.gate is '$3'; expected required or best-effort" >&2
            exit 64
            ;;
    esac
}
sg_class_hook bash "$BASH_HOOK_ENABLED" "$BASH_HOOK_GATE"
sg_class_hook zsh  "$ZSH_HOOK_ENABLED"  "$ZSH_HOOK_GATE"
sg_class_hook fish "$FISH_HOOK_ENABLED" "$FISH_HOOK_GATE"

# `[filesystems]`, for the reconcile's mount report (ADR-0016, tier three):
# the same three inputs the shim classifies with, so this file and guard.sh
# cannot disagree about which mount is running on its default. The type list
# is colon-joined at build and walked without word-splitting below, because
# it carries globs (`fuse.*`) and this script does not `set -f`.
SG_MOUNT_TABLE='/proc/mounts'  # GENERATED from site.toml:filesystems.mount_table
SG_REMOTE_FSTYPES='lustre:wekafs:beegfs:gpfs:ceph:nfs4:nfs:cifs:smb3:glusterfs:panfs:fuse.*:9p:sshfs:s3fs:daos'  # GENERATED from site.toml:filesystems.remote_fstypes[:]
SG_REMOTE_PROXY='true'  # GENERATED from site.toml:filesystems.remote_proxy
# `[[filesystems.mounts]]` as `PATH=CLASS` or `PATH=CLASS=MAXDEPTH` tokens,
# space-separated -- the shape guard.sh's own override table takes, rendered
# by the same function at build. Safe to word-split: the schema refuses a
# path carrying whitespace or `=`.
SG_MOUNT_OVERRIDES='/home=expensive=4 /opt/site-tools=cheap'  # GENERATED from site.toml:derived.mount_overrides

# The audit sink for sg_report(). Absolute, and deliberately NOT taken from
# the environment: a sink that resolves through PATH can be shadowed by
# whatever set that PATH. The same TEST SEAM as guard.sh's SG_LOGGER -- a
# test that must not emit a real record stamps a copy with a path that does
# not exist, rather than being handed an environment override that production
# would also honour. A real record carries valid JSON and journald's own
# _UID, so a test-emitted one is indistinguishable from a genuine one.
SG_LOGGER='/usr/bin/logger'  # GENERATED from site.toml:trusted_binaries.logger

is_root() {
    [ "$(id -u)" -eq 0 ]
}

require_no_newline() {
    # A NEWLINE in a value that gets embedded in the sourced block cannot be
    # carried faithfully, so it is refused rather than silently altered.
    #
    # shquote()'s inner command substitution strips trailing newlines, and so
    # does the `$(shquote ...)` at the call site, and so would any third
    # layer: `$()` always strips them. Preserving a trailing newline through
    # that would mean emitting those lines outside the heredoc entirely.
    #
    # Not worth it for a path nothing here can use anyway: a newline in the
    # audit path also breaks the JSON record the shim writes and the
    # `logger -t` tag beside it. Silently writing to a DIFFERENT path than
    # configured is the part that matters -- the escape-hatch records would
    # land somewhere the reaper never reads.
    #
    # The build refuses a newline in any stamped value already; this is the
    # half that holds for a hand-edited copy.
    case $1 in
        *"
"*)
            echo "install.sh: refusing $2: it contains a newline." >&2
            echo "  This value is embedded in a file every login shell" >&2
            echo "  sources, and a newline cannot survive that faithfully --" >&2
            echo "  it would be silently truncated and the shim would write" >&2
            echo "  its audit records somewhere other than configured." >&2
            exit 3
            ;;
    esac
}

shquote() {
    # Single-quote a value for safe reuse in a file that will be SOURCED.
    # These assignments used to be emitted bare, so a path of
    # `/var/log/x;id>/tmp/pwn` -- no whitespace, so nothing rejected it --
    # ended the assignment and ran `id` in every login shell and in the root
    # re-execs the verify functions perform.
    #
    # Inside single quotes the only character needing care is the single
    # quote, closed and reopened around an escaped one: '\''. The schema
    # refuses shell metacharacters in these values already; this is the half
    # that holds for a hand-edited copy.
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

require_deployed_copy() {
    # link_farm points every shim at "$HERE/guard.sh", so this script must be
    # running FROM the deployed copy whenever it is about to build that farm
    # as root. Out of an ordinary user's tree it puts that user's file on
    # every other user's PATH, and every login shell then execs code they can
    # rewrite at will. Being root at the time does not help; root is what
    # makes it effective.
    #
    # deploy.py copies the payload to $PREFIX/shim, re-owns it to root and
    # runs THAT, which is why the comparison is exact rather than an
    # ownership heuristic -- and why the payload check follows it: the right
    # location does not prove the right bytes.
    #
    # `|| true`, because `set -e` exits on a failed command substitution in an
    # assignment, and failing is the ordinary case for a standalone preview
    # with no $PREFIX/shim yet. An empty value never matches $HERE.
    _deployed=$(cd "$PREFIX/shim" 2>/dev/null && pwd || true)
    if [ "$HERE" != "$_deployed" ]; then
        echo "install.sh: refusing $1 from $HERE." >&2
        echo "  This must run from the deployed copy, $PREFIX/shim, so" >&2
        echo "  that the shims point at root-owned files. Run from here they" >&2
        echo "  would point into this directory, which every login shell on" >&2
        echo "  the node would then execute." >&2
        echo "  Use: python3 deploy.py --system --i-have-approval" >&2
        exit 3
    fi
    require_root_owned_payload "$HERE"
}

load_wrapped_names() {
    # Validate, then source. The file being executed is checked before it is
    # executed, which is the only order that means anything.
    #
    # The ownership requirement applies when we are ROOT, which is every path
    # that matters: the timer's `--relink`, and an approved install. A
    # non-root preview sourcing a file owned by the user running it gains that
    # user nothing, and requiring root ownership there would refuse an
    # ordinary preview from a checkout.
    _wn=$HERE/wrapped_names.sh
    if is_root; then
        # The DIRECTORY first. Checking only the file leaves a race: if $HERE
        # is writable by someone else, its owner can replace this root-owned
        # file between the stat below and the `.` that follows, and their
        # replacement runs as root. The approved install and root relink
        # reach here having already validated the chain through
        # require_deployed_copy(), but the standalone `--uninstall` does not
        # -- so the check belongs in this function rather than in its
        # callers, where three of the four paths happened to have it.
        require_trusted_chain "$HERE" "This script sources a file from there as root." 1
        if [ -L "$_wn" ]; then
            echo "install.sh: refusing: $_wn is a symlink, and this script" >&2
            echo "  sources it as root." >&2
            exit 3
        fi
        if [ ! -f "$_wn" ]; then
            echo "install.sh: refusing: $_wn is not a regular file, and" >&2
            echo "  sourcing a fifo as root would block here indefinitely." >&2
            exit 3
        fi
        _wninfo=$(stat -c '%u %a' "$_wn" 2>/dev/null) || {
            echo "install.sh: refusing: cannot examine $_wn" >&2
            exit 3
        }
        if [ "${_wninfo%% *}" != 0 ]; then
            echo "install.sh: refusing: $_wn is owned by uid" >&2
            echo "  ${_wninfo%% *}, not root, and this script sources it as root." >&2
            exit 3
        fi
        if [ "$(( 0${_wninfo##* } & 022 ))" -ne 0 ]; then
            echo "install.sh: refusing: mode ${_wninfo##* } on $_wn lets a" >&2
            echo "  non-root user choose what this script sources as root." >&2
            exit 3
        fi
    fi
    # shellcheck source=/dev/null
    . "$_wn"
}

require_trusted_chain() {
    # $1 and every directory above it must be root-owned and not writable by
    # anyone else. $2 is why, for the message.
    #
    # Checking a path's own type is not enough on its own: with only the final
    # component validated, in a writable directory another user could create
    # or replace it as a symlink between the check and the write that follows
    # -- and the root process then read the target and rewrote its contents
    # 0644. An absent target with a writable parent is the same race with an
    # extra step. deploy.py applies the same walk to every hook file's
    # parent; this is the half that holds when install.sh is run by hand.
    #
    # Strict about group/other write, including sticky directories: /tmp is
    # 1777 and the sticky bit does not stop the owner of an existing entry
    # rewriting it. The system directories the stamped paths sit under are
    # ordinarily uid 0 mode 755, so the real paths are unaffected.
    #
    # $3: 1 to refuse, 0 to note it on stdout and continue. A PREVIEW writes
    # nothing, so there is no race to lose -- but staying silent would print a
    # copy-pastable command that the install then refuses. So the preview
    # says so and keeps going.
    #
    # ANCHOR a relative path before walking. `dirname` on a relative path
    # eventually returns `.`, and `dirname .` is `.` forever, so the only
    # break -- reaching `/` -- would never be taken and the walk would HANG.
    # The schema admits only absolute paths, so a relative one arrives here
    # only from a hand-edited copy; anchored rather than refused, because a
    # relative path's real ancestors are exactly what needs checking, and
    # stopping the walk at `.` would have stopped before looking at them.
    _walk=$1
    case "$_walk" in
        /*) ;;
        *) _walk=$PWD/$_walk ;;
    esac
    _fatal=${3:-1}
    while :; do
        # A SYMLINK component breaks the walk, because `stat` follows it while
        # `dirname` does not: with /trusted/link -> /user-owned/rootdir this
        # would check the root-owned TARGET and then continue to /trusted,
        # never visiting /user-owned -- whose owner can swap rootdir straight
        # after. Refused rather than resolved, because resolving means walking
        # two chains and the sanctioned paths need neither.
        if [ -L "$_walk" ]; then
            _bad="$_walk is a symlink, so the chain above it cannot be walked"
            break
        fi
        _info=$(stat -c '%u %a' "$_walk" 2>/dev/null) || {
            _bad="cannot examine $_walk"
            _walk=''
            break
        }
        _wuid=${_info%% *}
        _wmode=${_info##* }
        _bad=''
        if [ "$_wuid" != 0 ]; then
            _bad="$_walk is owned by uid $_wuid, not root"
        # 022: group-write or other-write, whatever the setuid/sticky digits say.
        elif [ "$(( 0$_wmode & 022 ))" -ne 0 ]; then
            _bad="mode $_wmode on $_walk lets a non-root user replace what is under it"
        fi
        [ -n "$_bad" ] && break
        [ "$_walk" = / ] && break
        # Fixed-point guard as well as the `/` test. Belt and braces: the
        # anchor above makes `/` reachable for every input this function is
        # given today, and a loop that cannot terminate is a hang in a
        # root-run installer, which is not a failure mode worth leaving one
        # assumption away from.
        _prev=$_walk
        _walk=$(dirname "$_walk")
        [ "$_walk" = "$_prev" ] && break
    done
    if [ -n "$_bad" ]; then
        if [ "$_fatal" -eq 1 ]; then
            echo "install.sh: refusing --i-have-approval: $_bad." >&2
            echo "  $2" >&2
            echo "  Use: python3 deploy.py --system --i-have-approval" >&2
            exit 3
        fi
        echo "# NOTE: $_bad,"
        echo "# so --i-have-approval will refuse. $2"
        echo
    fi
}

require_root_owned_payload() {
    # An approved install links every shim at "$HERE/guard.sh", so that file
    # and every directory above it must be root-owned and not writable by
    # anyone else -- otherwise the shim farm points at code an ordinary user
    # can rewrite, and every login shell on the node executes it.
    #
    # The location check ($HERE = $PREFIX/shim) does NOT establish this:
    # copying shim/ into any user-writable directory laid out the same way
    # makes the two paths match. This is the check that actually holds, and it
    # is the same property deploy.py asserts with unowned_by().
    #
    # Strict about group/other write, including sticky directories: /tmp is
    # 1777, and a payload under it is not a trustworthy place to exec from
    # even though the sticky bit stops others deleting the entry.
    require_trusted_chain "$1" "The shims would point at code a non-root user can rewrite, and every login shell would exec it." 1

    # The chain is not enough. A root-owned 0755 shim/ can still CONTAIN a
    # user-owned file -- root only has to have copied it in with ownership
    # preserved, which is exactly the `cp -a` defect ADR-0004 records. The
    # directory permissions stop a user creating or replacing entries there;
    # they do not stop the owner of an existing entry from rewriting it. So
    # every file this payload actually executes gets checked itself:
    #
    #   guard.sh          every shim points at it; every user execs it
    #   install.sh        the systemd unit runs it AS ROOT on every poll
    #   wrapped_names.sh  SOURCED by this script, so it executes in the
    #                     caller's context -- root's, on that timer
    #   ../walk-job       link_farm puts it on every user's PATH too, and
    #                     every refusal tells them to run it. One level up
    #                     because it is payload rather than shim; the trusted
    #                     chain above covers $PREFIX as an ancestor of $HERE,
    #                     so only the file itself is left to check. A prefix
    #                     with no such file is refused here -- rerun
    #                     deploy.py, which is what puts it there.
    #
    # Symlinks are refused outright rather than followed: the target can sit
    # anywhere, and this walk has just finished proving things about paths
    # under $HERE specifically.
    #
    # Runs on EVERY root path that is about to link, `--relink` included --
    # so this executes on every timer poll, not only at install time. The
    # unit prefixes that ExecStartPre with `-` and bounds it with a timeout,
    # so a refusal here stops the relink and nothing else. It is still a
    # refusal rather than a warning -- a payload failing these checks is not
    # one to point every user's PATH at -- and after a successful install
    # nothing unprivileged can change it, so the check passes on every
    # ordinary poll.
    #
    # $PREFIX/walk-job, not ${1%/*}/walk-job: the invariant is "check exactly
    # what link_farm is about to link", and link_farm links $PREFIX/walk-job.
    for _path in "$1/guard.sh" "$1/install.sh" "$1/wrapped_names.sh" \
                 "$PREFIX/walk-job"; do
        if [ -L "$_path" ]; then
            echo "install.sh: refusing --i-have-approval: $_path is a symlink," >&2
            echo "  so what every shim executes is decided elsewhere." >&2
            exit 3
        fi
        # And a REGULAR file. `stat -c '%u %a'` answers happily for a FIFO or
        # a device node -- a root-owned 0644 fifo reports exactly what a file
        # would -- so refusing symlinks and then reading uid and mode would
        # let any other inode type through. A fifo named wrapped_names.sh
        # blocks the install the moment load_wrapped_names() sources it; a
        # non-regular guard.sh leaves every shim unusable while the verify
        # functions, which only check that the name resolves, still pass.
        if [ ! -f "$_path" ]; then
            echo "install.sh: refusing --i-have-approval: $_path is not a" >&2
            echo "  regular file, and uid and mode say nothing about that." >&2
            exit 3
        fi
        _info=$(stat -c '%u %a' "$_path" 2>/dev/null) || {
            echo "install.sh: refusing --i-have-approval: cannot examine $_path" >&2
            exit 3
        }
        _euid=${_info%% *}
        _emode=${_info##* }
        if [ "$_euid" != 0 ]; then
            echo "install.sh: refusing --i-have-approval: $_path is owned by" >&2
            echo "  uid $_euid, not root. Its owner could rewrite what every" >&2
            echo "  login shell on this node executes." >&2
            exit 3
        fi
        if [ "$(( 0$_emode & 022 ))" -ne 0 ]; then
            echo "install.sh: refusing --i-have-approval: mode $_emode on" >&2
            echo "  $_path lets a non-root user rewrite it." >&2
            exit 3
        fi
    done
}

require_plain_hook_file() {
    # require_plain_hook_file FILE WILL_WRITE KEY
    #
    # Generic over which hook file this is, because nothing below depends on
    # which shell reads it. $3 is the site.toml key the file came from, so
    # the messages name the value an operator would change.
    #
    # $2: 1 when this run will WRITE the file, 0 for a preview. It is NOT
    # $APPROVED: "did the operator approve an install" and "is this run about
    # to rewrite the file" are different questions. `--uninstall` never sets
    # APPROVED and rewrites the file through strip_block() all the same.
    #
    # strip_block() and prepend_block() both READ this file and write it back
    # as a 0644 regular file, so a symlink here republishes its target's
    # contents to every user on the node. Called from BOTH the system and
    # the uninstall arm, because both rewrite the same file for the same
    # effect.
    #
    # -L before -e: a dangling link fails -e, and the write would then create
    # a regular file at the link's TARGET rather than at this path.
    if [ -L "$1" ]; then
        echo "install.sh: refusing $3 $1: it is a symlink." >&2
        echo "  This file is read and rewritten 0644, which would publish the" >&2
        echo "  link target's contents to every user on the node." >&2
        exit 3
    fi
    if [ -e "$1" ] && [ ! -f "$1" ]; then
        echo "install.sh: refusing $3 $1: not a regular file." >&2
        exit 3
    fi
    # The LEAF's own ownership and mode, not just its type and its ancestors.
    # prepend_block PRESERVES the existing contents, and the verify functions
    # then have the real shell source the whole file AS ROOT to prove the
    # hook fires -- so a user-owned or group-writable hook file supplies
    # commands that run as root during the deploy. Refused only when the file
    # exists; an absent one is created by this script and the ancestor walk
    # below covers where.
    #
    # Reported in PREVIEW too, as a note rather than a refusal. Gating the
    # whole check on $2 meant a user-owned or group-writable file gave a
    # clean preview and an advertised command that then failed the instant
    # approval was supplied. A preview writes nothing, so there is nothing to
    # protect; there is only a refusal to predict.
    if [ -e "$1" ]; then
        _lbad=''
        _linfo=$(stat -c '%u %a' "$1" 2>/dev/null) || _linfo=''
        if [ -z "$_linfo" ]; then
            _lbad="cannot be examined"
        else
            _luid=${_linfo%% *}
            _lmode=${_linfo##* }
            if [ "$_luid" != 0 ]; then
                _lbad="owned by uid $_luid, not root"
            elif [ "$(( 0$_lmode & 022 ))" -ne 0 ]; then
                _lbad="mode $_lmode, writable by group or other"
            fi
        fi
        if [ -n "$_lbad" ]; then
            if [ "$2" -eq 1 ]; then
                echo "install.sh: refusing $3 $1: $_lbad." >&2
                echo "  This file is sourced as root to verify the hook fires," >&2
                echo "  so its owner would be choosing what runs during the deploy." >&2
                exit 3
            fi
            echo "# NOTE: $3 $1 is $_lbad,"
            echo "# so --i-have-approval will refuse. It is sourced as root to"
            echo "# verify the hook fires, so its owner would choose what runs."
            echo
        fi
    fi
    # ...and the directory it sits in, or the checks above are a snapshot
    # someone can invalidate before the write. See require_trusted_chain().
    require_trusted_chain "$(dirname "$1")" "Another user could replace that file between this check and the write, and its contents would be rewritten world-readable." "$2"
}

require_plain_dropin() {
    # require_plain_dropin FILE WILL_WRITE KEY -- the drop-in's own, weaker
    # refusal (ADR-0008). write_fish_conf() never READS its file, only
    # overwrites it via rename(), which replaces a destination symlink rather
    # than following it -- so there is no read-and-republish path to defend
    # and no leaf-ownership question, since nothing pre-existing is kept.
    # The only thing to refuse is "something already at this path is not
    # what this installer put there". Predicted in preview, refused on write,
    # like require_plain_hook_file().
    _dbad=''
    if [ -L "$1" ]; then
        _dbad="a symlink"
    elif [ -e "$1" ] && [ ! -f "$1" ]; then
        _dbad="not a regular file"
    fi
    if [ -n "$_dbad" ]; then
        if [ "$2" -eq 1 ]; then
            echo "install.sh: refusing $3 $1: it is $_dbad." >&2
            exit 3
        fi
        echo "# NOTE: $3 $1 is $_dbad, so --i-have-approval will refuse."
        echo
    fi
}

link_farm() {
    _bin=$1
    # After the caller's validation, never before it. Idempotent: a second
    # call just re-sources the same checked file.
    load_wrapped_names
    # An explicit mode, not `mkdir -p`, whose mode comes from the caller's
    # umask. This directory goes on every user's PATH: at 0700 (umask 077)
    # none of them can traverse it to reach a shim, and at 0777 (umask 000)
    # any of them can replace one. Neither is visible to a root-run verify,
    # which is why it is set rather than inherited.
    install -d -m 0755 "$_bin"
    _linked=0
    _skipped=''
    _dropped=0
    for _name in $SG_WRAPPED_NAMES; do
        # Only link names that already resolve. A shim named `ag` where ag is
        # absent makes `command -v ag` succeed and silently changes how other
        # people's scripts probe for tools.
        #
        # $SG_TOOL_PATH, not $PATH -- see the comment beside DEFAULT_TOOL_PATH.
        _found=''
        _oifs=$IFS
        IFS=:
        for _d in $SG_TOOL_PATH; do
            [ -n "$_d" ] || _d=.
            [ "$_d" = "$_bin" ] && continue
            if [ -x "$_d/$_name" ] && [ ! -d "$_d/$_name" ]; then
                _found=$_d/$_name
                break
            fi
        done
        IFS=$_oifs
        if [ -z "$_found" ]; then
            _skipped="$_skipped $_name"
            # Was this name wrapped until a moment ago? Removing a link is a
            # COVERAGE CHANGE and has to be visible; never having found a
            # tool that is not installed here is not. A plain `if`, because
            # whether `set -e` acts on a failing AND-OR list is
            # shell-dependent.
            if [ -L "$_bin/$_name" ]; then
                _dropped=$((_dropped + 1))
            fi
            rm -f "$_bin/$_name"
            continue
        fi
        ln -sfn "$HERE/guard.sh" "$_bin/$_name"
        _linked=$((_linked + 1))
    done

    # Unconditionally, and OUTSIDE the loop above. That loop skips a name the
    # node does not already have elsewhere on PATH, because shadowing a
    # missing tool changes how other people's scripts probe for it. walk-job
    # is the inverse: nothing else on the node provides it, which is the
    # entire reason it is installed. Every refusal ends by naming it, so a
    # node where this link is absent prints advice that resolves to
    # "command not found" -- a refusal with no alternative behind it, which
    # is what sends people to the absolute path of the real tool.
    #
    # require_root_owned_payload() has already checked the target: root-owned,
    # regular, not a symlink, not group- or other-writable.
    ln -sfn "$PREFIX/walk-job" "$_bin/walk-job"

    # walk-job is CLAIMED here, or the sweep below -- which removes every
    # symlink the name list does not cover -- would delete the link this
    # function just made, on this run and on every timer poll after it.
    sweep_unclaimed "$_bin" "$SG_WRAPPED_NAMES walk-job"
    echo "walk-blocker: $_linked shims plus walk-job in $_bin"
    [ -n "$_skipped" ] && echo "walk-blocker: not present on this node, not linked:$_skipped"
    [ -n "$SWEPT" ] && echo "walk-blocker: no longer wrapped, unlinked:$SWEPT"

    # Coverage SHRANK. That is the direction which is invisible by
    # definition: a tool that STARTS being wrapped shows up as a wrapped
    # tool, while a tool that STOPS being wrapped shows up as nothing at all
    # -- and this runs unattended on every poll, so "nothing at all" is what
    # anyone reading later would see. A tool that stops being wrapped is a
    # coverage change and belongs in the audit trail.
    #
    # Counts, not names. sg_report cannot escape a caller-shaped string, and
    # a swept entry is a filename read off a directory. The names are on
    # stdout above, under the unit, for whoever needs them.
    #
    # Silent when nothing was unwrapped, like every other report here.
    if [ "$((_dropped + SWEPT_N))" -gt 0 ]; then
        sg_report coverage_change "unwrapped-$((_dropped + SWEPT_N))"
    fi
    return 0
}

SWEPT=''
SWEPT_N=0
sweep_unclaimed() {
    # sweep_unclaimed DIR "NAME..." -- remove every symlink in DIR that the
    # name list does not claim. Sets SWEPT to what it removed.
    #
    # The loop above walks the TABLE, so a name the table no longer carries is
    # never visited and its symlink would survive every future reconcile: a
    # shim on every user's PATH, under a name the dispatcher itself no longer
    # knows, with nothing that would ever remove it. Sweeping the DIRECTORY
    # rather than the list reconciles both a tool that disappears from the
    # node and a name the table stops wrapping -- and uninstall is this with
    # an empty list, so its `rmdir` cannot fail silently on a residue.
    #
    # Symlinks only. This installer creates nothing else in here, so anything
    # else is an anomaly -- and a root-owned directory on every user's PATH is
    # the last place to silently delete something nobody can explain.
    _sw_dir=$1
    _sw_keep=$2
    SWEPT=''
    # Counted HERE, one entry at a time, rather than by re-splitting $SWEPT
    # afterwards. install.sh does not `set -f` the way guard.sh does, so
    # `for x in $SWEPT` glob-expands: a swept entry named `*` counts every
    # file in the working directory. Only root can put such a name in
    # $PREFIX/bin, but a count that feeds an audit record has no business
    # being wrong, and a name with a space in it would miscount too.
    SWEPT_N=0
    [ -d "$_sw_dir" ] || return 0
    for _sw_path in "$_sw_dir"/*; do
        # No matches leaves the pattern itself, which is not a symlink.
        [ -L "$_sw_path" ] || continue
        _sw_entry=${_sw_path##*/}
        # Quoted inside the pattern, so a glob character in a filename is
        # matched literally rather than matching everything.
        case " $_sw_keep " in
            *" $_sw_entry "*) continue ;;
        esac
        rm -f "$_sw_path"
        SWEPT="$SWEPT $_sw_entry"
        SWEPT_N=$((SWEPT_N + 1))
    done
    return 0
}

# --------------------------------------------------------------------------
# hooks: one table per shell (ADR-0008). Block content and verification are
# per shell; file mechanics are shared.
# --------------------------------------------------------------------------

hook_select() {
    # hook_select SHELL -- sets HK_FILE, HK_PKG, HK_KIND (block: a marked
    # block prepended to a shared file; dropin: a dedicated file this
    # installer generates in full), HK_BLOCK_FN, HK_VERIFY_FN and HK_KEY (the
    # site.toml key, for messages). A new shell is a new arm here plus a
    # block function and a verify function, never a hardcoded path.
    case $1 in
        bash)
            HK_FILE=$BASHRC_FILE; HK_PKG=$BASH_HOOK_PACKAGE; HK_KIND=block
            HK_BLOCK_FN=bashrc_block; HK_VERIFY_FN=verify_bash_hook ;;
        zsh)
            HK_FILE=$ZSHENV_FILE; HK_PKG=$ZSH_HOOK_PACKAGE; HK_KIND=block
            HK_BLOCK_FN=zshenv_block; HK_VERIFY_FN=verify_zsh_hook ;;
        fish)
            HK_FILE=$FISH_CONF_FILE; HK_PKG=''; HK_KIND=dropin
            HK_BLOCK_FN=fish_conf_block; HK_VERIFY_FN=verify_fish_hook ;;
        *) echo "install.sh: no hook is defined for a shell named $1" >&2; exit 64 ;;
    esac
    HK_KEY="hooks.$1.file"
}

check_hook_file() {
    # check_hook_file SHELL WILL_WRITE -- the pre-write refusals (or, in a
    # preview, the notes predicting them) for one shell's file.
    hook_select "$1"
    case $HK_KIND in
        block)  require_plain_hook_file "$HK_FILE" "$2" "$HK_KEY" ;;
        dropin) require_plain_dropin "$HK_FILE" "$2" "$HK_KEY" ;;
    esac
}

write_hook() {
    # write_hook SHELL [SUFFIX] -- write one shell's hook and say so.
    hook_select "$1"
    case $HK_KIND in
        block)
            prepend_block "$HK_FILE" "$HK_BLOCK_FN"
            echo "walk-blocker: $HK_FILE block installed${2:-}"
            ;;
        dropin)
            write_fish_conf "$HK_FILE" "$HK_BLOCK_FN"
            echo "walk-blocker: $HK_FILE written${2:-}"
            ;;
    esac
}

bashrc_block() {
    cat <<BLOCK
$BEGIN
# Layer 1 of walk-blocker. ADVISORY: an absolute path to the real tool, a
# private PATH, a container or a batch script all go around it. See
# $PREFIX/README.md.
#
# This block is deliberately ABOVE the interactivity guard below, and the
# mechanism it depends on is worth stating exactly, because it is not obvious
# and it is not ours.
#
# bash does not read this file when non-interactive. A bash built with
# SSH_SOURCE_BASHRC reads it anyway, but only when all of these hold:
#   - SSH_CLIENT or SSH2_CLIENT is set (or stdin is a socket), AND
#   - SHLVL < 2, i.e. this is the session's TOP-LEVEL shell.
# Every inbound ssh transport that exports SSH_CLIENT is covered by this one
# block; one that stops exporting it takes this hook quiet for
# \`ssh host 'cmd'\` -- the incident shape -- while interactive shells keep
# working, which is the failure noticed last. The installer verifies the
# hook actually fires rather than assuming it.
#
# The SHLVL clause is a real bypass, and an ordinary one:
# \`ssh host 'bash -c "find ..."'\` runs the search in a second-level shell
# that never reads this file. Layer 1 is advisory; this is one more way around
# it, and one more reason Layer 2 exists.
#
# Given this file IS read, the stock \`[ -z "\$PS1" ] && return\`-style guard
# still returns before anything useful runs, hence going above it.
# /etc/profile.d would not fire at all: a non-login non-interactive bash reads
# no profile.
WALK_BLOCKER_SHIM_DIR=$(shquote "$BIN")
WALK_BLOCKER_AUDIT=$(shquote "$AUDIT")
case ":\$PATH:" in
    *":\$WALK_BLOCKER_SHIM_DIR:"*) ;;
    *) PATH="\$WALK_BLOCKER_SHIM_DIR:\$PATH" ;;
esac
export PATH WALK_BLOCKER_SHIM_DIR WALK_BLOCKER_AUDIT
$END
BLOCK
}

zshenv_block() {
    # Same idempotent PATH-prepend as bashrc_block(); the prose differs
    # because the mechanism does. No interactivity-guard positioning note --
    # there is no such guard in the global zshenv to out-race.
    cat <<BLOCK
$BEGIN
# Layer 1 of walk-blocker. ADVISORY: an absolute path to the real tool, a
# private PATH, a container or a batch script all go around it. See
# $PREFIX/README.md.
#
# Unlike the bash hook, this file needs no SSH_CLIENT or SHLVL condition:
# zsh reads the global zshenv UNCONDITIONALLY -- every invocation, login or
# not, interactive or not, \`-c\` or not -- and \`-f\`/\`--no-rcs\` does not
# skip it (that only unsets the RCS option, so LATER files are skipped).
# That is why one hook here covers both a zsh account reached directly by
# \`ssh host 'cmd'\` and an interactive zsh started by hand inside an
# already-hooked bash session: it re-fires on every new zsh process either
# way, unlike bash's SHLVL<2 bypass.
#
# So this depends on zsh's own compiled-in behaviour, a third-party
# implementation detail the same way SSH_SOURCE_BASHRC is for bash. The
# installer verifies the hook actually fires rather than assuming it.
WALK_BLOCKER_SHIM_DIR=$(shquote "$BIN")
WALK_BLOCKER_AUDIT=$(shquote "$AUDIT")
case ":\$PATH:" in
    *":\$WALK_BLOCKER_SHIM_DIR:"*) ;;
    *) PATH="\$WALK_BLOCKER_SHIM_DIR:\$PATH" ;;
esac
export PATH WALK_BLOCKER_SHIM_DIR WALK_BLOCKER_AUDIT
$END
BLOCK
}

fish_conf_block() {
    # No $BEGIN/$END markers: unlike bashrc_block()/zshenv_block(), this is
    # not prepended into a file fish or an admin owns -- the fish hook file is
    # a dedicated snippet under fish's own conf.d drop-in directory, entirely
    # ours. Nothing to preserve, so nothing to mark the boundaries of. See
    # write_fish_conf() for what that changes about how it is written.
    #
    # shquote()'s POSIX close-escape-reopen output ('\'') also parses as a
    # fish single-quoted string with an embedded quote -- checked on the tool
    # by feeding its exact output, for a value containing both a literal
    # quote and a space, through fish and reading the value back unchanged.
    # Reused for that reason, not because the two shells' quoting rules are
    # the same in general (they are not: fish also recognises \\' and \\\\
    # INSIDE single quotes, where POSIX sh's single quotes have no escapes at
    # all -- shquote()'s output just happens to be valid under both readings).
    cat <<BLOCK
# walk-blocker -- generated by install.sh, do not edit by hand.
# Reinstalling (deploy.py --system --i-have-approval) regenerates this file;
# editing it here will not survive that.
#
# Layer 1 of walk-blocker. ADVISORY: an absolute path to the real tool, a
# private PATH, a container or a batch script all go around it. See
# $PREFIX/README.md.
#
# fish reads every *.fish file under \$__fish_sysconf_dir/conf.d
# UNCONDITIONALLY -- every invocation, login or not, interactive or not,
# \`-c\` or not; fish's own documentation names ssh/scp/rsync non-interactive
# invocations as exactly the case this matters for. No SSH_CLIENT/SHLVL
# condition to satisfy: the same property zsh's zshenv has. A NEW file placed
# in conf.d is not part of the fish package's own file manifest, so a
# reinstall or upgrade of that package neither removes nor conflicts over it.
set -gx WALK_BLOCKER_SHIM_DIR $(shquote "$BIN")
set -gx WALK_BLOCKER_AUDIT $(shquote "$AUDIT")
if not contains -- \$WALK_BLOCKER_SHIM_DIR \$PATH
    set -gx PATH \$WALK_BLOCKER_SHIM_DIR \$PATH
end
BLOCK
}

strip_block() {
    _file=$1
    [ -f "$_file" ] || return 0
    # mktemp, not `> "$_file.walk-blocker.tmp"`. That would be a root
    # redirection to a PREDICTABLE name, and a plain `>` follows a symlink --
    # so in a shared directory another user could pre-create it pointing
    # anywhere and have this truncate the target. mktemp creates with O_EXCL,
    # mode 0600, and an unpredictable name, in the target's own directory so
    # the mv stays on one filesystem.
    _tmp=$(mktemp "$_file.walk-blocker.XXXXXX") || return 1
    awk -v b="$BEGIN" -v e="$END" '
        $0 == b { skip = 1 }
        skip != 1 { print }
        $0 == e { skip = 0 }
    ' "$_file" > "$_tmp"
    # mktemp makes it 0600; this file is read by every login shell, and its
    # mode must not depend on the invoking umask either.
    chmod 0644 "$_tmp"
    mv "$_tmp" "$_file"
}

prepend_block() {
    # prepend_block FILE BLOCK_FN -- BLOCK_FN is bashrc_block or zshenv_block,
    # a function name invoked as a plain command below. Everything here is
    # file-handling mechanics (the backup, the mktemp/chmod/mv race-avoidance)
    # that does not depend on which shell reads the result; only the CONTENT
    # written differs, and that stays in separate per-shell functions so a
    # change meant for one shell's prose cannot silently touch the other's.
    _file=$1
    _block_fn=$2
    # A snapshot of whatever this run found, taken before `touch` can create
    # the file it did not find and before this function's own write touches
    # it -- the directory is already proven trusted (root-owned, not group-
    # or other-writable) by require_plain_hook_file()'s ancestor check before
    # this function is ever called, the same property `touch` on the next
    # line already relies on, so a plain `cat > ...` needs no mktemp dance
    # of its own. Never overwritten on a later run: the point of a backup is
    # the state before walk-blocker ever touched this file, not the state
    # before its most recent reinstall. Absent entirely when `$_file` did not
    # exist -- a `.orig` snapshot of nothing would misreport "it existed and
    # was empty."
    if [ -e "$_file" ] && [ ! -e "$_file.walk-blocker.orig" ]; then
        cat "$_file" > "$_file.walk-blocker.orig"
        chmod 0644 "$_file.walk-blocker.orig"
    fi
    touch "$_file"
    strip_block "$_file"
    _tmp=$(mktemp "$_file.walk-blocker.XXXXXX") || return 1
    "$_block_fn" > "$_tmp"
    cat "$_file" >> "$_tmp"
    chmod 0644 "$_tmp"
    mv "$_tmp" "$_file"
}

write_fish_conf() {
    # write_fish_conf FILE BLOCK_FN -- deliberately not prepend_block reused a
    # third time. That function's whole shape (preserve existing content,
    # back it up once, strip a previous marked block before rewriting) exists
    # because the bash and zsh files are shared files this installer does not
    # own the rest of. The fish file is not shared: it is a dedicated snippet
    # under fish's own conf.d drop-in directory (see fish_conf_block), so
    # there is no pre-existing content to preserve, nothing to back up, and
    # no markers to strip -- the whole file is generated, every time.
    #
    # The refusal is require_plain_dropin()'s, applied here as well as by the
    # caller's pre-check, so it cannot be forgotten by a new caller.
    #
    # The directory is created here, not required to pre-exist: unlike the
    # bash and zsh files' parents, whether fish's conf.d exists at all depends
    # on whether the fish package happens to be installed right now, which
    # this repo does not control and does not assume. `install -d` sets the
    # mode on every directory it creates, intermediates included (unlike
    # os.makedirs on the Python side).
    _file=$1
    _block_fn=$2
    require_plain_dropin "$_file" 1 "hooks.fish.file"
    install -d -m 0755 "$(dirname "$_file")"
    _tmp=$(mktemp "$_file.walk-blocker.XXXXXX") || return 1
    "$_block_fn" > "$_tmp"
    chmod 0644 "$_tmp"
    mv "$_tmp" "$_file"
}

# Everything below exists so the installer cannot report an install it did
# not achieve. The same defect as an audit log claiming a kill that did not
# land.

path_without_bin() {
    # The probe must not be able to pass because the CURRENT shell already has
    # $BIN on PATH -- that would test nothing and pass anyway.
    _out=''
    _oifs=$IFS
    IFS=:
    for _d in $PATH; do
        [ -n "$_d" ] || _d=.
        [ "$_d" = "$BIN" ] && continue
        _out="${_out:+$_out:}$_d"
    done
    IFS=$_oifs
    printf '%s' "$_out"
}

assert_audit_dir() {
    # assert_audit_dir AUDIT_FILE -- create the audit directory, or put its
    # mode back, and say so in the journal if it had to.
    #
    # This directory is THIS PROJECT'S. Nothing but the root-run reaper and
    # this installer writes it, and it exists at all only because deploy.py
    # put the reaper on the node. That is what separates correcting it from
    # report_hook_state() deliberately refusing to repair a shell's system rc
    # file: that file is the distribution's and a package update is entitled
    # to it, so reporting is the honest limit there. Here, declining to fix
    # our own directory is not restraint; it is a monitor nobody can read.
    #
    # 0755, not 0750 (ADR-0012). The invariant ADR-0004 records is that a
    # MONITORED USER CANNOT WRITE here. Root ownership with no group/other `w`
    # is what carries that, and 0755 carries it identically -- an append needs
    # `w`, which 0755 still refuses. Layer 1's escape-hatch records go to
    # journald for exactly that reason. What 0750 additionally did was hide
    # the trail from the people who have to read it, including the account
    # that has to decide --kill.
    #
    # On a directory carrying an ACL the group bits ARE the mask. 0750 and
    # 0755 both leave that mask at r-x, so every named grant keeps precisely
    # the access it already has; what changes is `other`. This never reads or
    # writes an ACL -- a named grant is someone else's decision, not ours.
    _aad_dir=$(dirname "$1")
    case $_aad_dir in
        ''|.|/) return 0 ;;
    esac
    _aad_before=''
    [ -d "$_aad_dir" ] && _aad_before=$(stat -c '%u %a' "$_aad_dir" 2>/dev/null)
    install -d -m 0755 "$_aad_dir" || {
        sg_report audit_dir create-failed
        return 1
    }
    _aad_after=$(stat -c '%u %a' "$_aad_dir" 2>/dev/null) || _aad_after=''
    if [ -z "$_aad_before" ]; then
        sg_report audit_dir created
    elif [ "$_aad_before" != "$_aad_after" ]; then
        # Somebody changed it between polls and the tool has just changed it
        # back. Recorded rather than silent: a monitor that one chmod can
        # blind, quietly, is not a monitor -- and the record is the only way
        # anyone learns it was attempted.
        sg_report audit_dir mode-corrected
    fi
    # Ownership is reported, never seized. A directory owned by someone else
    # can still be replaced by its owner, so this matters -- but chowning it
    # is the kind of action deploy.py refuses the install over rather than
    # something an unattended poll should do on its own initiative.
    case $_aad_after in
        '0 '*) ;;
        '') sg_report audit_dir unreadable ;;
        *)  sg_report audit_dir owner-not-root ;;
    esac
    return 0
}

sg_report() {
    # sg_report ACTION STATE
    # sg_report uncovered_mount MOUNTPOINT FSTYPE expensive|covered|unmounted
    #
    # One line into `journalctl -t walk-blocker`, the tag the shim's
    # escape-hatch records already use. So one query answers what overrode
    # Layer 1, when Layer 1 stopped being installed, when its upkeep last
    # refused to run, and when a mount began or stopped running on its
    # default class.
    #
    # No timestamp field: journald stamps its own, and unlike the shim's file
    # sink there is no second sink here that would need one.
    #
    # Every field must be a literal from this file, an integer, or a value
    # that passes a conservative character check. There is no substring
    # replacement in POSIX sh and therefore no way to escape a caller-shaped
    # string for JSON here -- the shim needs awk for that. Rather than leave
    # that as a rule someone has to remember, anything outside the allowed
    # set is refused and the record says so: a record that does not parse is
    # worse than a coarse one, and a `caller-bug` state in the journal is a
    # bug report. A mount point or type off the mount table gets the same
    # treatment with `unrepresentable`; the octal-escaped rows that could
    # carry whitespace or a backslash are skipped before they get here.
    _rep_action=$1
    _rep_state=$2
    _rep_extra=''
    _rep_prio=user.warning
    if [ $# -eq 4 ]; then
        _rep_mount=$2
        _rep_fs=$3
        _rep_state=$4
        case $_rep_mount in
            ''|*[!A-Za-z0-9._/@+,:=-]*) _rep_mount=unrepresentable ;;
        esac
        case $_rep_fs in
            ''|*[!A-Za-z0-9._-]*) _rep_fs=unrepresentable ;;
        esac
        _rep_extra=',"mount":"'$_rep_mount'","fstype":"'$_rep_fs'"'
        # A mount on its default is a fact to act on out of band, not a
        # fault: notice, where a hook gone missing is a warning. Reported
        # on change, not on state (ADR-0019), so notice is not a place
        # where a repeating line goes to be ignored.
        _rep_prio=user.notice
    fi
    for _rep_arg in "$_rep_action" "$_rep_state"; do
        case $_rep_arg in
            ''|*[!A-Za-z0-9._-]*)
                _rep_action=caller-bug
                _rep_state=caller-bug
                _rep_extra=''
                break
                ;;
        esac
    done
    _rep_json='{"layer":"shim","action":"'$_rep_action'","state":"'$_rep_state'"'$_rep_extra'}'
    # The absolute path first, and the PATH lookup only as a fallback for a
    # host that keeps logger elsewhere -- the same shape, and the same reason,
    # as SG_LOGGER in guard.sh: an audit sink that resolves through PATH can
    # be shadowed by whatever set that PATH. This usually runs as root under
    # systemd's PATH, but the by-hand debug relink does not, and a sink is
    # not worth having two rules for.
    _rep_logger=$SG_LOGGER
    if [ ! -x "$_rep_logger" ]; then
        _rep_logger=$(command -v logger 2>/dev/null) || _rep_logger=''
    fi
    if [ -n "$_rep_logger" ]; then
        "$_rep_logger" -t walk-blocker -p "$_rep_prio" -- "$_rep_json" \
            2>/dev/null || true
    fi
    return 0
}

sg_relink_exit() {
    # EXIT-trap handler for the relink arm, installed before the first check
    # that can refuse.
    #
    # The unit's ExecStartPre is `-` prefixed so that a Layer 1 refusal cannot
    # stop Layer 2 -- which also means systemd no longer surfaces the refusal
    # at all, and a quiet failure made quieter is worse unless something says
    # so. The refusal messages already reach the journal on stderr, under the
    # unit; this puts a structured record under the walk-blocker tag, where
    # someone auditing Layer 1 will actually be looking.
    #
    # PRESERVES the status. `install.sh --relink` still exits 3 for a human
    # running it by hand. `exit` inside an EXIT trap does not re-enter the
    # trap.
    #
    # A plain `if`, not `[ ... ] && return`: whether `set -e` acts on a failing
    # AND-OR list is shell-dependent.
    if [ "$1" -eq 0 ]; then
        return 0
    fi
    sg_report relink_refused "exit-$1"
    exit "$1"
}

report_hook_state() {
    # report_hook_state FILE VERIFY_FN PKG_NAME SHELL
    #
    # Is Layer 1's PATH hook still there? Generic over which block-style hook,
    # since the marker scan and the report/never-repair/never-fail policy
    # below do not depend on which shell reads the result -- only which
    # function proves it fires ($2) and which package's file it is ($3).
    #
    # A shell's system rc file is typically protected configuration of that
    # shell's package. Installing a block makes it modified, and the next
    # upgrade of that package is a conflict with two outcomes, neither
    # detected without a check: an interactive upgrade prompts, and an
    # administrator taking the maintainer's version deletes the block; an
    # unattended upgrade hits the same conflict and defers the update. So the
    # likeliest remover of this hook is a routine update of $3, and this
    # runs on every poll so that removal is noticed within one.
    #
    # REPORTS. Never repairs, and never fails:
    #
    #   * not repairs, because rewriting a root-sourced hook file unattended
    #     is a larger action than this has standing to take, and the install
    #     path's own checks (symlink, plain file, ownership) exist because
    #     that write is dangerous
    #   * not fails. A Layer 1 diagnosis has no business being able to end a
    #     Layer 2 poll, whatever the unit file happens to say this week
    #
    # Silent when healthy. The alternative is one journal line per poll
    # asserting a fact that is only interesting when it is false.
    _hook_file=$1
    _verify_fn=$2
    _hook_pkg=$3
    _hook_shell=$4
    _hook_state=''

    if [ ! -e "$_hook_file" ]; then
        _hook_state=file-absent
    elif [ ! -f "$_hook_file" ]; then
        # A fifo, a directory, a device node. Distinguished from absent
        # because the two mean different things to whoever reads the journal
        # -- one says a package update took the file, the other says
        # something is at that path that should not be.
        #
        # It also matters that this arm exists at all: `-f` is what keeps the
        # read below off a FIFO, which would block this function, hang
        # ExecStartPre and stop the reaper on every poll.
        # require_plain_hook_file() makes that check on the writing paths;
        # this is the reading one, and it needs its own.
        _hook_state=not-a-regular-file
    else
        # Read it rather than shelling out to grep: the file is small, this
        # runs as root on every poll, and `grep` here would resolve through a
        # PATH that may well have our own shim in front of it.
        _begin=0
        _end=0
        while IFS= read -r _line; do
            case $_line in
                "$BEGIN") _begin=1 ;;
                "$END") _end=1 ;;
            esac
        done < "$_hook_file"
        if [ "$_begin" -eq 1 ] && [ "$_end" -eq 1 ]; then
            _hook_state=present
        else
            _hook_state=block-missing
        fi
    fi

    # The markers being present is necessary, not sufficient -- the shell has
    # to actually source the file for a non-interactive remote command. Only
    # worth probing when the block is there; when it is not, the marker check
    # is already the more precise diagnosis.
    if [ "$_hook_state" = present ]; then
        if "$_verify_fn" >/dev/null 2>&1; then
            return 0
        fi
        _hook_state=not-firing
    fi

    sg_report hook_check "$_hook_state"
    echo "walk-blocker: $_hook_shell PATH hook $_hook_state in $_hook_file" >&2
    echo "  Layer 1 is not reaching \`ssh host 'cmd'\` for $_hook_shell. Reinstall" >&2
    echo "  with deploy.py --system --i-have-approval, as root." >&2
    if [ "$_hook_state" = block-missing ]; then
        echo "  $_hook_file may be configuration owned by the $_hook_pkg package;" >&2
        echo "  an upgrade of $_hook_pkg that took the maintainer's version is the" >&2
        echo "  likeliest thing to have removed the block. See ADR-0008." >&2
    fi
    return 0
}

report_dropin_state() {
    # report_dropin_state FILE VERIFY_FN SHELL -- simpler than
    # report_hook_state, because a drop-in carries no $BEGIN/$END markers to
    # scan for (it is a dedicated file, not shared content -- see
    # write_fish_conf()). "Does it exist as a regular file, and does it
    # verify" is the whole state. Same never-repair-never-fail policy.
    _hook_file=$1
    _verify_fn=$2
    _hook_shell=$3
    if [ ! -e "$_hook_file" ]; then
        _hook_state=file-absent
    elif [ ! -f "$_hook_file" ]; then
        _hook_state=not-a-regular-file
    elif "$_verify_fn" >/dev/null 2>&1; then
        return 0
    else
        _hook_state=not-firing
    fi
    sg_report hook_check "$_hook_state"
    echo "walk-blocker: $_hook_shell PATH hook $_hook_state in $_hook_file" >&2
    echo "  Reinstall with deploy.py --system --i-have-approval, as root, to" >&2
    echo "  regenerate it." >&2
    return 0
}

report_hook() {
    # report_hook SHELL -- the reconcile's per-shell state report.
    hook_select "$1"
    case $HK_KIND in
        block)  report_hook_state "$HK_FILE" "$HK_VERIFY_FN" "$HK_PKG" "$1" ;;
        dropin) report_dropin_state "$HK_FILE" "$HK_VERIFY_FN" "$1" ;;
    esac
}

sg_find_linked_probe() {
    # Print the first wrapped name that is actually linked under $BIN, or
    # fail if none is. Shared by every verify function -- identical either
    # way, since it says nothing about which shell will go on to probe the
    # result.
    #
    # A plain if, not `[ ... ] && { ...; }`: that form leaves the loop body's
    # last command failing on every miss, and whether `set -e` acts on that is
    # shell-dependent.
    for _name in $SG_WRAPPED_NAMES; do
        if [ -L "$BIN/$_name" ]; then
            printf '%s' "$_name"
            return 0
        fi
    done
    return 1
}

verify_nothing_linked() {
    # verify_nothing_linked FILE -- the failure every verify function shares
    # when there is no shim to probe for. By the time this is reached in an
    # approved install, the hook is written: every login shell's PATH gets
    # $BIN prepended -- to a directory with no shims in it. Not a rollback:
    # this repo's policy is to report a live partial state accurately rather
    # than silently undo it. `--uninstall` reverses it.
    echo "walk-blocker: nothing was linked, so there is nothing to verify" >&2
    echo "walk-blocker: $1 was still written. Nothing wrapped means" >&2
    echo "  nothing here to protect, but the hook is live -- \`install.sh" >&2
    echo "  --uninstall\` removes it if that is not wanted." >&2
}

verify_not_rolled_back() {
    # verify_not_rolled_back FILE PROBE -- the epilogue every verify failure
    # shares. Said plainly rather than rolled back: a probe only speaks to the
    # non-interactive `ssh host 'cmd'` path, and the shims plus the hook are
    # real, live protection for the interactive one regardless of what the
    # probe found. Tearing that down to "clean up" a failure specific to one
    # path would remove working protection over a partial one.
    echo "" >&2
    echo "  This install is NOT rolled back: $2 is linked under $BIN, and the" >&2
    echo "  $1 hook is live -- interactive shells are protected right" >&2
    echo "  now. \`install.sh --uninstall\` removes both if that is not wanted." >&2
}

verify_bash_hook() {
    _probe=$(sg_find_linked_probe) || {
        verify_nothing_linked "$BASHRC_FILE"
        return 1
    }
    # A required shell's binary being absent is a hard verify failure: an
    # automatic pass on "absent" would silently convert "unchecked" into
    # "verified". A best-effort shell never reaches here without resolving.
    if ! command -v bash >/dev/null 2>&1; then
        echo "walk-blocker: no bash on PATH; cannot verify the hook fires" >&2
        return 1
    fi

    # Reproduce the shape a remote command actually arrives in. bash sources
    # its system rc file when non-interactive iff ALL of:
    #   - built with SSH_SOURCE_BASHRC, AND
    #   - SSH_CLIENT or SSH2_CLIENT is set, OR stdin is a socket, AND
    #   - SHLVL < 2 -- it only does this for a TOP-LEVEL shell.
    # SHLVL=0 is therefore load-bearing here: this function is itself running
    # inside a shell, so without it the probe inherits SHLVL>=1, bash declines,
    # and the gate fails every time for a reason that has nothing to do with
    # whether the install worked.
    # stdin is /dev/null so that only the SSH_CLIENT path can satisfy the test
    # -- a socket on stdin would let it pass without proving anything about the
    # variable the transports actually rely on. BASH_ENV is blanked so a hit
    # cannot come from some other sourced file.
    # shellcheck disable=SC1007  # deliberate: BASH_ENV set to empty, not a
    # typo for BASH_ENV=SHLVL -- these are two separate assignments.
    _got=$(BASH_ENV= SHLVL=0 PATH="$(path_without_bin)" \
           SSH_CLIENT="${SSH_CLIENT:-127.0.0.1 0 22}" \
           bash -c "command -v $_probe" 2>/dev/null < /dev/null) || _got=''

    case $_got in
        "$BIN"/*)
            echo "walk-blocker: verified -- a non-interactive bash resolves $_probe to $_got"
            return 0
            ;;
    esac

    echo "walk-blocker: FAILED to verify the bash hook fires. Not installed usefully." >&2
    echo "  probed: BASH_ENV= SHLVL=0 PATH=<\$PATH minus $BIN> SSH_CLIENT=... \\" >&2
    echo "            bash -c 'command -v $_probe' < /dev/null" >&2
    echo "  wanted: a path under $BIN" >&2
    echo "  got:    ${_got:-<nothing>}" >&2
    echo "" >&2
    echo "  The $BASHRC_FILE block was written, but a non-interactive shell is not" >&2
    echo "  reading it. Either bash is built without SSH_SOURCE_BASHRC, or the SSH" >&2
    echo "  server no longer sets SSH_CLIENT. Layer 1 would still work in" >&2
    echo "  interactive shells and be silently absent for \`ssh host 'cmd'\` --" >&2
    echo "  the incident shape. See ADR-0008." >&2
    verify_not_rolled_back "$BASHRC_FILE" "$_probe"
    return 1
}

verify_zsh_hook() {
    # Deliberately NOT a copy of verify_bash_hook's gating: zsh reads the
    # global zshenv unconditionally (see zshenv_block()'s comment), so there
    # is no SSH_CLIENT/SHLVL condition to reproduce here -- doing so anyway
    # would assume a condition that does not exist, the mistake this whole
    # verify-not-assume discipline exists to avoid.
    _probe=$(sg_find_linked_probe) || {
        verify_nothing_linked "$ZSHENV_FILE"
        return 1
    }
    if ! command -v zsh >/dev/null 2>&1; then
        echo "walk-blocker: no zsh on PATH; cannot verify the hook fires" >&2
        return 1
    fi

    # path_without_bin(), same reason as verify_bash_hook: the probe must not
    # pass merely because the CALLER's shell already has $BIN on PATH.
    # ZDOTDIR is blanked so a stray per-user zshenv (root's own, or a leftover
    # from a prior by-hand test) cannot supply a false pass or mask a false
    # fail -- the point is to prove the SYSTEM file fires, not some per-user
    # one. No BASH_ENV-equivalent to blank: zsh has no environment variable
    # that redirects which rc file is read, so there is nothing analogous to
    # neutralize.
    # shellcheck disable=SC1007  # deliberate: ZDOTDIR set to empty, not a
    # typo -- same false positive as verify_bash_hook's BASH_ENV= above.
    _got=$(PATH="$(path_without_bin)" ZDOTDIR= \
           zsh -c "command -v $_probe" 2>/dev/null < /dev/null) || _got=''

    case $_got in
        "$BIN"/*)
            echo "walk-blocker: verified -- a non-interactive zsh resolves $_probe to $_got"
            return 0
            ;;
    esac

    echo "walk-blocker: FAILED to verify the zsh hook fires. Not installed usefully." >&2
    echo "  probed: PATH=<\$PATH minus $BIN> ZDOTDIR= \\" >&2
    echo "            zsh -c 'command -v $_probe' < /dev/null" >&2
    echo "  wanted: a path under $BIN" >&2
    echo "  got:    ${_got:-<nothing>}" >&2
    echo "" >&2
    echo "  The $ZSHENV_FILE block was written, but a non-interactive zsh is not" >&2
    echo "  reading it, which zsh should do UNCONDITIONALLY regardless of how it" >&2
    echo "  was invoked. See ADR-0008." >&2
    verify_not_rolled_back "$ZSHENV_FILE" "$_probe"
    return 1
}

verify_fish_hook() {
    # Same shape as verify_zsh_hook -- no SSH_CLIENT/SHLVL condition to
    # reproduce, since conf.d is read unconditionally too. Whether a failure
    # here gates the install is the site's `[hooks.fish].gate`, decided by
    # the caller, not by this function.
    _probe=$(sg_find_linked_probe) || {
        verify_nothing_linked "$FISH_CONF_FILE"
        return 1
    }
    if ! command -v fish >/dev/null 2>&1; then
        echo "walk-blocker: no fish on PATH; cannot verify the hook fires" >&2
        return 1
    fi

    _got=$(PATH="$(path_without_bin)" \
           fish -c "command -v $_probe" 2>/dev/null < /dev/null) || _got=''

    case $_got in
        "$BIN"/*)
            echo "walk-blocker: verified -- a non-interactive fish resolves $_probe to $_got"
            return 0
            ;;
    esac

    echo "walk-blocker: FAILED to verify the fish hook fires. Not installed usefully." >&2
    echo "  probed: PATH=<\$PATH minus $BIN> fish -c 'command -v $_probe' < /dev/null" >&2
    echo "  wanted: a path under $BIN" >&2
    echo "  got:    ${_got:-<nothing>}" >&2
    echo "" >&2
    echo "  $FISH_CONF_FILE was written, but a non-interactive fish is not" >&2
    echo "  reading it, which fish should do UNCONDITIONALLY for every file in" >&2
    echo "  its conf.d. See ADR-0008." >&2
    verify_not_rolled_back "$FISH_CONF_FILE" "$_probe"
    return 1
}

verify_hooks() {
    # Every REQUIRED hook, without short-circuiting, so a single failed
    # install reports every problem it has rather than the first one only --
    # the whole point being to not make an operator fix one hook, rerun, and
    # only then discover another was broken too. Best-effort hooks are the
    # caller's business and never gate.
    _hooks_ok=0
    for _vh in $SG_HOOKS_REQUIRED; do
        hook_select "$_vh"
        "$HK_VERIFY_FN" || _hooks_ok=1
    done
    return "$_hooks_ok"
}

# --------------------------------------------------------------------------
# the mount table (ADR-0016, tier three)
# --------------------------------------------------------------------------

sg_default_class() {
    # sg_default_class SOURCE FSTYPE OPTIONS -- sets sg_class to `expensive`
    # or `cheap`: the tier-one default the shim applies to a mount no
    # override covers, decided from the mount table's fields and the compiled
    # site values alone. No statfs, no measurement, nothing that can block on
    # the filesystem being judged (ADR-0015). The same tiers, in the same
    # order, as guard.sh's sg_classify minus the override tier, which the
    # caller has already settled:
    #   1. a type matching any remote_fstypes glob is expensive;
    #   2. else, with remote_proxy, a `host:` source or a `_netdev` / `addr=`
    #      option marks the mount remote, and remote-unknown is expensive;
    #   3. else cheap.
    sg_class=''
    # Colon-joined at build; walked by trimming, never word-split, so `fuse.*`
    # reaches `case` as a pattern and is never expanded against a directory.
    _dc_rest=$SG_REMOTE_FSTYPES
    while [ -n "$_dc_rest" ]; do
        _dc_one=${_dc_rest%%:*}
        case $_dc_rest in
            *:*) _dc_rest=${_dc_rest#*:} ;;
            *) _dc_rest='' ;;
        esac
        [ -n "$_dc_one" ] || continue
        # shellcheck disable=SC2254  # a glob, matched as one on purpose
        case $2 in
            $_dc_one) sg_class=expensive; return 0 ;;
        esac
    done
    if [ "$SG_REMOTE_PROXY" = true ]; then
        # `^[^/]+:` -- a source spelled host:path. The part before the first
        # colon must be non-empty and contain no slash, so `//srv/share` and
        # `/dev/sda1` are local and `srv:/export` is not.
        _dc_head=${1%%:*}
        if [ "$_dc_head" != "$1" ] && [ -n "$_dc_head" ]; then
            case $_dc_head in
                */*) ;;
                *) sg_class=expensive; return 0 ;;
            esac
        fi
        _dc_opts=$3
        while [ -n "$_dc_opts" ]; do
            _dc_opt=${_dc_opts%%,*}
            case $_dc_opts in
                *,*) _dc_opts=${_dc_opts#*,} ;;
                *) _dc_opts='' ;;
            esac
            case $_dc_opt in
                _netdev|addr=*) sg_class=expensive; return 0 ;;
            esac
        done
    fi
    sg_class=cheap
}

# Report-on-change memory for report_uncovered_mounts() (ADR-0019). One
# line per mount the last relink found uncovered, after a first line naming
# the boot it was written under. It lives in the spool because the spool is
# asserted on every root relink and survives a reboot; the boot line is what
# makes a reboot report every uncovered mount once more, since a journal on
# volatile storage has forgotten the earlier line.
UNCOVERED_STATE=$SG_SPOOL_DIR/uncovered-mounts.state

uncovered_boot_id() {
    # Sets sg_boot to the kernel's boot id, or to empty where it cannot be
    # read -- then every relink is treated as the same boot and the state
    # is trusted as it stands.
    sg_boot=''
    [ -r /proc/sys/kernel/random/boot_id ] || return 0
    read -r sg_boot < /proc/sys/kernel/random/boot_id || sg_boot=''
    return 0
}

uncovered_listed_in() {
    # uncovered_listed_in FILE MOUNTPOINT FSTYPE -- does FILE, in the state
    # format, list this mount? The boot line cannot match: a mount point is
    # absolute and "boot" is not.
    [ -r "$1" ] || return 1
    while read -r _ul_mnt _ul_fs _ul_rest; do
        if [ "$_ul_mnt" = "$2" ] && [ "$_ul_fs" = "$3" ]; then
            return 0
        fi
    done < "$1"
    return 1
}

uncovered_mount_in_table() {
    # uncovered_mount_in_table MOUNTPOINT -- is it still in the live table?
    # Read with the same trailing-slash rule the report applies, so a mount
    # recorded as `/x` is found when the table spells it `/x/`.
    [ -r "$SG_MOUNT_TABLE" ] || return 1
    while read -r _ut_src _ut_mnt _ut_rest; do
        case $_ut_mnt in
            /) ;;
            */) _ut_mnt=${_ut_mnt%/} ;;
        esac
        [ "$_ut_mnt" = "$1" ] && return 0
    done < "$SG_MOUNT_TABLE"
    return 1
}

report_uncovered_mounts() {
    # One journald line per CHANGE in the set of mounts in the live table
    # that no `[[filesystems.mounts]]` override covers AND that the tier-one
    # default guards (ADR-0016, ADR-0019): `expensive` when a mount is first
    # seen running on its default, `covered` when an override or a narrower
    # default has since taken it over, `unmounted` when it has left the
    # table. A steady state is silent. A line that repeated identically on
    # every poll trained readers to filter the tag, which is the failure
    # ADR-0001 describes for Layer 1, arriving in the journal instead.
    #
    # A cheap-by-default local mount (tmpfs, proc, an overlay) cannot
    # produce a false refusal and is never reported: one line per
    # pseudo-filesystem would bury the line this record exists to surface.
    #
    # The memory is $UNCOVERED_STATE. Where it can be written -- the root
    # relink, whose spool assert_audit_dir() has just asserted -- the report
    # is on change. Where it cannot -- the unprivileged debug relink, a spool
    # not yet created -- every poll reports the whole set, as it did before
    # the state existed, rather than say nothing. A state written under an
    # earlier boot is read as no state: everything current is reported once
    # and nothing is reported as covered or unmounted, since the table has
    # changed for reasons of its own.
    #
    # Never fails, never refuses: a table that cannot be read is a mount
    # judgement the shim also cannot make, and the shim's own seams already
    # audit that. Plain reads of small files; it costs the poll nothing it
    # would notice.
    [ -r "$SG_MOUNT_TABLE" ] || return 0
    uncovered_boot_id
    _um_new=''
    if [ -d "$SG_SPOOL_DIR" ] && : > "$UNCOVERED_STATE.new" 2>/dev/null; then
        _um_new=$UNCOVERED_STATE.new
        # World-readable like the spool around it (ADR-0012), whatever the
        # caller's umask: systemd's default and a root shell's differ, and
        # the person reading the journal should be able to read what the
        # relink currently believes without being root.
        chmod 0644 "$_um_new" 2>/dev/null || :
        printf 'boot %s\n' "$sg_boot" > "$_um_new"
    fi
    # Fresh means: report everything current, compare against nothing.
    _um_fresh=1
    if [ -n "$_um_new" ] && [ -r "$UNCOVERED_STATE" ]; then
        read -r _um_word _um_prev_boot _um_rest < "$UNCOVERED_STATE" || _um_word=''
        if [ "$_um_word" = boot ] && [ "$_um_prev_boot" = "$sg_boot" ]; then
            _um_fresh=0
        fi
    fi
    while read -r _um_src _um_mnt _um_fs _um_opts _um_rest; do
        [ -n "$_um_fs" ] || continue
        # An octal-escaped mount point (a space, a tab, a newline or a
        # backslash in the path) is skipped, as the shim's reader skips it:
        # nothing here can un-escape it, and an unescaped one cannot be put
        # in a record safely.
        case $_um_mnt in
            *\\*) continue ;;
        esac
        # ONE trailing slash stripped, as the shim keys its mounts; `/` stays.
        case $_um_mnt in
            /) ;;
            */) _um_mnt=${_um_mnt%/} ;;
        esac
        _um_covered=0
        for _um_o in $SG_MOUNT_OVERRIDES; do
            if [ "${_um_o%%=*}" = "$_um_mnt" ]; then
                _um_covered=1
                break
            fi
        done
        [ "$_um_covered" -eq 0 ] || continue
        sg_default_class "$_um_src" "$_um_fs" "$_um_opts"
        [ "$sg_class" = expensive ] || continue
        [ -z "$_um_new" ] || printf '%s %s\n' "$_um_mnt" "$_um_fs" >> "$_um_new"
        if [ "$_um_fresh" -eq 1 ] \
                || ! uncovered_listed_in "$UNCOVERED_STATE" "$_um_mnt" "$_um_fs"; then
            sg_report uncovered_mount "$_um_mnt" "$_um_fs" expensive
        fi
    done < "$SG_MOUNT_TABLE"
    if [ -n "$_um_new" ] && [ "$_um_fresh" -eq 0 ]; then
        # What the last relink reported and this one did not: the earlier
        # line is answered once, with which way it was resolved.
        while read -r _um_omnt _um_ofs _um_rest; do
            case $_um_omnt in
                /*) ;;
                *) continue ;;
            esac
            uncovered_listed_in "$_um_new" "$_um_omnt" "$_um_ofs" && continue
            if uncovered_mount_in_table "$_um_omnt"; then
                sg_report uncovered_mount "$_um_omnt" "$_um_ofs" covered
            else
                sg_report uncovered_mount "$_um_omnt" "$_um_ofs" unmounted
            fi
        done < "$UNCOVERED_STATE"
    fi
    if [ -n "$_um_new" ]; then
        mv -f "$_um_new" "$UNCOVERED_STATE" 2>/dev/null || rm -f "$_um_new"
    fi
    : "$_um_rest"
    return 0
}

# --------------------------------------------------------------------------
# the three arms
# --------------------------------------------------------------------------

case $MODE in
    relink)
        # --relink is what the systemd unit runs AS ROOT on every poll. Run
        # as root out of a checkout -- by an operator following the usage
        # text, or by mistake -- link_farm would repoint every shim at the
        # checkout's guard.sh, handing its owner the code every user's shell
        # executes. The legitimate caller is `$PREFIX/shim/install.sh
        # --relink`, which satisfies this by construction.
        #
        # Root only: the unprivileged relink is the test and debug path, and a
        # non-root run cannot write $PREFIX/bin on a real install anyway.
        #
        # The unit's ExecStartPre is `-` prefixed and timeout-bounded, so a
        # refusal here stops the relink and nothing else, and it is reported
        # to `journalctl -t walk-blocker` so that ignoring it at the unit
        # level does not make it silent. Refusing remains right: a payload
        # that fails these checks is one nobody should be linking every
        # user's PATH at, and failing loudly beats relinking quietly at code
        # someone else controls.
        # Before the first check that can refuse, so every non-zero exit from
        # this arm is reported on its way out. See sg_relink_exit().
        trap 'sg_relink_exit $?' EXIT
        if is_root; then
            require_deployed_copy "--relink"
        fi
        # On every poll, not just at install. The hooks below are REPORTED
        # and never repaired because their files are the distribution's; the
        # audit directory is ours, so a chmod that blocks the tool from its
        # own records is corrected here and recorded in the same journal
        # (ADR-0012). Non-fatal by construction: a failure to fix it must not
        # take the relink -- or, through ExecStartPre, Layer 2 -- down with it.
        if is_root; then
            assert_audit_dir "$AUDIT" || :
        fi
        link_farm "$BIN"
        # The symlink farm is half of Layer 1; the hook blocks that put those
        # symlinks on anyone's PATH are the half a routine package update can
        # remove, so both are checked here. Reports; never repairs, never
        # fails. See report_hook_state().
        for _rh in $SG_HOOKS_REQUIRED; do
            report_hook "$_rh"
        done
        # Best-effort and gated on presence: a node without that shell does
        # not get a "file absent" report on every poll forever for a file it
        # was never going to need.
        for _rh in $SG_HOOKS_BEST_EFFORT; do
            if command -v "$_rh" >/dev/null 2>&1; then
                report_hook "$_rh"
            fi
        done
        # And the mount table: which mounts have begun, or stopped, being
        # guarded on their default rather than by a reviewed override
        # (ADR-0016, ADR-0019).
        report_uncovered_mounts
        ;;
    system)
        # The two values that get embedded in the sourced block. Checked in
        # preview too: the preview PRINTS the block, so a truncated value
        # there would advertise a path the install would not use.
        require_no_newline "$PREFIX" "install.prefix"
        require_no_newline "$AUDIT" "install.spool_dir/install.audit_filename"
        # A preview writes nothing; an approved install does. Every required
        # hook's file is checked; a best-effort hook's only when its shell
        # resolves, because that is the only case in which it is written.
        for _ch in $SG_HOOKS_REQUIRED; do
            check_hook_file "$_ch" "$APPROVED"
        done
        for _ch in $SG_HOOKS_BEST_EFFORT; do
            if command -v "$_ch" >/dev/null 2>&1; then
                check_hook_file "$_ch" "$APPROVED"
            fi
        done
        # See require_deployed_copy(). Two arms need this, so it is a
        # function rather than a second copy of the same lines -- install and
        # uninstall diverge when written twice.
        if [ "$APPROVED" -eq 1 ]; then
            require_deployed_copy "--system --i-have-approval"
        fi
        if [ "$APPROVED" -eq 1 ] && is_root; then
            # Root-owned and not user-writable, which is the invariant --
            # NOT a particular mode. See assert_audit_dir().
            assert_audit_dir "$AUDIT"
            # A fresh install forgets which mounts an earlier one reported,
            # so the first poll after it names every mount on its default
            # once (ADR-0019). The audit trail beside it is left alone.
            rm -f "$UNCOVERED_STATE"
            link_farm "$BIN"
            for _wh in $SG_HOOKS_REQUIRED; do
                write_hook "$_wh"
            done
            verify_hooks || exit 4
            # Best-effort, gated on presence, and outside the gate above: a
            # best-effort shell's absence is the ordinary case at this site,
            # not a reason to fail an otherwise-good install, and its hook
            # failing to verify is a warning (ADR-0008).
            for _wh in $SG_HOOKS_BEST_EFFORT; do
                if command -v "$_wh" >/dev/null 2>&1; then
                    write_hook "$_wh" " (best-effort)"
                    hook_select "$_wh"
                    "$HK_VERIFY_FN" || echo "walk-blocker: the $_wh hook did not verify -- best-effort, not fatal; see ADR-0008" >&2
                fi
            done
            echo "walk-blocker: system-wide install complete under $PREFIX"
        elif [ "$APPROVED" -eq 1 ]; then
            echo "install.sh: --i-have-approval given, but this must run as root" >&2
            exit 3
        else
            cat <<SYS
# System-wide install of walk-blocker Layer 1, under $PREFIX.
#
# Root is required to actually install. As root, from the built payload:
#
#   python3 deploy.py --system --i-have-approval
#
# NOT this script run directly, which always refuses an approved install
# from anywhere but the deployed \$PREFIX/shim copy: link_farm points every
# shim at \$HERE/guard.sh, and a checkout is writable by the account that
# owns it. deploy.py stages the payload, makes it root-owned, verifies that,
# and then runs the copy.
#
# This will:
#   - symlink whichever of the wrapped names resolve on this node into
#     $BIN, and walk-job unconditionally
#   - write a PATH block into each REQUIRED hook file, and verify each one
#     actually fires before claiming success rather than assume it -- a hook
#     that cannot be proven fails the install (ADR-0008):
SYS
            for _pv in $SG_HOOKS_REQUIRED; do
                hook_select "$_pv"
                echo "#       $_pv: $HK_FILE"
            done
            cat <<SYS
#     (for bash, ABOVE its \`[ -z "\$PS1" ] && return\`-style guard: a
#     non-interactive \`ssh host 'cmd'\` returns before anything below it
#     runs, so the block has to come first; zsh reads its global zshenv and
#     fish its conf.d unconditionally, so there is no guard to out-race)
#   - write and check each BEST-EFFORT hook whose shell is on PATH; its
#     absence does not fail the install, and presence-but-broken is a
#     warning:
SYS
            for _pv in $SG_HOOKS_BEST_EFFORT; do
                hook_select "$_pv"
                echo "#       $_pv: $HK_FILE"
            done
            cat <<SYS
#   - point WALK_BLOCKER_AUDIT at $AUDIT
#
# Reverting: as root, from the built payload (it also removes the systemd
# unit):
#   python3 deploy.py --uninstall
# or just this installer's own half, from the DEPLOYED copy:
#   sh $PREFIX/shim/install.sh --uninstall
#
# Not from a checkout: a root teardown sources wrapped_names.sh, and
# load_wrapped_names() refuses one that is not root-owned -- which a
# checkout's copy is not. deploy.py --uninstall runs the deployed helper for
# the same reason.
SYS
        fi
        ;;
    uninstall)
        if ! is_root; then
            echo "install.sh: --uninstall must run as root" >&2
            exit 3
        fi
        # ALWAYS 1: strip_block() rewrites the file, so every check that
        # protects a write applies here, whether or not --i-have-approval was
        # given. Uninstall already requires root, so this path is as
        # privileged as the install.
        #
        # Every enabled hook, not gated on the shell still resolving: an
        # uninstall cleans up what was written; it does not condition that on
        # the reader still being around (ADR-0008).
        _removed=''
        for _uh in $SG_HOOKS_REQUIRED $SG_HOOKS_BEST_EFFORT; do
            hook_select "$_uh"
            if [ "$HK_KIND" = block ]; then
                require_plain_hook_file "$HK_FILE" 1 "$HK_KEY"
            fi
        done
        load_wrapped_names
        for _uh in $SG_HOOKS_REQUIRED $SG_HOOKS_BEST_EFFORT; do
            hook_select "$_uh"
            case $HK_KIND in
                block) strip_block "$HK_FILE" ;;
                # Plain `rm -f`, not strip_block: there are no markers to
                # strip out of shared content, because the drop-in was never
                # shared content -- see write_fish_conf(). A symlink there is
                # just removed, not followed.
                dropin) rm -f "$HK_FILE" ;;
            esac
            _removed="$_removed $HK_FILE,"
        done
        # Every symlink, not every CURRENTLY wrapped name: a name this repo
        # has stopped wrapping since the install still has a shim here, and
        # walking the table would leave it behind -- along with the directory,
        # since the rmdir below would then fail into its own `|| true`.
        sweep_unclaimed "$BIN" ""
        rmdir "$BIN" 2>/dev/null || true
        # The report-on-change memory is upkeep state, not a record; the
        # spool stays for the audit trail it holds.
        rm -f "$UNCOVERED_STATE"
        echo "walk-blocker: removed from$_removed and $BIN"
        echo "walk-blocker: $SG_SPOOL_DIR left in place; it holds the audit trail"
        ;;
esac
