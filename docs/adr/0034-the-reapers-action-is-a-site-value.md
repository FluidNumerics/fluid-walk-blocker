# ADR-0034: The reaper's action is a site value compiled into the unit

**Status:** accepted, 2026-10-08
**Narrows:** ADR-0028, "Under `--kill-others`, an orphaned walk is killed at the first poll after it passes the budget, so it may run for up to the budget plus one poll interval."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

The reaper has carried `--kill` since the first release, tested and off.
The unit the deployer writes said `--report` from a literal, and the
operating guide's promotion section said that changing the unit's
arguments was not a `site.toml` value but an edit to the unit, made at the
site, to be recorded beside the evidence that justified it. The intent was
that a promotion stay a deliberate act rather than a flag a rebuild could
flip.

It had the opposite effect. `deploy.py --system` renders both units from
the payload's constants on every install, and every change to any
`site.toml` value is a redeploy (ADR-0013). So the hand edit was exactly
the thing a rebuild flipped: the next install for any reason, a threshold,
a hook file, a timer slot, rewrote the unit to `--report` and said nothing,
and the decision itself was recorded nowhere a review reached. The unit was
also the one root-written artifact whose content the dry run could not
preview truthfully, because the preview rendered the default and the
operator then changed it by hand.

The flag had a second problem. `--kill` acted only on processes owned by
the invoking uid, and `--kill-others` unlocked the rest. That split was
written for a tool that could be installed for one user; that install mode
is gone, and the reaper's one caller is the root-run system unit. Under it
`--kill` alone acted on root's own processes and nothing else, so the flag
a site would actually set was always `--kill-others`, and the users' page
had to explain a combination that never ran.

The alternatives, in their strongest form:

- **Keep the hand edit and document that a redeploy reverts it.** The
  promotion stays an act a person performs on the node, which is the most
  deliberate form there is. Rejected: it makes demotion a side effect of
  any unrelated change, with no record that it happened, and it leaves the
  one decision this tool exists to gate outside the review the site's own
  repository gives every other value.
- **Have `deploy.py` read the installed unit and preserve its `ExecStart`.**
  The decision would survive a redeploy without becoming a config value.
  Rejected: that is a node-side read of installed state that reaches a
  behaviour, the shape ADR-0013 rules out, and it would make the preview
  depend on what happens to be installed rather than on the payload.
- **A `deploy.py --system --kill` flag.** The root operator decides at
  install time. Rejected on ADR-0005's reasoning: a decision passed as a
  command-line argument to a root-run installer leaves no reviewed record,
  and must be remembered and re-passed on every redeploy.
- **Keep `--kill-others` as a second ceremony beside the key.** Two
  explicit steps are harder to take by accident than one. Rejected: there
  is no second caller for the first step to serve, and a ceremony that
  gates nothing teaches the reader that the flags mean less than they say.

## Decision

`[reaper].action` is a site value, `report` or `kill`, default `report`.
The build stamps it into `deploy.py`, and `render_units()` turns it into
the service's `ExecStart` flag and its `Description`, so the dry run
previews the exact line the install writes and every later redeploy
carries the site's decision.

`kill` renders `--kill`, and `--kill` is the whole action: the reaper
signals findings outside `NEVER_KILL`, whoever owns the process, up to
`[reaper].max_kills` per poll and with `[reaper].kill_grace_s` between
`SIGTERM` and `SIGKILL`; the rest of a poll's findings are recorded
`skipped_kill_cap`. `--kill-others`
is removed from the parser and `skipped_other_user` from the action
vocabulary.

The reaper does not read the key. Its flags come from its argv, so a hand
run of `reaper.py --report` on a promoted node reports and signals nothing.

## Consequences

- Promotion is a reviewed `site.toml` diff and a redeploy (operating guide,
  §12 and §13). Demotion is the same diff in reverse. A unit edited by hand
  on the node is rewritten by the next install, as it always was; now that
  is the documented reason not to do it.
- The bar for setting `kill` is unchanged: ADR-0009's "Re-measure when",
  read against the site's own trail. The key moves where the decision is
  recorded, not what justifies it.
- Killing another user's process is still a human decision made at the
  site; it is now the one reviewed change, made once per site and standing
  until reverted, rather than a per-incident act. The record that justified
  it belongs beside `site.toml`, outside this tree (ADR-0014).
- `walk-blocker validate`, and a `walk-blocker build` that writes, print one
  line on stdout when the value is `kill`, saying what the unit will run.
  `build --check` prints nothing extra, because its stdout is the list of
  differences and an empty one means identical. The line is not a warning:
  the value is valid, and the line is for whoever reads the build log.
- The users' page check, `systemctl cat walk-blocker.service | grep
  ExecStart=`, still answers whether killing is on, and now reads one word.
- A payload whose stamped action was edited past the schema is caught as
  ADR-0029 says: the install's snapshot check refuses it before the first
  `systemctl`, and the dry run prints a note in place of the units and
  refuses at its own payload check, exit 6 and no traceback
  (`test_the_preview_refuses_an_unmapped_action_without_a_traceback`). The
  tables in `deploy.py` are the schema's enum and nothing else, and
  `test_the_action_tables_are_exactly_the_schema_enum` pins that.
- `test_the_compiled_action_is_all_that_puts_kill_in_execstart`,
  `test_the_preview_and_the_install_agree_on_a_promoted_unit`,
  `test_a_promoted_site_stamps_kill_into_deploy_and_nowhere_else`,
  `test_kill_signals_another_users_process` and
  `test_the_parser_no_longer_knows_kill_others` pin the rest.

## Re-measure when

n/a: structural. The condition for setting `kill` is ADR-0009's, and that
record says how to take the measurement.

## Site config touched

- `[reaper].action`, new.
- `[reaper].max_kills` and `[reaper].kill_grace_s`, unchanged, bound what
  `kill` may do in one poll.
