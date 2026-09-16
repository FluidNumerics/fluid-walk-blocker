# What a site measures before filling `site.toml`

Every value in `site.toml` is compiled into the node artifacts (ADR-0013),
and most of them are not defaults to accept but measurements to take. This
page is the procedure for each: what to measure, how to measure it without
causing the incident the tool exists to prevent, and which key the result
lands in. It carries no figures and no site names (ADR-0014); the numbers
belong beside your own `site.toml`, outside this tree.

Start from `examples/site.example.toml`, a fictional site with every key
written out, and run `walk-blocker validate --site site.toml` after each
change. Validation is shape and consistency; it cannot tell a measured value
from a copied one.

## Census the login shells

Keys: `[hooks.bash]`, `[hooks.zsh]`, `[hooks.fish]` — `enabled`, `file`,
`package`, `gate`. Decision record: ADR-0008.

The shim reaches a remote command only if some startup file put the shim
directory on `PATH` before the command was exec'd, and which file fires for
`ssh host 'cmd'` differs per shell. Which shells matter is a property of the
site, not of the shells, so it is a census, taken twice.

1. **Registered login shells.** `getent passwd` lists every account's
   shell. Tally the distinct shells. A shell that is any account's
   registered login shell is exec'd directly by `ssh host 'cmd'`; no other
   shell's hook can cover it.
2. **Shells actually running.** A live process census on the login node
   (`ps -eo comm=` is enough) over a representative period, not one moment.
   This finds shells reached by nesting — typed by hand inside another
   shell — and shells present as a per-user install rather than a system
   package. Every `sh` process is typically `sh -c '<command>'` launched by
   other tooling, not a personal shell; it is noise here.

Then, per shell:

- A **registered login shell** is `gate = "required"`. Its absence at
  install is an anomaly; the install fails if the hook cannot be proven to
  fire under remote-command conditions.
- A **nested-only shell** is `gate = "best-effort"`. It is written and
  verified when its binary resolves, and its failure never gates the
  install. Do not promote it to soften a warning, and do not demote a
  required shell to make an install pass.
- `file` is the system startup file that fires for a non-interactive,
  non-login invocation — the shell's own documentation says which, and the
  distribution family may relocate it. `package` is the package that owns
  that file, so the installer can warn that it is a managed conffile. The
  installer verifies the file fires; this table only says where it is.

Keep the census output beside `site.toml`. Re-run it when a shell is added
to the node's packages or a group of accounts changes its shell.

## Identify the ssh transports and scope naming

Key: `[reaper].origins`. Decision record: ADR-0003.

A finding records how its process arrived — a login session, a scheduler
step, a container — as a descriptive label read from the process's cgroup
leaf name. More than one inbound ssh transport may serve one node, and each
registers its sessions under its own scope-naming scheme.

1. On the node, `systemd-cgls` shows the tree under each user slice; for a
   given process, the last component of `/proc/<pid>/cgroup` is the leaf
   name the reaper sees. Collect the distinct leaf shapes over a period:
   seated sessions, seatless sessions, container scopes, the per-user
   manager service.
2. Identify what produced each shape. A second ssh transport that terminates
   the connection in its own daemon lands sessions under a different scope
   name from `sshd`; a container runtime has its own.
3. Add one `{pattern, label}` entry per naming scheme, in the order they
   should be tried. A leaf that matches nothing is `other`; an unreadable
   cgroup file is `unknown`. Both are legitimate, and a rising `other`
   count is the signal that the table is behind the site.

The label is descriptive only; it never enters the kill decision. Match the
leaf name, never the full path — the path carries another user's session
id, and the audit log has to stay a document that can be shown to the
person whose process is in it (ADR-0012).

## Survey the mounts and decide per mount

Keys: `[filesystems].remote_fstypes`, `remote_proxy`,
`[[filesystems.mounts]]`. Decision record: ADR-0016.

A mount's class defaults from `/proc/mounts` fields alone: a type on the
remote list, a `host:` source, or a `_netdev`/`addr=` option makes it
expensive. That default fails toward refusal, and the refusal names
`walk-job`. Overrides loosen it, per mount, in a diff. Narrowing the type
list or turning `remote_proxy` off loosens every mount at once and is not
the fix for one small export.

