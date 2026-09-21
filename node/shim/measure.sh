#!/bin/sh
# What the shim costs on the fast path, measured where it matters.
#
# The claim being tested is that `sh` is cheap enough to sit in front of grep
# on a node where grep is on the hot path of thousands of pipelines, and that
# a Python shim would not be (ADR-0015). Run this on the login node the shim
# will guard, from local disk; a workstation number is not the number.
#
#   sh shim/measure.sh /path/to/guard.sh [N] [BUDGET_MS] [GUARDED_BUDGET_MS]
#   sh shim/measure.sh --against /path/to/old-guard.sh /path/to/guard.sh \
#                      [N] [PAIRS] [MAX_RATIO] [GUARDED_MAX_RATIO]
#
# The budgets and ceilings are ARGUMENTS, not constants: they belong to the
# site that measured them and are recorded beside that site's own
# configuration, never in this tree (ADR-0014).
#
# TWO GATES, because they catch different failures and neither subsumes the
# other:
#
#   ABSOLUTE   With BUDGET_MS, exits non-zero when the shim's overhead over the
#              bare binary exceeds it. Without that gate this only ever printed
#              numbers, and a quoted fast-path figure went stale while the
#              shim grew severalfold. A performance claim nothing enforces is a
#              comment, not a budget.
#   RATIO      With --against, measures an OLD guard.sh and the new one
#              alternately and gates on the median per-pair ratio. This is the
#              gate that survives a change of machine: the absolute reading
#              moves with the node's load -- at a reference deployment the
#              SAME shim read measurably slower under a higher load average,
#              unchanged code, a range and not a number -- while the ratio
#              between two shims measured in the same minute does not.
#
# Keep both. A ratio gate alone cannot see cumulative drift -- five changes at
# a few percent each pass every ratio check and walk the absolute budget off a
# cliff -- and an absolute gate alone cannot be run anywhere but the machine
# it was calibrated on.
#
# TWO PATHS are measured, because for most of the wrapped tools the cheap one
# is unreachable:
#
#   FAST     `grep PATTERN` with no -r. Decided before any file is opened, and
#            the majority of grep calls on a login node.
#   GUARDED  an ALLOWED traversing call (`du -sh CHEAPDIR`). find, du, rg, fd,
#            tree and fzf are all `always=True` in the rule table, so this is
#            the ONLY path they ever take -- and the allowed case pays the
#            most, because a refusal returns early and an allowed one judges
#            every operand.
#
# Quoting only the fast-path number invites the reader to believe it covers
# everything, which is how a small fast-path figure came to describe a shim
# that cost several times that for `du -sh onedir`. The per-operand slope is
# measured too, since that cost used to be paid once per root.

set -u

usage() {
    echo "usage: measure.sh GUARD [N] [BUDGET_MS] [GUARDED_BUDGET_MS]" >&2
    echo "       measure.sh --against REF_GUARD GUARD [N] [PAIRS] [MAX_RATIO] [GUARDED_MAX_RATIO]" >&2
}

REF=''
if [ "${1:-}" = "--against" ]; then
    REF=${2:-}
    if [ -z "$REF" ]; then usage; exit 2; fi
    shift 2
fi
GUARD=${1:-./guard.sh}
N=${2:-200}
if [ -n "$REF" ]; then
    PAIRS=${3:-6}
    MAX_RATIO=${4:-}
    GUARDED_MAX_RATIO=${5:-}
    BUDGET_MS=''
    GUARDED_BUDGET_MS=''
else
    PAIRS=1
    MAX_RATIO=''
    GUARDED_MAX_RATIO=''
    BUDGET_MS=${3:-}
    GUARDED_BUDGET_MS=${4:-}
fi

# A pair whose two baseline readings disagree by more than this was taken
# while the machine was doing something else, and its ratio is noise. The
# default is measured need at a reference deployment, not a round number
# picked for looks: one pair in the first ratio run there read the SAME bare
# binary a fifth apart and produced a ratio several times the spread of every
# other pair put together.
DRIFT_MAX_PCT=${WALK_BLOCKER_MEASURE_DRIFT_PCT:-12}
WARMUP=${WALK_BLOCKER_MEASURE_WARMUP:-30}
# Below this many surviving pairs there is no median worth believing, so the
# run fails as unmeasurable rather than reporting a number it did not earn.
MIN_USABLE_PAIRS=3

