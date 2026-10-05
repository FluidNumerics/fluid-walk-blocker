# What to run instead on Example HPC login node

This node runs `walk-blocker`, which does two things to commands that walk
one of its expensive filesystems with no bound: a guard on your PATH refuses
them before they start, and a reaper that runs as root finds the ones that
got past the guard. This page is the short answer for this node, filled in
from its own configuration. The longer one — the reasoning, and the walk
patterns seen at other sites and what replaced them — is `alternatives.md`
in this directory, installed at
`/usr/local/lib/walk-blocker/docs/alternatives.md`.

## If walk-blocker stopped your command or killed your process

### Why it happened

**A refusal** happens before anything runs. The guard prints why on stderr,
one line on stdout, and exits 77. The walk would have visited every inode
below its root on a guarded mount. A walk bounded within the mount's depth
allowance, or one whose root sits at least 2 directory components below the
mount point, is allowed and always was. The refusal prints the depth that
applied.

**A kill** can come only from the reaper, and only when it runs with
`--kill-others`. By default it only reports: it records what it would have
killed and signals nothing. When it does kill, it sends `SIGTERM`, waits,
then sends `SIGKILL`. It kills a process for one of three reasons, all of
them about a traversal tool walking a guarded mount:

- the walk ran longer than 900 seconds while the process that started it was
  still alive;
- the walk ran longer than that, counted from its own start, and the process
  that started it had gone, for example an ssh session that closed without
  taking its command with it;
- 4 or more such walks were running at once under one parent.

It never kills a command it could not parse, a tool it does not model (even
one stuck in I/O), or an idle orphan. It only reports those.

### How to check

1. **Is killing switched on at all?** Any account can read the unit:

       systemctl cat walk-blocker.service | grep ExecStart=

   If that line says `--report`, or has `--kill` without `--kill-others`,
   walk-blocker has not killed anything of yours, and something else ended
   your process.

2. **Does the timing fit?** The reaper runs only when its timer fires:

       systemctl list-timers walk-blocker.timer

   A kill lands after the timer fires, while that run lasts. The reaper
   signals one process at a time and waits for each, so with several
   findings that can be minutes. It shows as `Terminated` (exit 143), or as
   `Killed` (exit 137) if the process ignored `SIGTERM`. The kernel's
   out-of-memory killer, a scheduler, and a person running `kill` all look
   the same from your shell, so a matching exit code is not proof.

3. **Ask for the record.** You cannot read it yourself unless the node's
   ACLs let you. The reaper's audit trail is readable by root, the `wbaudit`
   group, and any account the node's ACLs name on it, and the reaper's
   output goes to the system journal, not to yours. Give the people who run
   this node the time, the command, and the pid if you have it. The trail
   records every kill, and every kill it attempted that did not land.

A refusal is different: it is recorded in your own journal, which you can
read for as long as the node's journal keeps it. On a busy node that may be
less than a day.

    journalctl -t walk-blocker -o json

### How to avoid it next time

- Bound the walk: give it a depth within the mount's allowance, or start it
  from a directory deep enough to be allowed. The table below has both.
- Put a long walk on a compute node with `walk-job`. See below.
- Make sure a remote command ends when its session does. A client that times
  out and retries leaves the first walk running, with nobody reading its
  output.
- Do not run 4 or more walks in parallel from one parent, such as `xargs -P`
  over `find`. Run one bounded walk, or submit the parallel version with
  `walk-job`.
- Put your own limit on a walk you expect to be long: `timeout` below 900
  seconds ends it on your terms, whether or not your session is still open.

## The mounts this node guards

| Mount | Class | Bounded walk allowed to depth | Measured |
|---|---|---|---|
| `/home` | expensive | 4 | 24.4M inodes in use, 81.9 TiB, as of 2026-01-15 |
| `/opt/site-tools` | cheap | not bounded (cheap) | not surveyed |

Any other mount whose filesystem type is one of `lustre`, `wekafs`,
`beegfs`, `gpfs`, `ceph`, `nfs4`, `nfs`, `cifs`, `smb3`, `glusterfs`,
`panfs`, `fuse.*`, `9p`, `sshfs`, `s3fs`, `daos`, or that is mounted from a
remote source, is guarded on the compiled default: depth 2. The measurements
are what `walk-blocker survey` read on the date shown; a full walk visits
every inode in use on that mount.

## Ask the question, not the filesystem

### Where did my Slurm job write its output?

Ask the scheduler. It knows, and it does not have to look:

    sacct -j <jobid> -o JobID,StdOut,StdErr,WorkDir
    scontrol show job <jobid> | grep -E 'StdOut|StdErr|WorkDir'
    job-report <jobid>

`sacct` is durable but scoped to the jobs your account may see, and it
returns `StdOut` as stored, with `%j` unexpanded. `scontrol` prints the
resolved path, for 300 seconds after the job ends. The two fail in opposite
directions, so try both before concluding either is broken.

### How big is this directory?

    du -x DIR                    # on a local root: -x is the bound that makes it cheap
    walk-job -- du -sh DIR       # on a guarded mount

`du` has no shallower form: its `-d` and `--max-depth` prune the output, not
the walk, so `du -d 1 DIR` costs exactly what `du -sh DIR` costs.

### Where is this file?

    find DIR -maxdepth N -name 'PATTERN'     # N from the table above
    ls DIR
    walk-job -- find DIR -name 'PATTERN'     # when the bound does not answer it

Start from the deepest directory you can name: a root at least 2 components
below the mount point may be walked unbounded.

### Which files under this dataset match?

Prefer the dataset's own manifest: whatever wrote the data knew what it
wrote. Otherwise change tool — `grep -r` has no depth flag and cannot be
bounded — or submit it:

    rg --max-depth N PATTERN DIR
    walk-job -t 4:00:00 -- grep -r PATTERN DIR

## walk-job on this node

`walk-job -- COMMAND` submits the command to the `datamover` partition under
a wall-clock limit the scheduler enforces (default 1:00:00, memory 8G;
`walk-job -h` lists the options). It is linked beside the guard in
`/usr/local/lib/walk-blocker/bin`, so it is on your PATH wherever the guard
is. It moves load off the login node, not off the filesystem: the same
metadata operations reach the same storage from a different client. Raise
`-m` only after a job was killed for exceeding it, never to be safe.

## The escape hatch

    WALK_BLOCKER_UNSCOPED=1 COMMAND

The guard is advisory: an absolute path bypasses it outright. The variable
exists so that an override is recorded (`journalctl -t walk-blocker -o
json`) rather than invisible. If you reach for it routinely, this page is
missing your case — say so rather than working around it quietly.

Documentation: <https://docs.example.org/hpc/walk-blocker>

Contact: hpc-help@example.org

Rendered by walk-blocker 0.3.2 from this node's `site.toml`; the mount table
above is that file's `[[filesystems.mounts]]`, as built.
