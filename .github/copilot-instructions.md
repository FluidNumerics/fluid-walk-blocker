# Reviewing this repository

`walk-blocker` is two guardrails for a shared HPC login node, built from a
site's `site.toml`: an advisory PATH shim (Layer 1, POSIX `sh`) that refuses
unscoped filesystem searches and names `walk-job` as the alternative, and a
root-run `systemd` timer (Layer 2, Python) that finds the ones that got past
it. Read `README.md` and the ADR index under `docs/adr/` before reviewing.

Everything under `node/` runs on the node: stdlib-only Python 3.9 or POSIX
`sh`. No third-party imports, no `uv`, no bash-isms, and no configuration
file — every site value is compiled in by `walk-blocker build` (ADR-0013). The
workstation tool under `src/walk_blocker/` runs under `uv` and may have
dependencies.

## Ask these first

A diff prompts you to check whether the new lines are correct. These questions
it does not prompt, and they have produced the highest-value findings here and
in the predecessor:

| Question | Why it matters |
|---|---|
| When this code's own parsing or validation **fails**, does the failure become visible, or does the subject silently drop out? | Failing open is the one answer a guard must never give. A `try/except` returning an empty result told callers "clean". |
| Does a **claim in prose** still hold after this change — a budget in `README.md`, a count, an ADR statement? Is anything enforcing it? | A performance claim with no budget is a comment. A documented property with no test goes stale silently. |
| Does a **stated design property** have a test, or only prose? | "The fast path forks nothing" is the entire argument for `sh` over Python, and in the predecessor it had no assertion for a long stretch of its review history. |
| Which of {the ancestors, the final component's type, its **ownership**, the contents} does this check cover — and which does it not? | Repeated findings in the predecessor were one of these four missing. A root-owned `0755` directory can still hold a user-owned file. |
| Is this flag a **switch or a setting**? Can it be restated, or turned back off by a later flag? | A setting must be read to the end of argv. First-match scanning of `-d`, `-maxdepth` produced both bypasses and false refusals. |
| Does the **sibling path** do this too — install vs uninstall, preview vs execute, Layer 1 vs Layer 2, compiled artifact vs its source? | Install and uninstall diverged repeatedly; preview advertised what the installer refused more than once; a generated file edited by hand is stale on the next build. |
| Did this diff put a **site literal** in the tree, or a site **decision** into code? | A hostname, mount path, partition name or measured figure belongs in a site's `site.toml` or its private evidence, never here (ADR-0014). A rule that should be a `site.toml` key — a mount's class, a hook shell, a depth ceiling — belongs in the schema and the generator, not in a branch (ADR-0016, ADR-0008, ADR-0007). |

## Both directions are defects

Layer 1 is **advisory** by design: `/usr/bin/find` walks straight past it. So:

- a **bypass** matters because Layer 2 is the only backstop, and
- a **false refusal** matters just as much, because a guard that is wrong
  about a legitimate command teaches every user of the node to alias around
  it permanently, after which neither layer is in front of anything.

When you report a parsing finding, say which direction it fails in. A fix that
closes a bypass by introducing a false refusal is not an improvement.

## Verify before filing

1. **Run the tool.** Grammar claims must be measured, not read from `--help`.
   `fzf --color` prints like a required-value option and consumes nothing;
   `du --time` is its own optional-argument option, not a prefix of
   `--time-style`; `grep --binary` is not a prefix of `--binary-files`;
   `grep -d` is last-wins. Every wrong grammar claim in this project's history
   came from reading about a tool; every correction came from running it. Say
   which version you ran.
2. **Check it is still true at `HEAD`.** Reviews are computed against a
   snapshot. Before filing, confirm the code still looks that way — findings
   have been filed against lines that were already gone.
3. **Check the ADRs and existing replies.** If a thread already answers this
   with a measurement or an ADR, do not re-file it; add to that thread.
4. **`walk-blocker build --check` passes.** A finding about a compiled
   artifact is usually about its source or the schema. Locate the source line
   before filing.

## Decided — do not propose these again

Each was raised, considered, and settled with reasons. Re-proposing them costs
a round and gets the same answer. ADRs live under `docs/adr/`.

| Proposal | Answer | Where |
|---|---|---|
| Read `site.toml`, or any configuration file, on the node at run time | Every site value is compiled into the artifacts; the node has no config parser | ADR-0013 |
| Give `deploy.py` path flags, or add validation for them | It takes no path arguments; the values are stamped literals whose shape the schema and one test assert | ADR-0005, ADR-0013 |
| Refuse to deploy from a source tree that is not root-owned | Rejected by the owner; the trust boundary is the installed artifact | ADR-0006 |
| Rewrite the shim in Python | Measured at a reference deployment: interpreter start is roughly an order of magnitude slower than `sh`, and a Python shim cannot be fork-free on the fast path | ADR-0015, ADR-0018, `node/shim/measure.sh` |
| Add a `statfs`, network or configuration read to the shim | The shim reads `/proc/mounts` and nothing else; a guard that hangs when the filesystem hangs is worse than no guard | ADR-0015, ADR-0007, ADR-0016 |
| Make `uncovered_mount` fire on every poll, or only once ever | It fires on change: first seen, covered, unmounted; the memory is `uncovered-mounts.state` in the spool and a new boot reports once more | ADR-0019 |
| Report the guarded path's one `awk` as a violation of "forks nothing", or add a second program beside it | The fork-free rule is the fast path's; the guarded path runs exactly one documented program, counted under `strace` | ADR-0018 |
| Hardcode a mount, host or path in the rule table | Paths are `site.toml` rows, compiled in; the rule table contains none | ADR-0007, ADR-0014, ADR-0016 |
| Key a mount's class on filesystem type alone, or allow an unknown remote type by default | Type is the default, remoteness is the fallback, an unknown remote mount is guarded; overrides loosen | ADR-0016 |
| Prompt the administrator interactively at install time | Unreviewable; the mount list is a diff, and `walk-blocker survey` proposes it out of band | ADR-0016, ADR-0005 |
| Ship a default timer slot, add jitter, or coalesce the timer | The slot is chosen against the node's live schedule and recorded beside `site.toml`; `RandomizedDelaySec` and a loose `AccuracySec` undo the choice (ADR-0017) |
| Copy a figure from a predecessor record into this tree | Decisions, not evidence; name the class of measurement and the re-measure condition | ADR-0014 |
| Make Layer 1 enforcing rather than advisory | Advisory is the design; Layer 2 is the backstop | ADR-0001 |
| Let PSI gate scanning or `--kill` | PSI corroborates a finding; it never gates one | ADR-0009 |
| Gate `opaque_traversal` on "on an expensive mount" | It names tools the model does not cover; the mount is unknown by definition | ADR-0010 |
| Treat `fd --exec=CMD` or `-xCMD` as consuming the rest of argv | Only the **separate** `-x CMD` form does; measured with a positive control | matrix rows in `tests/argv_cases.py` |
| Fold a best-effort hook into the hard gate, or soften a required one | The class is the site's call from a census, per shell, in `[hooks.<shell>].gate` | ADR-0008 |
| Make the audit directory `0750` | Root-writable-only and world-readable is the decision | ADR-0012 |

## Repository rules

- **Never propose editing generated files.** That is everything under
  `examples/payload/**`, and any line in `node/` carrying a `# GENERATED from`
  marker. Change `src/walk_blocker/search_rules.py`, the `.in` template, the
  schema, or `site.example.toml`, and rebuild; `walk-blocker build --check`
  fails on a stale artifact.
- `--report` is the default and `--kill` is a separate human decision. Do not
  suggest promoting it.
- Issue and PR text describes shapes, not identities: no usernames, no live
  PIDs, no hostnames, no site paths, no customer or vendor names. "A remote
  mount", "a user's process", "the reference deployment".
- A finding about a value in `site.example.toml` is a finding about a
  fictional example. Do not propose making it "realistic"; the realistic
  values are a site's, and they live in that site's own `site.toml`.

## Maintaining this file

Add a rule here only when a finding recurs or a decision is made that would
otherwise be re-litigated. Prefer narrowing an existing row to adding one.
When an ADR settles a question, add it to the decided table with its number
rather than describing the reasoning again. Nothing added here may carry a
site literal (ADR-0014).