# How far the two halves of a pair may disagree about how fast the MACHINE is,
# measured by each half's `python3 -S` floor. **Empty by default: the check
# reports and does not discard.**
#
# The reason it is off is the reason it exists. In a ratio run at a reference
# deployment, ranking the usable pairs by how much busier the machine was
# during the candidate half ordered their ratios almost exactly -- the ratio
# tracked the machine, not the shim. But any ceiling tight enough to catch
# that would have discarded half the run and dropped it below
# MIN_USABLE_PAIRS, failing a deploy gate that should have passed. Choosing a
# number from one run is a guess; printing the mismatch on every pair is what
# makes it choosable, from a record, later.
FLOOR_MAX_PCT=${WALK_BLOCKER_MEASURE_FLOOR_PCT:-}

# Filesystems where reading the guard is the expensive part: the network and
# parallel types the rule table treats as expensive by default, for the same
# reason. Generic vocabulary, not a site's mount census (ADR-0014).
#
# Measured at a reference deployment: the SAME guard.sh read roughly twice as
# slow per call out of a clone on the parallel filesystem as out of local
# disk. The slow reading failed a budget and said "the fast path grew".
# Nothing had grown. Every iteration re-read a script of a hundred-odd
# kilobytes over the filesystem this whole project exists because it is slow,
# and the benchmark faithfully reported it.
#
# A ratio run is not saved by symmetry either: a deploy compares the INSTALLED
# guard on local disk against a new one in the operator's clone, so a clone on
# the slow filesystem makes the candidate look slower by where it sits rather
# than by what it does.
#
# `-` and not `:-`: an EMPTY value is the documented way to disable the check,
# and `:-` would read empty as unset and quietly put the default list back.
# The predecessor had `:-` and its test never noticed, because the guard it
# timed sat on a filesystem that was not on the list either way.
SLOW_FSTYPES=${WALK_BLOCKER_MEASURE_SLOW_FSTYPES-wekafs nfs4 nfs lustre gpfs cifs beegfs ceph}
# Where that judgement reads the mount table from. A seam for the test suite,
# which drives the refusal with a fixture table rather than needing a slow
# mount to point at; nothing on a node has a reason to set it.
MOUNT_TABLE=${WALK_BLOCKER_MEASURE_MOUNTS:-/proc/mounts}

# A run that asks for fewer pairs than the gate needs is unmeasurable before it
# starts, and it has to say so before it starts: the post-measurement check
# further down blames the machine and tells the operator to re-run when it is
# quieter, which is true of a drift discard and false of an argument -- and a
# PAIRS=2 run reached that message only after paying for the whole benchmark.
# Exit 2, with the other usage errors rather than with the gate failures, so
# that "you asked for something unmeasurable" and "the shim got slower" are
# distinguishable by exit code and not only by prose. Guarded on $REF because
# single-guard mode sets PAIRS=1 and has no median to take.
# A count: a whole number, at least one.
#
# The shape check cannot be folded into an arithmetic comparison. `[ abc -lt
# 3 ]` does not evaluate false in dash -- it prints "Illegal number" and
# returns non-zero, so a guard written that way reads as satisfied, the loop's
# own test fails the same way and runs zero times, and a typo'd argument lands
# on a message that blames the machine and says to re-run when it is quieter.
# That is the confusion these checks exist to remove, arriving through a check.
require_count() {
    case "$2" in
        ''|*[!0-9]*)
            echo "measure.sh: $1 must be a whole number, not '$2'." >&2
            exit 2
            ;;
    esac
    # The floor differs by argument and both values are real: N=0 divides by
    # zero in the per-call arithmetic, while WARMUP=0 is a legitimate ask --
    # it is how a probe run skips the warmup entirely.
    _rc_min=${3:-1}
    if [ "$2" -lt "$_rc_min" ]; then
        echo "measure.sh: $1 must be at least $_rc_min, not '$2'." >&2
        exit 2
    fi
}