1. Run `walk-blocker survey` on the node as an administrator (the payload
   ships it as `survey.py`; it needs only the node's `python3`). It takes a
   timeout-bounded `statvfs` per mount in a child process, so a wedged
   mount is reported `unmeasured` rather than hanging the survey, and it
   never writes a file.
2. Read the table: type, remoteness and why, the compiled default, capacity
   and inode count where measured. Network and parallel filesystems may
   report synthetic totals — a quota, a tiered capacity, a per-client view —
   so treat the figures as evidence to weigh, not a verdict.
3. Decide per mount. A small remote export that is cheap to walk in full
   gets a `class = "cheap"` entry with a comment saying why. A remote mount
   that should keep its default gets no entry — delete the proposed one.
   A mount that deserves a deeper bounded walk gets `maxdepth`, but only
   after the measurement in the next section.
4. **A mount left on its default should be left there on purpose.** Keep
   the survey output beside `site.toml`: it is the record that the default
   was seen and chosen, not overlooked. The reconcile logs a line per mount
   that no override covers, so the choice stays visible on the node too.

Re-run the survey when the reconcile reports a mount you did not decide
on, when a filesystem is added or replaced, and when a site disputes a
default in either direction.

## Measure a depth allowance without causing the incident

Keys: `[filesystems].maxdepth_allowed`, `depth_allowance_max`,
`[[filesystems.mounts]].maxdepth`. Decision record: ADR-0007.

A per-mount `maxdepth` authorises a bounded walk deeper than the global
ceiling on that mount alone. The natural first draft of the measurement —
an unbounded `find` per root to see how big things are — is the incident
this tree exists to prevent, run in the name of preventing it. The method
below is the bar; the figures it produces are yours.

1. **Pre-register the criteria.** Before any walk, write down what entry
   count and what wall time at the proposed depth would be acceptable, and
   make them stricter for a larger mount, not looser. A criterion chosen
   after seeing the result is not a criterion.
2. **Walk one root at a time, bounded and capped.** Each walk is
   depth-bounded at the depth under test, capped at an entry count so it
   aborts on the first cap hit, and paused between roots. Run it from a
   `[slurm].partition` node — never from the login node — with the
   filesystem watched from elsewhere so the run can be cancelled the
   moment it hurts anyone.
3. **Never run depth N+1 when depth N already fails the bar.** A walk at
   depth N+1 is a superset of the walk at depth N, so its entry count and
   wall time are at least as large by definition. Start from the depth the
   ceiling already permits; if that already exceeds the criteria, the
   question is answered without walking deeper. Measuring the thing must
   not cause the thing.
4. **Sample the shape.** A root that is trivial at depth N can be enormous
   at N+1 — "shallow and wide" means the width sits exactly at the level a
   raise would open. A capped sample of roots, aborting on the first cap
   hit, gives the shape of the risk before any full count.
5. **Measure the content-reading tool before `find`.** The allowance is
   tool-agnostic, and `rg --max-depth` is the content-reading tool the
   refusal text itself recommends. What a raise authorises is not only a
   metadata walk but a multi-threaded read of every byte one level deeper,
   on the login node, with no wall-clock bound. Time that first.

Record the criteria, the roots sampled, the depth reached and the result
beside `site.toml`, and re-run the method before raising any allowance,
including when adding a mount that was not listed before. `validate`
enforces only that no `maxdepth` exceeds `depth_allowance_max`.

## Calibrate the stall thresholds

Keys: `[reaper].io_stall_fraction`, `cpu_stall_fraction`. Decision records:
ADR-0002, ADR-0009.

PSI totals are cumulative since boot. A threshold read off the cumulative
table ranks users by how long they have been logged in, and every finding
it produces is a finding forever. Calibrate from the differenced rate over
one poll window, never from a total.

1. Under **known load** — one unbounded recursive traversal running in one
   user's slice, which you start and can stop — read that slice's
   `io.pressure` `full` total at the start and the end of one poll
   interval and difference them. Do the same for every other user slice on
   the node at the same time.
2. The bar is the gap: the differenced fraction for the loaded slice
   against the highest differenced fraction any legitimate slice sustains
   over the same window. Set `io_stall_fraction` inside that gap. If there
   is no gap, the threshold cannot separate them at this poll interval and
   the record should say so rather than picking a number.
3. Repeat for `cpu.pressure` and `cpu_stall_fraction`.

The thresholds select which findings carry `stalling_slice: true`; they
never suppress a record and never gate a kill. A wedged walker shows zero
differenced I/O stall while it hurts everyone, which is why every poll
classifies the whole process table regardless. Re-calibrate when the
node's load class changes.

## Choose the timer slot against the live schedule

Keys: `[timer].on_calendar`, `randomized_delay_sec`, `accuracy_sec`,
`timeout_start_sec`. Decision record: ADR-0017.

This node is shared, and the reaper's poll is one more scheduled thing on
it. The slot in the example is fictional; copying it is the failure mode.

1. As root, `systemctl list-timers --all`. For every account on the node,
   `systemctl --user list-timers` (a per-user manager's timers are
   invisible from the system manager). Add the site's cron.
2. Pick a minute **and** a second that no calendar neighbour uses. A
   per-minute collector makes the second field the only separation left.
3. Note the monotonic timers (`OnUnitActiveSec` and relatives): they drift
   and cannot be dodged by a slot. Record which exist and accept the
   overlap knowingly.
4. Leave `randomized_delay_sec` at zero unless the schedule has no clear
   slot — jitter undoes a chosen one — and keep `accuracy_sec` tight so the
   timer fires where it was aimed rather than coalescing onto a neighbour.
5. `validate` computes the floor for `timeout_start_sec` from the relink
   and kill budgets and refuses a value below it, so systemd never cuts a
   poll off mid-kill. It also passes the expression to
   `systemd-analyze calendar` when that tool is present.

Re-survey the schedule when a timer or cron entry is added to the node.

## Measure the shim budgets from local disk

Decision record: ADR-0015. No `site.toml` key: the budgets are arguments to
the payload's `measure.sh` and live in the site's own evidence.

`measure.sh` times the fast path (an allowed command) and the guarded path
(a command that reaches a mount judgement) against absolute budgets, and
times the shim against itself so the guarded path's relative cost is
bounded. Run it from **local disk, never from the expensive mount**: the
measurement must not depend on the thing it is measuring the cost of
avoiding. Measure under representative load — a quiet machine reports on
the hour of the day, not on the code — and if the result moves between
runs, record the load with it before deciding anything. Repeat on each
deploy and whenever the node's load class changes.
