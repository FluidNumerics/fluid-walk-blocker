# ADR-0037: The reaper's action is a required site value with no default

**Status:** accepted, 2026-10-30
**Narrows:** ADR-0034, "`[reaper].action` is a site value, `report` or `kill`, default `report`."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

ADR-0034 made `[reaper].action` a site value compiled into the unit, and
gave it a default: "`[reaper].action` is a site value, `report` or `kill`,
default `report`." That moved the decision into the site's reviewed
`site.toml`, but only for a site that wrote the key. A site that left it
out still shipped an action, and the review of its repository showed
nothing, because the line that decided it was not there to read.

Neither value is safe to take by omission. A silent `kill` ends other
people's work on a decision nobody wrote down. A silent `report` is the
failure ADR-0034 set out to end, in another form: the site believes the
backstop acts and it does not, and nothing in its own repository says so.
The two failures differ in who is hurt, not in whether a decision was
skipped.

The schema already has the precedent. ADR-0025 gave `[install].spool_group`
and `[install].trusted_groups` no default, because a default reader group
or trusted list would be a guess about the site. The practice of writing
every key in a client-facing `site.toml` was a habit, not a check, and a
habit is what a new site's first config forgets.

The alternatives, in their strongest form:

- **Keep the `report` default.** It is the conservative value: a site that
  forgets the key loses enforcement and gains no new harm, and the trail it
  writes is the evidence ADR-0009 asks for before `kill`. Rejected: lost
  enforcement is itself the harm this tool exists to prevent, and a default
  that is safe for the people whose processes run is not safe for the node
  they share. "Report before kill" is guidance about when a site chooses
  `kill`, not a value the engine picks for it.
- **Keep the default and have `validate` warn when the key is absent.** The
  site is told without being refused. Rejected: a warning in a build log is
  read once, if at all, and the value it warns about still ships; ADR-0034
  already prints a line for `kill` and notes that the line is not a warning.
  A required key puts the decision in the diff a reviewer reads.
- **Make the example say `report`.** The example would model the cautious
  first deployment. Rejected: the example is the template a site copies,
  and a copied `report` is the same unexamined default moved one file over.
  The example states `kill`, with a comment saying what it does and that a
  site reads its report trail before choosing it, so it shows the decision
  being made rather than standing in for one.
- **Bring back `--kill-others` as a second step for another user's
  processes.** Rejected for ADR-0034's reason: the reaper's one caller is
  the root-run unit, and a ceremony that gates nothing teaches the reader
  that the flags mean less than they say.

## Decision

`[reaper].action` is required and has no default. The schema lists it
under the `[reaper]` table's `required`, so a site that omits the key, or
the whole table, is refused by `validate` and `build` with the key named.

The shipped example sets `action = "kill"`. Whether a site starts at
`report` is the site's call, reviewed in the site's own repository; the
bar for `kill` stays ADR-0009's.

## Consequences

- The decision is a line in every site's `site.toml`, so the review of
  that repository always reaches it. Writing every key in a client-facing
  config is now a schema fact for the one key where omission was most
  costly.
- A `site.toml` that validated under ADR-0034 without the key no longer
  does. The fix is one line, and the refusal names it:
  `/reaper: 'action' is a required property`.
- The example payload, and so this suite's stamped `deploy.py`, renders
  `--kill`. The tests that pin the `report` rendering set it explicitly;
  `test_the_reaper_ships_the_action_the_site_states` pins that the
  payload carries the example's stated value.
- `validate` and a writing `build` of the example print ADR-0034's notice
  line. That is the line working, not noise.
- `node/reaper.py` keeps `--report` as its own argparse default. A hand
  run with no flag reports; that is the reaper's behaviour under its argv
  (ADR-0034), not a site default, and the unit always passes a flag.
- `test_the_reaper_action_is_a_closed_set_with_no_default` and the
  `reaper.action removed` row of `test_bad_site_is_refused` pin the rest.

## Re-measure when

n/a: structural. The condition for choosing `kill` is ADR-0009's.

## Site config touched

- `[reaper].action`, now required.