# A ceiling: a decimal, or empty. Empty is how every gate in this script says
# "not enabled", so it has to pass.
#
# This one matters more than it looks, and the reason it matters is not the
# obvious one.
#
# A bad count dies loudly in dash. A bad ceiling does not die at all: it
# reaches `awk -v b="$CEILING"` in `BEGIN{exit !(o > b)}`. The obvious
# reading is that awk coerces the garbage to 0 and the gate then fires on
# everything -- and `b+0` really is 0, which is what makes that reading
# feel settled. The COMPARISON does not go through arithmetic. `b` is not a
# numeric string, so `o > b` is a STRING comparison, and the answer turns on
# the first byte rather than on any value:
#
#   awk -v o=2.87 -v b=abc  'BEGIN{exit !(o > b)}'   ->  silent pass
#   awk -v o=2.87 -v b=3.o  'BEGIN{exit !(o > b)}'   ->  silent pass
#   awk -v o=2.87 -v b=nan  'BEGIN{exit !(o > b)}'   ->  silent pass
#   awk -v o=2.87 -v b=-1   'BEGIN{exit !(o > b)}'   ->  FIRES
#   awk -v o=2.87 -v b=1.5x 'BEGIN{exit !(o > b)}'   ->  FIRES
#   awk -v b=abc 'BEGIN{printf "%s %d", b+0, ("2.87" > b)}'   ->   0 0
#
# Measured on GNU awk, on a node and again on a workstation, so it is the
# deployed tool's behaviour and not an inference from POSIX prose.
#
# So the damage is the opposite of "fires on every run": for a letter-leading
# typo -- the common shape -- the gate silently PASSES, and a shim that really
# did regress reports "within budget" and exits 0. A gate nobody can see fail
# is worse than one that fails too often, because the second gets fixed.
require_threshold() {
    [ -n "$2" ] || return 0
    # One leading `-` is allowed, and deliberately: a negative threshold means
    # "discard everything" here, which is how the drift and floor gates are
    # driven in tests and how an operator forces a gate on. It fires always,
    # which is noisy rather than silent, and noisy gets fixed. A bare `-` is
    # still refused, because `${2#-}` leaves it empty.
    _rt=${2#-}
    case "$_rt" in
        ''|*[!0-9.]*|.|*.*.*)
            echo "measure.sh: $1 must be a number, not '$2'." >&2
            echo "  awk does not reject it: with this value the budget gate" >&2
            echo "  can pass silently, reporting a regressed shim as within" >&2
            echo "  budget. Refused here so it cannot." >&2
            exit 2
            ;;
    esac
}

require_count N "$N"
# The env knobs carry the identical hazard and were once missed when the
# positional ones were fixed -- FLOOR_MAX_PCT worst of all, because it was
# added in the same change that added require_threshold, and then not routed
# through it. A typo'd floor ceiling produced no refusal and no discard: the
# operator believes a gate is on and it is doing nothing.
require_count WARMUP "$WARMUP" 0
require_threshold DRIFT_MAX_PCT "$DRIFT_MAX_PCT"
require_threshold FLOOR_MAX_PCT "$FLOOR_MAX_PCT"
if [ -n "$REF" ]; then
    require_count PAIRS "$PAIRS"
    require_threshold MAX_RATIO "$MAX_RATIO"
    require_threshold GUARDED_MAX_RATIO "$GUARDED_MAX_RATIO"
    if [ "$PAIRS" -lt "$MIN_USABLE_PAIRS" ]; then
        echo "measure.sh: PAIRS=$PAIRS is below the $MIN_USABLE_PAIRS pairs this" >&2
        echo "  gate needs before it will take a median. Ask for more pairs -- the" >&2
        echo "  machine has nothing to do with this one." >&2
        exit 2
    fi
else
    require_threshold BUDGET_MS "$BUDGET_MS"
    require_threshold GUARDED_BUDGET_MS "$GUARDED_BUDGET_MS"
fi

tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/shim" "$tmp/bin"

abspath() {
    # A failed `cd` used to leave the first substitution empty, so this
    # returned `/<basename>` -- a plausible-looking absolute path that was
    # never on disk -- and let dash's own `cd:` complaint leak to stderr above
    # the script's diagnosis. Fall back to the caller's own string instead of
    # inventing one; `require_guard` has already refused a path that is
    # missing outright.
    _ap_dir=$(cd "$(dirname "$1")" 2>/dev/null && pwd)
    if [ -z "$_ap_dir" ]; then
        printf '%s' "$1"
        return 0
    fi
    printf '%s/%s' "$_ap_dir" "$(basename "$1")"
}

# A guard that is not there at all. Kept distinct from the reached-the-binary
# failure further down, which names the exec bit: `git show` produces a file
# with the wrong MODE, and a typo produces no file, and telling someone to
# chmod a path that does not exist sends them after the wrong thing.
require_guard() {
    [ -e "$2" ] && return 0
    echo "measure.sh: the $1 guard does not exist:" >&2
    echo "  $2" >&2
    echo "  Nothing to chmod -- there is no file at that path. A guard" >&2
    echo "  extracted with \`git show\` needs both: the file written, and" >&2
    echo "  \`chmod +x\`, because git does not preserve the mode." >&2
    exit 2
}

