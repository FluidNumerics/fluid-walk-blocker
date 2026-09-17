# Where the evidence is

This tree carries decisions, not the evidence for them (ADR-0014). An ADR
here says what was decided, what was rejected and in what form, what follows,
and — where the decision rests on a measurement — what class of thing was
measured, at what class of deployment, and under what condition a site should
measure again. It does not say the number. The number describes a site, not
the tool; it is that site's operational information; and a second site that
reads it copies it instead of measuring.

The evidence exists. It was gathered at a reference deployment while the
predecessor was designed and reviewed, and every measured claim in the ADR
index rests on a record: the commands run, their output, the conditions they
ran under, and the reasoning from result to decision. It is held privately by
Fluid Numerics, keyed by ADR number. It is not in this repository, not in any
site's build of it, and not in the payload a site deploys.

## What is held, and how to ask

For each ADR that rests on a measurement, Fluid Numerics holds the commands
that were run, the results they produced, the conditions under which they ran
— load, time window, account, what else was on the machine — and the
reasoning that connected the result to the decision. Records are keyed by ADR
number and nothing else.

A site that needs a record asks Fluid Numerics, citing the ADR number. What is
released, and to whom, is decided per request under the terms the reference
deployment's owner set; the ADR number is the key, not an entitlement.

A site that measures for itself — and every "Re-measure when" section is an
invitation to — keeps its own record beside its own `site.toml`, outside this
tree, in the same shape: commands, results, conditions, reasoning, keyed by
the ADR it bears on.

## What is held, by ADR

| ADR | Class of evidence held |
|---|---|
| 0001 Two layers, only one enforces | The text-guard payload harness and the case it missed; a sample of unbounded traversals, by tool |
| 0002 Budget on per-user I/O pressure, not CPU | Per-slice `io.pressure` ranking; differenced-rate calibration over one window |
| 0003 Read PSI at the user slice; origin descriptive | Session-scope census by transport; slice-versus-scope readability probes |
| 0004 Root-owned deployment only | Ownership state after `cp -a` from a user checkout |
| 0005 `deploy.py` takes no path arguments | The root-write-sink × interpolation-context matrix, and the review rounds behind it |
| 0006 Source tree stays user-owned | The repeated review finding, and the snapshot-window measurement |
| 0007 No namespace index; per-mount ceiling is site config; walk-job is the general answer | Mount census (type, capacity, inodes); staged depth walks with pre-registered criteria; vendor catalog sizing; `du -d` traversal counts |
| 0008 Hook shells are a config list, required vs best-effort | Login-shell and live-process shell censuses; startup-file probes per shell |
| 0009 PSI corroborates, does not gate | Differenced PSI per uid against a live process table; counter-by-counter blindness table; full-table classification count; the trail reading behind the alerting split |
| 0010 `opaque_traversal` names unmodelled tools | The first trail reading, by verdict and tool |
| 0011 Layer 2 reads stdin from /proc | `ugrep` stdin behaviour by version; fd/0 device probes; per-process `stat` cost |
| 0012 Audit trail root-writable-only, world-readable | Audit directory mode as deployed, and who could read it |
| 0013 Site configuration is compiled at build time, not read at run time | n/a: structural |
| 0014 The generic tree carries decisions, not site evidence | n/a: structural |
| 0015 Node code is POSIX `sh` or stdlib-only Python 3.9, and the shim is the `sh` part | `sh` versus `python3 -S` start cost; the budget history against load |
| 0016 A mount's class defaults from remoteness, is overridden per mount in site config, and is measured only out of band | n/a: design discussion |
| 0017 The reaper's timer slot is site configuration chosen against the live schedule of the target node | Timer census at a reference deployment: the neighbours, their accuracy and delay settings, and the slot chosen |
| 0018 The fork-free rule is the fast path's; the guarded path pays one documented fork to read the mount table | Guarded-path timing with the trusted `awk` against the pure-`sh` reader on a reference deployment's mount table; the clone and program counts under `strace` are tool facts and live in the test |

## What this file is not

- **It is not a pointer to where the records live.** No path, host, ticket
  system or document store is named here, and none should be added. The key
  is the ADR number and the route is to ask.
- **It is not a promise that a record exists for an n/a row.** A structural
  decision has no measurement behind it, and asking for one returns the ADR.
- **It is not a substitute for re-measuring.** A record from a reference
  deployment says what was true there, then. The "Re-measure when" section of
  each ADR says what a site should do instead of reading it.
