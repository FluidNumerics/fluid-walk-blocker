#!/bin/sh
# Which of a tool's short flags may cluster with a DIGIT, measured by running it.
#
# This exists because `depth_digits` in src/walk_blocker/search_rules.py is a
# character set rather than a bool, and a wrong character in it is a silent
# bypass in one direction or a false refusal in the other. The set must come
# from the tool.
#
#   sh shim/measure-flags.sh 'ugrep' [LETTERS]
#   sh shim/measure-flags.sh 'exec -a ugrep /path/to/bundle-that-embeds-it'
#
# The first argument is a COMMAND, not just a name, because the build that
# matters on a node is not always on $PATH: a tool embedded inside some other
# program's binary is reached through `exec -a NAME /path/to/that/program`,
# and the version it embeds is the one whose flags need measuring.
#
# Method. Build a tree five levels deep with one matching file per level, then
# for each letter X compare `TOOL -lX3 NEEDLE .` against the two references:
#
#   TOOL -l  NEEDLE .   1 hit   -- the tool's own default for a directory operand
#   TOOL -l3 NEEDLE .   3 hits  -- the digit read as a depth
#
# A letter that yields the depth-3 count did NOT eat the digit and may be in
# the charset. Anything else did eat it -- either a required-value flag, which
# value_flags already handles, or an OPTIONAL-argument one like ugrep's
# -K/-m/-Z/-Q, which cannot be listed there at all (grep's --color rule) and
# is exactly what the charset is for.
#
# stdout and stderr are counted SEPARATELY, and both halves earn their place.
# Merging them made this report two wrong answers on ugrep: `-f3` ate the 3 as
# a filename and printed a three-line error, which coincided exactly with the
# depth-3 hit count and read as "clusterable" for a required-value flag; and
# -L/-q/-v/-V write no file list at all, so their zero coincided with nothing
# and had to be told apart from a real count rather than guessed at.
#
# So: anything on stderr means the flag CONSUMED the digit and the tool
# complained. A stdout count matching neither reference is INCONCLUSIVE --
# the letter changes what is printed for reasons unrelated to the digit.
# Resolve those against the tool's --help, and leave them OUT if in doubt: a
# missing character costs a bound, an extra one invents one.

set -u
TOOL=${1:?usage: measure-flags.sh TOOL-COMMAND [LETTERS]}
LETTERS=${2:-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPRSTUVWXYZ}

tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/a/b/c/d"
for d in . a a/b a/b/c a/b/c/d; do
    printf 'NEEDLE\n' > "$tmp/$d/f.txt"
done

build_cmd() {
    # The tool is a command STRING on purpose (see the header: the build that
    # matters may be reached through `exec -a`), so eval is the point rather
    # than an oversight -- and the arguments are appended to it rather than
    # passed around it, because `eval "$TOOL" "$@"` would re-split them.
    mf_cmd=$TOOL
    for mf_arg in "$@"; do
        mf_cmd="$mf_cmd '$mf_arg'"
    done
}

out_lines() {
    build_cmd "$@"
    ( cd "$tmp" && eval "$mf_cmd" 2>/dev/null ) | wc -l | tr -d " "
}

err_lines() {
    build_cmd "$@"
    ( cd "$tmp" && eval "$mf_cmd" 2>&1 1>/dev/null ) | wc -l | tr -d " "
}

base=$(out_lines -l NEEDLE .)
deep=$(out_lines -l3 NEEDLE .)
printf 'default depth: %s hit(s)   depth 3: %s hit(s)\n\n' "$base" "$deep"
if [ "$base" = "$deep" ]; then
    printf 'REFUSING to report: the two references are identical, so nothing\n'
    printf 'here can tell a depth from an eaten digit. Check the tool command.\n'
    exit 1
fi

clusterable=''
for letter in $(printf '%s' "$LETTERS" | sed 's/./& /g'); do
    got=$(out_lines "-l${letter}3" NEEDLE .)
    err=$(err_lines "-l${letter}3" NEEDLE .)
    if [ "$err" != "0" ]; then
        verdict='flag ERRORED on the digit -- keep out of the charset'
    elif [ "$got" = "$deep" ]; then
        clusterable="$clusterable$letter"
        verdict='digit is a DEPTH -- may be in the charset'
    elif [ "$got" = "$base" ]; then
        verdict='digit was EATEN by the flag -- keep out of the charset'
    else
        verdict="INCONCLUSIVE ($got hits); resolve against --help"
    fi
    printf '  -%s3  out=%-4s err=%-4s %s\n' "$letter" "$got" "$err" "$verdict"
done

printf '\nclusterable letters: %s\n' "$clusterable"
printf 'charset = those, plus the digits 0-9 and the range separators - and ,\n'
