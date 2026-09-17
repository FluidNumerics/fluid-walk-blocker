---
applyTo: "src/walk_blocker/search_rules.py,src/walk_blocker/render/shim.py,node/shim/guard.sh.in,examples/payload/shim/guard.sh,node/shim/measure.sh,tests/test_rules_table.py,tests/test_shim_agrees.py,tests/argv_cases.py,tests/conftest.py"
description: Reviewing the argv rule table and the shell shim generated from it.
---

# The two consumers must agree

`src/walk_blocker/search_rules.py` is the rule table; `guard.sh` is generated
from it by `walk-blocker build`, from the rule table and `site.toml`, through
the template `node/shim/guard.sh.in`. Both implement the same grammar, and
`tests/test_shim_agrees.py` asserts they produce the same verdict for every
row in the shared matrix in `tests/argv_cases.py`.

**A disagreement between them is worse than a defect in either.** Layer 1
allowing what Layer 2 reports, or the table and the shim differing, is the
condition this repo treats as worse than having one layer alone. When you
find one, say so explicitly — it outranks the underlying parsing question.

## Checklist for any change here

- [ ] Was the same change made in **both** consumers? A fix to one is half a fix.
- [ ] Is there a **matrix row** for the shape, and does it exercise the
      *collision* case rather than the clean one? A row using a spelling that
      parses correctly by accident hides a rule that was never implemented.
- [ ] Is there an **inverse row**? Every "this is now blocked" needs a paired
      "this is still allowed", or the fix gets over-applied later.
- [ ] Does a **repeated occurrence** of the option have a row? A last-wins fix
      without one leaves the disagreement it was meant to catch undetectable.
- [ ] For a cwd-dependent row, does it use the `FAST_CWD` sentinel rather than
      any literal mount path? The fixture mount table comes from the test site
      config. `run_shim` falls back to `/` for a cwd that does not exist on the
      test machine, which makes the row prove something else.
- [ ] Does the row contain a **site literal**? Use the fixture table. A real
      mount path, hostname or partition name in a test row is a finding on its
      own (ADR-0014).

## Grammar rules that were measured, not assumed

Do not "correct" these without re-measuring on the tool, and saying which
version:

| Spelling | Behaviour |
|---|---|
| `grep -d`, `-maxdepth`, `-L`, `du -d` | **Last** occurrence wins |
| `grep --binary`, `du --time`, `fzf --color` | Real options, **not** abbreviations or value-takers |
| `--opt[=WORD]` in `--help` | Optional argument: consumes **nothing** |
| `fd -x CMD ...` | Consumes the rest of argv to `;` |
| `fd -xCMD`, `fd --exec=CMD` | Consumes **only** the attached value |
| `find -exec ... {} +` | `+` terminates only after `{}`, and never for `-ok`/`-okdir` |
| `fd --base-directory` | Changes the coordinate system; it is **not** a root |
| `find -xdev`/`-mount`, `du -x`, `rg`/`fd --one-file-system`, `tree -x` | Bound by **device**, not depth: they answer `descends_into` and never `at_or_near_root` |
| `rg --no-one-file-system` | The only spelling that turns a device bound back off; **last** occurrence wins |
| Abbreviations | Only `getopt_long` tools: grep family, `du`. `rg`/`fd` are clap and reject them |
| `classify_mount(entry, policy)` | Both consumers, every branch: listed type, remote by source (`host:`), remote by option (`_netdev`, `addr=`), local unknown type, cheap override, expensive override with `maxdepth` (ADR-0016) |

## The fast path

Everything before `sg_exec_real` runs on **every** `grep`, `find` and `du`
invocation on the node. The fast path — a call decided without a mount
judgement — must fork nothing: no `$(...)`, no backticks, no pipelines. The
guarded path runs exactly one program before the real tool, the trusted `awk`
that reads `/proc/mounts` once (ADR-0018); a second is a design change, not a
tidy-up. Both paths read only `/proc/mounts`: no `statfs`, no network, no
config file (ADR-0015). There is a test for each, counting under `strace`; do
not propose changes that would trip them, and do not propose adding forks or
reads there for tidiness.
