# ADR-0017: The reaper's timer slot is site configuration chosen against the live schedule of the target node

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0017 — see `docs/evidence.md`

## Context

Layer 2 runs on a `systemd` timer, and a login node is shared: it already
carries other tenants' timers, cron lines and user-level schedulers, some of
them latency-sensitive. Every firing of the reaper is a process-table scan
plus the reconcile, and a firing that lands on top of a neighbour's collector
adds jitter to a measurement someone else is taking on the same machine. At a
reference deployment the neighbours were enumerated by hand, and the slot was
placed in a minute and a second that none of them used; that placement has
held, and the reason it held is that it was taken from the live schedule and
not from any document.

Until this record, the rule lived in `CLAUDE.md` as a non-negotiable and in
the schema's description of the key. `[timer].on_calendar` is cited by the
governance files and by the installer checklist, but no ADR said why the slot
is configuration rather than a default, what "chosen" means, or what a later
change to the schedule obliges a site to do. A rule that exists only as a
non-negotiable is easy to obey and hard to cite, and the question of whether a
default slot would do keeps arriving with each new site.

Three alternatives were on the table.

- **A default slot shipped in the tree**, in its strongest form: pick a
  second that no distribution's stock timers use, ship it as the schema
  default, and let a site override it. Rejected because the collision that
  matters is with a tenant's timer, not a distribution's, and the tree cannot
  know a tenant's schedule. A default is copied; a copied slot is the one
  thing this record forbids.
- **Randomised delay**, so that collisions average out. Rejected because it
  walks the firing back into the neighbours the slot was chosen to miss, and
  because it makes the audit trail uncorrelatable: "did the reaper run during
  that latency event" is a question a deterministic timer answers and a
  smeared one does not.
- **Coalescing with the default accuracy**, letting `systemd` merge the
  wakeup with its neighbours' to save power. Rejected for the same reason
  from the other side: coalescing is designed to move a firing onto a
  neighbour's, which is the collision being avoided.

## Decision

`[timer].on_calendar` is required site configuration with no default. A site
chooses it against the **live** schedule of the target node — every system
timer, every account's user timers and the site's cron — and records the
census beside its `site.toml`. The slot names both a minute and a second,
because a per-minute collector on the node makes the second field the only
separation left.

`randomized_delay_sec` defaults to zero and `accuracy_sec` to one second,
and the schema records why: jitter and coalescing each undo the choice. A
site may raise either, and doing so is a visible diff.

The compiled unit carries the slot as a literal (ADR-0013). `deploy.py`
prints the rendered timer in its preview so the slot is read before it is
written, and the reconcile's `TimeoutStartSec` floor is validated against the
relink and kill budgets so a firing cannot overrun into the next.

## Consequences

**No two sites share a slot by inheritance.** The example site's slot is
fiction, and a test in the site's own repository, not this tree, is the right
place to pin that a slot matches its census.

**A monotonic neighbour cannot be dodged by a slot.** A timer on
`OnUnitActiveSec` drifts; the census records it, the site accepts the
occasional coincidence, and the audit trail's timestamps are what make the
coincidence visible afterwards. This is a cost, and it is the reason the
trail is deterministic.

**Replacing a deployment means replacing its slot too.** A successor that
inherits a predecessor's slot does so because the predecessor is removed
first, in the same maintenance window; two reapers never fire on one node.
A successor that must coexist with its predecessor takes a fresh census and a
different slot.

**The rule is now citable.** The architect's settled table and the
installer checklist point here rather than at a `CLAUDE.md` paragraph.

## Re-measure when

Before every deploy that changes `[timer]`, and whenever a tenant adds or
moves a timer, a collector, or a cron line on the node. Run `systemctl
list-timers --all` as root and `systemctl --user list-timers` for every
account, read each neighbour's `AccuracySec` and `RandomizedDelaySec`, list
the site's cron, and choose a minute and second no neighbour uses. Keep the
census, dated, beside `site.toml`. A slot that still holds against a new
census is kept on purpose; a slot that no longer holds is changed in a diff.

## Site config touched

- `[timer].on_calendar`
- `[timer].randomized_delay_sec`
- `[timer].accuracy_sec`
- `[timer].persistent`
- `[timer].timeout_start_sec`, `[timer].relink_timeout_s`,
  `[timer].relink_kill_after_s` — the bounds that keep one firing inside its
  slot
