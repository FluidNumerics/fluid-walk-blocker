# Contributing

Thanks for looking. This is a small, opinionated tree with a few rules that are
not obvious from the code, so this file is short and worth reading first.

## Read these, in this order

1. `README.md` — what it is, and what is deliberately out of scope
2. `docs/plan.md` — the architecture on one page
3. `docs/adr/0001` … `0024`, in order — the decisions and why alternatives lost
4. `CLAUDE.md` — the non-negotiables and the parts that are easy to get wrong

A change that contradicts an ADR is not refused, but it needs a new ADR that
names the clause it narrows, per `docs/adr/TEMPLATE.md`. A new record never
edits an old one's Decision; it narrows it, and the old record gets a line
under its Status saying so. `tests/test_adr_conventions.py` checks that the
quoted clause still resolves.

## Never contribute a site fact

This tree is generic on purpose. No hostnames, cluster or node names,
usernames, uids, account, partition or QoS names, measured figures from a real
deployment, vendor build strings as observed somewhere, dates of on-node
measurements, or paths that exist only at one site — in code, tests, fixtures,
docs, comments, or commit and pull-request text. ADR-0014 says why, and
`docs/evidence.md` says where the evidence lives instead.

Filesystem type names (`wekafs`, `nfs4`, `lustre`, `gpfs`), tool names and
versions measured on the tool itself, and the fictional values in
`examples/site.example.toml` are all fine.

### How CI enforces it, and what you will see on a fork

`tools/check_no_site_literals.py` runs as two jobs (ADR-0022,
`tools/README-ip-gate.md`):

- **`ip-hygiene-structural`** scans committed structural patterns. It needs no
  secret and runs on your pull request like any other check. If it fires,
  either reword, or add a one-line `# site-literal-ok: <reason>` waiver for a
  structural false positive.
- **`ip-hygiene-terms`** scans a customer term list that is deliberately not in
  this repository. GitHub withholds secrets from fork runs, so **this job will
  fail on a pull request from a fork, and that is expected.** It is not
  something you can fix, and it is not a reason to change your patch. A
  maintainer re-runs that scan from a trusted ref before merging.

## Code constraints

**Node code is stdlib-only Python 3.9 or POSIX `sh`.** Everything under `node/`
and `deploy.py` runs on a login node whose interpreter we do not choose and
which has no `uv`. No third-party imports, no `match` statements, no PEP 723
headers there. Shell must be dash-clean.

Everywhere else — the build tool, `src/`, `tests/` — targets Python 3.9+ across
the CI matrix, and standalone `uv run` scripts with PEP 723 headers are fine.

**`examples/payload/` is a build product**, not source. Edit the templates, the
rule table or `site.toml` and rebuild; a hand-edit is overwritten and CI catches
the drift. The shim and the reaper are generated from one rule table so that
they cannot disagree.

## Before you push

```sh
uv run --group dev pytest tests/ -q
uv run walk-blocker build --site examples/site.example.toml --out examples/payload --check
shellcheck --shell=sh --severity=warning examples/payload/shim/*.sh examples/payload/walk-job
uvx --from pymarkdownlnt==0.9.39 pymarkdown --config .pymarkdown scan README.md CLAUDE.md docs/*.md docs/adr/*.md
uvx yamllint==1.38.0 -c .yamllint .github/workflows/ci.yml
python3 tools/check_no_site_literals.py --quiet
```

A `git config core.hooksPath tools/hooks` once per clone gives you the
site-literal check as a pre-commit warning, which is cheaper than finding out
in review.

## Never run the installer as root from a development session

`deploy.py --system` installs for real. What is safe to run while working on
the tree is `deploy.py --system --dry-run` and `deploy.py --verify`; both write
nothing, and the dry run names the checks it could not make rather than
reporting them clean. See ADR-0021.

## Security

Do not open a public issue for a suspected vulnerability. `SECURITY.md` has the
private route, and the two design facts worth reading before filing.
