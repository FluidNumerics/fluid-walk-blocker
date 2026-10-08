# The IP-hygiene gate

`CLAUDE.md` says no site literal may appear in this tree and `ADR-0014` says
why. `tools/check_no_site_literals.py` is the backstop. It scans two lists.

**Structural patterns** are committed, in `tools/forbidden-patterns.txt`:
the shapes a site fact takes (an IPv4 literal, a five-digit uid, a login-node
hostname shape, a capacity in PB, a comma-grouped count, a
"measured on 20XX-XX-XX" phrase). They can be waived on one line with
`# site-literal-ok: <reason of eight or more characters>`, or exempted for a
path with an `allow` line in the pattern file.

**Customer terms** are never committed. A denylist of a customer's hostnames
in the generic tree would itself be the leak. The list lives at
`~/.config/walk-blocker/forbidden-terms.txt` (mode 600) on the maintainer's
workstation and as the GitHub Actions secret `FORBIDDEN_TERMS`. One regex per
line, case-insensitive; `tools/forbidden-terms.example.txt` shows the format
with made-up terms. A customer hit is reported as `path:line: customer-term`
and nothing else. The scanner never prints the term, the match or the line,
and a malformed regex is reported by line number only.

## Running it

```sh
python3 tools/check_no_site_literals.py                       # tracked files, structural + local terms if present
python3 tools/check_no_site_literals.py --require-terms --quiet   # what CI runs: exit 3 if no term list
python3 tools/check_no_site_literals.py --root path/to/payload .  # a built payload directory
git config core.hooksPath tools/hooks                         # pre-commit early warning, once per clone
```

Without `--root` it scans the repository that holds the script, whatever the
current directory: to check a worktree, run that worktree's copy of the script.
Paths named on the command line are the exception: without `--root` they are
read relative to the current directory, as the shell named them.

Exit codes: 0 clean, 1 findings, 2 configuration or usage error, 3
`--require-terms` given and no term list found. The stderr summary always
names the root it scanned, counts the files it actually read, and says
`terms=on` or `terms=off`, so a pass on the wrong tree and a structural-only
pass are both visible as such.

Three cases that used to pass are exit 2, because none of them scans what the
caller meant:

- a path named on the command line or in `--files-from` that does not exist
  (a typo is not a clean file);
- a scan that finds no file to read: a whole-tree scan of a root that tracks
  nothing, such as a copy of `tools/` inside another repository, or named
  directories that hold no files. An empty `--files-from` list is the
  exception and still passes, since a commit that only deletes stages no names
  for the pre-commit hook;
- `--files-from` together with positional paths. Put every name in the list,
  or name them all as arguments.

The pre-commit hook reads the working-tree copy of each staged name, so a file
staged and then deleted from the working tree before the commit now stops the
hook with exit 2 rather than passing unread.

## Rotating the secret

```sh
gh secret set FORBIDDEN_TERMS --repo FluidNumerics/fluid-walk-blocker < ~/.config/walk-blocker/forbidden-terms.txt
```

## Where each half runs

The gate is two CI jobs (ADR-0022), split along the line between what is
committed and what is secret.

`ip-hygiene-structural` needs no secret and runs everywhere, including in a
fork, where it is the whole of the gate that can run.

`ip-hygiene-terms` runs only in this repository. A fork's own CI skips it
quietly: a fork holds no customer list, has nothing of this customer's to
protect, and cannot be given the secret by any mechanism GitHub offers. A pull
request into this repository *from* a fork receives no secrets either, and
there the job fails and says so rather than skipping — a job skipped by a
job-level `if` counts as a *passing* required check, so skipping would be a
green merge button over a scan that never ran. A maintainer re-runs the scan
from a trusted ref before such a pull request merges, by pushing its branch to
this repository.

## What it cannot do

It matches shapes and listed terms. A site fact that has neither shape nor a
listed term passes. Review still reads the diff; the gate exists so that the
known classes cannot slip past unread.
