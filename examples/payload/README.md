# fluid-walk-blocker

Two guardrails for a shared HPC login node, generic and config-driven: a PATH
shim that refuses an unbounded filesystem walk before it starts, and a
periodic reaper that finds the walks that got past it. `walk-job` is the
sanctioned alternative every refusal names; `deploy.py` installs all of it
as root and takes no arguments.

Every fact about a particular site lives in `site.toml`, is validated by
`schema/site.schema.json`, and is compiled into the node artifacts by
`walk-blocker build`. The node never reads configuration.

**Status: not yet buildable.** This tree currently holds the decision record
and the governance kit (Milestone 0). The schema, the build tool and the node
artifacts arrive in later milestones.

Copyright (c) 2026 Fluid Numerics LLC. All rights reserved. This repository is
private; see `LICENSE`.

## Reading order

1. `docs/adr/0001` through `docs/adr/0016`, in order — the decisions
2. `docs/evidence.md` — where the evidence is, and why it is not here
3. `CLAUDE.md` — the non-negotiables, and the parts that are easy to get wrong

## Working on it

```sh
uv run --group dev pytest tests/ -q
uvx --from pymarkdownlnt==0.9.39 pymarkdown --config .pymarkdown scan README.md CLAUDE.md docs/*.md docs/adr/*.md
uvx yamllint==1.38.0 -c .yamllint .github/workflows/ci.yml
```