# The filesystem a path sits on: longest-prefix match against the mount
# table. Longest rather than first, because `/` matches everything and would
# otherwise shadow every real mount under it.
fstype_of() {
    _fp=$1
    _best=''
    _besttype=''
    while read -r _fdev _fmnt _ftype _frest; do
        _hit=0
        if [ "$_fmnt" = / ]; then
            _hit=1
        elif [ "$_fp" = "$_fmnt" ]; then
            _hit=1
        else
            case $_fp in "$_fmnt"/*) _hit=1 ;; esac
        fi
        if [ "$_hit" -eq 1 ] && [ "${#_fmnt}" -ge "${#_best}" ]; then
            _best=$_fmnt
            _besttype=$_ftype
        fi
    done < "$MOUNT_TABLE"
    printf '%s' "$_besttype"
}

# Refuse to time a guard that lives on a filesystem whose reads are the thing
# being measured. Refusing rather than warning: a warning above a table of
# numbers is read as a caveat on numbers that are still believed, and these
# numbers are not worth believing at all. WALK_BLOCKER_MEASURE_SLOW_FSTYPES=''
# disables it for a machine where every filesystem is one of these.
check_fs() {
    [ -n "$SLOW_FSTYPES" ] || return 0
    _cf_type=$(fstype_of "$1")
    for _cf_slow in $SLOW_FSTYPES; do
        [ "$_cf_type" = "$_cf_slow" ] || continue
        echo "measure.sh: $1" >&2
        echo "  is on $_cf_type, and reading it is the expensive operation this" >&2
        echo "  benchmark exists to keep OFF the measurement. Every iteration" >&2
        echo "  re-reads the guard, so a guard on a network or parallel" >&2
        echo "  filesystem reports that filesystem's read cost as the shim's." >&2
        echo "" >&2
        echo "  Copy it to local disk and time it there:" >&2
        echo "    D=\$(mktemp -d /var/tmp/measure-XXXXXX)" >&2
        echo "    cp <guard.sh> \"\$D\"/ && chmod +x \"\$D\"/guard.sh" >&2
        echo "" >&2
        echo "  (WALK_BLOCKER_MEASURE_SLOW_FSTYPES='' if every filesystem here is one.)" >&2
        exit 2
    done
    return 0
}

# Operands for the guarded bench. Cheap by construction -- under $TMPDIR, so
# on a node local disk or tmpfs and never one of the expensive types --
# because the case being timed is the one the guard ALLOWS. A refused call
# returns early and would measure the cheaper half.
OPERANDS=''
_o=1
while [ "$_o" -le 10 ]; do
    mkdir -p "$tmp/op$_o"
    OPERANDS="$OPERANDS $tmp/op$_o"
    _o=$((_o + 1))
done

cat > "$tmp/py_shim.py" <<'PY'
import os, sys
sys.exit(0)
PY

# Set by bench() so the caller can compare readings. `sh` has no arrays and
# no way to return a float, and this file is POSIX sh because it runs on the
# node -- so a global it is, documented rather than surprising.
BENCH_MS=""

bench() {
    _label=$1; shift
    # Warm up first. Without this the first benchmark in the list pays for cold
    # page cache and reads several times slower than the ones after it --
    # which on the first run of this script made the shim look FASTER than the
    # bare binary it wraps.
    #
    # The default is the measured need and is what every real reading uses.
    # The env var exists so tests/test_measure.py can drive the GATES -- the
    # drift discard, the ratio ceiling, the reached-the-binary probe -- in a
    # second instead of half a minute. Lowering it makes the timings
    # meaningless, which is exactly why no test asserts on a timing.
    _w=0
    while [ "$_w" -lt "$WARMUP" ]; do
        "$@" >/dev/null 2>&1
        _w=$((_w + 1))
    done
    _start=$(date +%s%N)
    _i=0
    while [ "$_i" -lt "$N" ]; do
        "$@" >/dev/null 2>&1
        _i=$((_i + 1))
    done
    _end=$(date +%s%N)
    BENCH_MS=$(awk -v a="$_start" -v b="$_end" -v n="$N" \
        'BEGIN{printf "%.2f", (b-a)/n/1000000}')
    printf '%-28s %8.2f ms/call\n' "$_label" "$BENCH_MS"
}

# One complete reading of one guard.sh. Sets the M_* globals; prints the table.
# Factored out of the script body so --against can call it twice per pair --
# the alternative, a second script that parses this one's stdout, would put the
# knowledge of which number matters in two places.
M_FAST=''        # fast-path overhead over the bare binary, ms
M_GUARDED=''     # guarded-path overhead, ms
M_SLOPE=''       # ms per additional operand
M_DRIFT=''       # % disagreement between the two baseline readings
M_USABLE=''      # 1 if the drift check passed, 0 if not
M_FLOOR=''       # `python3 -S` startup, ms -- this half's machine speed
M_POSITIVE=''    # 1 if both overheads came back above zero -- see below

measure_guard() {
    _guard=$(abspath "$1")
    ln -sf "$_guard" "$tmp/shim/grep"
    ln -sf "$_guard" "$tmp/shim/du"

    # Prove the shim actually runs and reaches the binary behind it before
    # timing anything. The first version of this script did not, and happily
    # reported the shim as several times FASTER than the bare binary it wraps
    # -- it was timing a "Permission denied" from a copy that had lost its
    # exec bit. A guard.sh extracted with `git show` lands mode 0644 and trips
    # this, which is the intended outcome and not a measurement bug.
    #
    # WALK_BLOCKER_SHIM_DIR is the shim's own seam for "skip this directory
    # when resolving the real binary"; set to the directory the symlink lives
    # in, it changes nothing the shim would audit.
    printf '#!/bin/sh\nprintf "REACHED\\n"\nexit 0\n' > "$tmp/bin/grep"
    chmod +x "$tmp/bin/grep"
    _probe=$(WALK_BLOCKER_SHIM_DIR="$tmp/shim" PATH="$tmp/shim:$tmp/bin:$PATH" \
            "$tmp/shim/grep" pattern 2>&1)
    _probe_rc=$?
    if [ "$_probe_rc" -ne 0 ] || [ "$_probe" != "REACHED" ]; then
        echo "measure.sh: the shim did not reach the binary behind it." >&2
        echo "  guard=$_guard exit=$_probe_rc output=[$_probe]" >&2
        echo "  (is it executable? neither scp nor git show preserves the mode)" >&2
        exit 1
    fi
    printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/grep"
    chmod +x "$tmp/bin/grep"

    # The same proof for the guarded path, and it needs its own: `du` reaching
    # its stub means the shim ALLOWED the call and exec'd. A refusal exits 77
    # without ever reaching it, and timing that would report the early-return
    # path while claiming to report the expensive one.
    printf '#!/bin/sh\nprintf "REACHED\\n"\nexit 0\n' > "$tmp/bin/du"
    chmod +x "$tmp/bin/du"
    _probe=$(WALK_BLOCKER_SHIM_DIR="$tmp/shim" PATH="$tmp/shim:$tmp/bin:$PATH" \
            "$tmp/shim/du" -sh "$tmp/op1" 2>&1)
    _probe_rc=$?
    if [ "$_probe_rc" -ne 0 ] || [ "$_probe" != "REACHED" ]; then
        echo "measure.sh: the guarded path did not reach the binary behind it." >&2
        echo "  guard=$_guard exit=$_probe_rc output=[$_probe]" >&2
        echo "  (a refusal here means the operand was judged expensive, so the" >&2
        echo "   guarded reading would be the early-return path, not the full one)" >&2
        exit 1
    fi
    printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/du"
    chmod +x "$tmp/bin/du"

    echo "n=$N, fast path (non-recursive grep), $_guard"
    PATH="$tmp/bin:$PATH" bench "real grep (baseline)" "$tmp/bin/grep" pattern
    _baseline=$BENCH_MS
    WALK_BLOCKER_SHIM_DIR="$tmp/shim" PATH="$tmp/shim:$tmp/bin:$PATH" \
        bench "sh shim -> exec grep" "$tmp/shim/grep" pattern
    _shim=$BENCH_MS
    bench "python3 -S (floor)" python3 -S "$tmp/py_shim.py"
    # Captured, not just printed. It measures no part of the shim -- it is
    # this half's reading of how fast the MACHINE is right now, and in a
    # ratio the two halves have to be comparable for the ratio to mean
    # anything. See the per-pair mismatch line further down.
    M_FLOOR=$BENCH_MS
    bench "python3 (floor)" python3 "$tmp/py_shim.py"
    # Baseline again, last: if it has drifted from the first reading, the
    # machine was busy and none of the numbers above are worth recording.
    PATH="$tmp/bin:$PATH" bench "real grep (baseline, again)" "$tmp/bin/grep" pattern
    _baseline2=$BENCH_MS

    M_FAST=$(awk -v s="$_shim" -v b="$_baseline" 'BEGIN{printf "%.2f", s-b}')
    M_DRIFT=$(awk -v a="$_baseline" -v b="$_baseline2" \
        'BEGIN{d=a-b; if(d<0)d=-d; printf "%.1f", (a>0 ? 100*d/a : 999)}')
    M_USABLE=$(awk -v d="$M_DRIFT" -v m="$DRIFT_MAX_PCT" 'BEGIN{print (d>m)?0:1}')
    printf '%-28s %8.2f ms/call\n' "shim overhead" "$M_FAST"
    printf '%-28s %8.1f %%\n' "baseline drift" "$M_DRIFT"

    # --- the guarded path ---------------------------------------------------
    # The shim reads its compiled mount table -- the node's real /proc/mounts
    # in a deployed guard -- and that table's size is a large part of what
    # this reading measures: a node running container runtimes carries
    # hundreds of overlay and squashfs rows, and every guarded call scans them.
    echo
    echo "n=$N, guarded path (allowed traversing du, the shim's own mount table)"
    PATH="$tmp/bin:$PATH" bench "real du (baseline)" "$tmp/bin/du" -sh "$tmp/op1"
    _g_baseline=$BENCH_MS
    WALK_BLOCKER_SHIM_DIR="$tmp/shim" PATH="$tmp/shim:$tmp/bin:$PATH" \
        bench "shim -> exec du, 1 operand" "$tmp/shim/du" -sh "$tmp/op1"
    _g_one=$BENCH_MS
    # shellcheck disable=SC2086  # deliberate word split: one operand per word
    WALK_BLOCKER_SHIM_DIR="$tmp/shim" PATH="$tmp/shim:$tmp/bin:$PATH" \
        bench "shim -> exec du, 10 operands" "$tmp/shim/du" -sh $OPERANDS
    _g_ten=$BENCH_MS

    M_GUARDED=$(awk -v s="$_g_one" -v b="$_g_baseline" 'BEGIN{printf "%.2f", s-b}')
    M_SLOPE=$(awk -v a="$_g_one" -v b="$_g_ten" 'BEGIN{printf "%.2f", (b-a)/9}')

    # An overhead at or below zero is not a fast shim, it is a reading that did
    # not happen -- and the ratio line downstream treats it as an ordinary
    # number: `(r > 0 ? c/r : 0)` yields 0.000, which clears any ceiling. A
    # gate that reports a pass for a run that measured nothing is worse than
    # no gate.
    #
    # The drift check does not cover this. It compares a guard's own two
    # baseline readings and says nothing about the sign of the difference
    # between the shim and the baseline. Measured, on a clock that advances by
    # a CONSTANT increment per call -- every reading identical, drift 0.0 %,
    # every pair "usable", every overhead exactly 0.00: the run reported
    # `3/3 usable pairs`, a median of 0.000x, "within the ceiling", and
    # exited 0. Noise at low N putting one reading under its own baseline
    # reaches the same line, though nothing has observed that happening.
    #
    # Separate from the drift flag on purpose: they refuse different things,
    # and the messages have to say which. Both modes consult it -- single-guard
    # mode had its own degenerate-clock pass, fixed by reading this flag there
    # too, BEFORE the drift check, because a clock that returns the same value
    # twice makes drift compute as the 999 sentinel and would otherwise refuse
    # with "the two readings disagree by 999 %" when they agree exactly.
    M_POSITIVE=$(awk -v f="$M_FAST" -v g="$M_GUARDED" \
        'BEGIN{print (f > 0 && g > 0) ? 1 : 0}')
    printf '%-28s %8.2f ms/call\n' "guarded overhead" "$M_GUARDED"
    printf '%-28s %8.2f ms/operand\n' "per additional operand" "$M_SLOPE"
}

median() {
    # shellcheck disable=SC2086  # deliberate word split: one reading per word
    printf '%s\n' $1 | sort -n | awk '
        {v[NR] = $1}
        END {
            if (NR == 0) { print "" ; exit }
            m = int((NR + 1) / 2)
            printf "%.3f", (NR % 2) ? v[m] : (v[m] + v[m + 1]) / 2
        }'
}

# Before anything is timed. A refusal after the table has printed is a
# refusal nobody reads.
require_guard candidate "$GUARD"
[ -z "$REF" ] || require_guard reference "$REF"
check_fs "$(abspath "$GUARD")"
[ -z "$REF" ] || check_fs "$(abspath "$REF")"

# --- single-guard mode ------------------------------------------------------

if [ -z "$REF" ]; then
    measure_guard "$GUARD"

    # Drift first, and it is a REFUSAL rather than a budget verdict. Ratio mode
    # has discarded drifted pairs since it was written; single-guard mode did
    # not, and that once let a run whose two baselines disagreed by a fifth
    # report "shim overhead exceeds the budget" -- a definite-sounding verdict
    # about the code, from a reading whose own two measurements of the SAME
    # bare binary did not agree. Its message did say "or this machine is too
    # busy", which is prose where an exit code was needed.
    #
    # Exit 2, with the usage errors rather than the gate failures: "this run
    # measured nothing" is not "the shim got slower", and CI cannot read the
    # difference out of a sentence.
    #
    # This was argued down once, on the grounds that the tests run at an N
    # where drift routinely exceeds the default and gating on it would make
    # them flaky. That reasoning was right about the tests and wrong about the
    # node: the tests set WALK_BLOCKER_MEASURE_DRIFT_PCT explicitly, which is
    # what the variable is for, and the node got a false verdict in the
    # meantime. The positivity check has to come first: see the M_POSITIVE
    # comment above.
    if [ "$M_POSITIVE" -eq 0 ]; then
        echo "measure.sh: an overhead came back at or below zero" >&2
        echo "  (fast ${M_FAST} ms, guarded ${M_GUARDED} ms). Nothing was" >&2
        echo "  measured, so there is nothing to compare to a budget -- check" >&2
        echo "  that \`date +%s%N\` on this machine returns nanoseconds." >&2
        exit 2
    fi
    if [ "$M_USABLE" -eq 0 ]; then
        echo "measure.sh: the two readings of the same bare binary disagree" >&2
        echo "  by ${M_DRIFT} %, over the ${DRIFT_MAX_PCT} % this gate allows." >&2
        echo "  Nothing here is a statement about the shim -- re-run when the" >&2
        echo "  machine is quieter, or use --against, which compares two shims" >&2
        echo "  in the same minute and discards a pair that reads like this." >&2
        exit 2
    fi

    if [ -n "$BUDGET_MS" ]; then
        # The gate. Compared on the OVERHEAD rather than the absolute reading,
        # because the absolute number moves with the machine while the overhead
        # is the thing this design claims is small.
        if awk -v o="$M_FAST" -v b="$BUDGET_MS" 'BEGIN{exit !(o > b)}'; then
            echo "measure.sh: shim overhead ${M_FAST} ms exceeds the" >&2
            echo "  ${BUDGET_MS} ms budget. Either the fast path grew or this" >&2
            echo "  machine is too busy to measure on -- the baseline drift" >&2
            echo "  above is ${M_DRIFT} %, and --against is the reading that" >&2
            echo "  tells those two apart." >&2
            exit 1
        fi
        echo "within the ${BUDGET_MS} ms budget"
    fi

    if [ -n "$GUARDED_BUDGET_MS" ]; then
        if awk -v o="$M_GUARDED" -v b="$GUARDED_BUDGET_MS" 'BEGIN{exit !(o > b)}'; then
            echo "measure.sh: guarded overhead ${M_GUARDED} ms exceeds the" >&2
            echo "  ${GUARDED_BUDGET_MS} ms budget. This is the path every find," >&2
            echo "  du, rg, fd and tree call takes -- they never reach the fast" >&2
            echo "  one -- so it is the number that reaches most users." >&2
            exit 1
        fi
        echo "guarded path within the ${GUARDED_BUDGET_MS} ms budget"
    fi
    exit 0
fi

# --- ratio mode -------------------------------------------------------------
# Alternating, and the ORDER alternates too: pair 1 runs ref then candidate,
# pair 2 candidate then ref. A node whose load is climbing through the run
# would otherwise charge the whole trend to whichever guard always went second.

FAST_RATIOS=''
GUARDED_RATIOS=''
USABLE=0
_p=1
while [ "$_p" -le "$PAIRS" ]; do
    echo "=== pair $_p/$PAIRS ==="
    if [ $((_p % 2)) -eq 1 ]; then _first=$REF; _second=$GUARD
    else _first=$GUARD; _second=$REF; fi

    measure_guard "$_first"
    _f_fast=$M_FAST; _f_guarded=$M_GUARDED; _f_usable=$M_USABLE
    _f_positive=$M_POSITIVE; _f_floor=$M_FLOOR
    echo
    measure_guard "$_second"
    _s_fast=$M_FAST; _s_guarded=$M_GUARDED; _s_usable=$M_USABLE
    _s_positive=$M_POSITIVE; _s_floor=$M_FLOOR
    echo

    if [ $((_p % 2)) -eq 1 ]; then
        _r_fast=$_f_fast; _r_guarded=$_f_guarded; _r_floor=$_f_floor
        _c_fast=$_s_fast; _c_guarded=$_s_guarded; _c_floor=$_s_floor
    else
        _r_fast=$_s_fast; _r_guarded=$_s_guarded; _r_floor=$_s_floor
        _c_fast=$_f_fast; _c_guarded=$_f_guarded; _c_floor=$_f_floor
    fi
    # How far apart the two halves were about the machine, as a percentage of
    # the reference half. Reported on every pair; only discards when
    # FLOOR_MAX_PCT is set.
    _pr_floor=$(awk -v c="$_c_floor" -v r="$_r_floor" \
        'BEGIN{d=c-r; if(d<0)d=-d; printf "%.1f", (r > 0 ? 100*d/r : 999)}')

    if [ "$_f_positive" -eq 0 ] || [ "$_s_positive" -eq 0 ]; then
        echo "pair $_p DISCARDED: an overhead at or below zero" \
             "($_r_fast/$_r_guarded against $_c_fast/$_c_guarded ms) is a" \
             "clock that did not measure, not a shim that is free"
        _p=$((_p + 1))
        continue
    fi
    if [ "$_f_usable" -eq 0 ] || [ "$_s_usable" -eq 0 ]; then
        echo "pair $_p DISCARDED: baseline drift over ${DRIFT_MAX_PCT} %"
        _p=$((_p + 1))
        continue
    fi
    if [ -n "$FLOOR_MAX_PCT" ] &&
            awk -v d="$_pr_floor" -v m="$FLOOR_MAX_PCT" 'BEGIN{exit !(d > m)}'
    then
        echo "pair $_p DISCARDED: the two halves disagree by ${_pr_floor} %" \
             "about how fast the machine is (floors ${_r_floor} vs" \
             "${_c_floor} ms), over the ${FLOOR_MAX_PCT} % allowed -- a ratio" \
             "between them would be measuring the machine"
        _p=$((_p + 1))
        continue
    fi
    _pr_fast=$(awk -v c="$_c_fast" -v r="$_r_fast" \
        'BEGIN{printf "%.3f", (r > 0 ? c/r : 0)}')
    _pr_guarded=$(awk -v c="$_c_guarded" -v r="$_r_guarded" \
        'BEGIN{printf "%.3f", (r > 0 ? c/r : 0)}')
    FAST_RATIOS="$FAST_RATIOS $_pr_fast"
    GUARDED_RATIOS="$GUARDED_RATIOS $_pr_guarded"
    USABLE=$((USABLE + 1))
    printf 'pair %d ratio: fast %sx (%s -> %s ms), guarded %sx (%s -> %s ms)' \
        "$_p" "$_pr_fast" "$_r_fast" "$_c_fast" "$_pr_guarded" "$_r_guarded" "$_c_guarded"
    printf ', halves %s%% apart on the machine\n' "$_pr_floor"
    _p=$((_p + 1))
done

echo
echo "=== $USABLE/$PAIRS usable pairs ==="
if [ "$USABLE" -lt "$MIN_USABLE_PAIRS" ]; then
    echo "measure.sh: only $USABLE pairs survived, below the" >&2
    echo "  $MIN_USABLE_PAIRS this gate needs. Each DISCARDED line above says" >&2
    echo "  which check refused it. Baseline drift over ${DRIFT_MAX_PCT} % is a" >&2
    echo "  statement about the machine -- re-run when it is quieter. An" >&2
    echo "  overhead at or below zero is a statement about the clock, and" >&2
    echo "  re-running will not fix it." >&2
    exit 1
fi

MED_FAST=$(median "$FAST_RATIOS")
MED_GUARDED=$(median "$GUARDED_RATIOS")
printf '%-28s %8s x\n' "fast-path ratio (median)" "$MED_FAST"
printf '%-28s %8s x\n' "guarded ratio (median)" "$MED_GUARDED"
printf 'per-pair fast ratios:%s\n' "$FAST_RATIOS"
# The spread, beside the median. A median of readings that ranged across a
# fifth of their value is not a three-decimal answer, and printing only the
# median invites it to be quoted as one.
# shellcheck disable=SC2086  # deliberate word split: one reading per word
printf '%-28s %8s\n' "fast-path ratio (spread)" \
    "$(printf '%s\n' $FAST_RATIOS | sort -n | awk 'NR==1{lo=$1} {hi=$1}
        END{if (NR) printf "%.3f-%.3f", lo, hi}')"

_rc=0
if [ -n "$MAX_RATIO" ]; then
    if awk -v o="$MED_FAST" -v b="$MAX_RATIO" 'BEGIN{exit !(o > b)}'; then
        echo "measure.sh: fast-path ratio ${MED_FAST}x exceeds the" >&2
        echo "  ${MAX_RATIO}x ceiling against $REF." >&2
        _rc=1
    else
        echo "fast path within the ${MAX_RATIO}x ceiling"
    fi
fi
if [ -n "$GUARDED_MAX_RATIO" ]; then
    if awk -v o="$MED_GUARDED" -v b="$GUARDED_MAX_RATIO" 'BEGIN{exit !(o > b)}'; then
        echo "measure.sh: guarded ratio ${MED_GUARDED}x exceeds the" >&2
        echo "  ${GUARDED_MAX_RATIO}x ceiling against $REF." >&2
        _rc=1
    else
        echo "guarded path within the ${GUARDED_MAX_RATIO}x ceiling"
    fi
fi
exit "$_rc"
