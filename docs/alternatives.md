# What to run instead: a grimoire of walks and their redirects

Every refusal names an alternative, and which one is right depends on what
you were asking — the same `find` is three different questions with three
different answers. This is the generic record of the walk patterns seen at
deployments of this tool and what replaced each one. It carries shapes, not
figures: mount names, sizes and inode counts belong to a site, and the page
a refusal actually names — `docs/what-to-run-instead.md`, rendered per site
into the payload — carries that site's own.

## The rule is about the walk, not the tool

The guard tests one property: **unbounded**. A walk of an expensive mount is
refused when nothing limits how much of the tree it will visit. Bound the
same invocation and it is allowed:

- a depth flag at or below the mount's allowance (the refusal prints the
  allowance that applied; it is per mount, and a site may have measured a
  deeper one for a smaller filesystem — see the site page);
- a root at least `[filesystems].unscoped_depth` components below the
  mount point, which may be walked unbounded because the subtree is already
  scoped;
- a device bound (`-xdev`, `--one-file-system`, `tree -x`, `du -x`) when the
  walk starts on a cheap mount and would only descend into the expensive
  one;
- a cheap mount, which is never judged at all.

The refusal exits 77, a code no wrapped tool claims, so no caller reads it
as "no match" OR as the tool's own failure. It also prints one line on
stdout, because stderr is the stream a scripted caller discards and an exit
code is what a pipeline hides (ADR-0024).

## Ask the question, not the filesystem

### Where did my Slurm job write its output?

**The shape seen**: `find` over `ssh`, two roots, `-name 'slurm-<jobid>*'`,
piped to `head`. The pipe's reader got nothing and never sent `SIGPIPE`; the
walk was orphaned when the session closed and ran for days in D state. This
was the founding incident.

**The redirect**: the scheduler knows and does not have to look.

```sh
sacct -j <jobid> -o JobID,StdOut,StdErr,WorkDir
scontrol show job <jobid> | grep -E 'StdOut|StdErr|WorkDir'
```

Both, because they fail in opposite directions. `sacct` is durable (it reads
the accounting database) but is scoped to the jobs your account may see, and
it returns `StdOut` as stored — with `%j` unexpanded, so substitute the job
id yourself. `scontrol` prints the resolved path but forgets the job
`[slurm].min_job_age_s` seconds after it ends. A site's
`[slurm].extra_job_tools` are printed by the refusal too. The refusal spots
this question by the `slurm-<digits>` in your argv and prints the two
commands with your job id filled in.

### How big is this directory?

**The shape seen**: `du -sh ~`, `du -sh <dataset>` on the expensive mount;
`du -d 2` in the belief that it walks less.

**The redirect**: on a local root, ask directly and stay off other
filesystems — `-x` is the bound that makes it cheap:

```sh
du -x -d1 /var
du -x -sh /
```

On an expensive mount there is no cheap answer without server-side
accounting, and **`du` has no shallower form**: `-d` and `--max-depth` prune
the *output*, not the walk. A total cannot be known without reading
everything below it, so `du -d 1 DIR` issues the same directory reads as
`du -sh DIR` (measured on the tool). The refusal says this rather than
advising a depth flag that would not help. Submit it:

```sh
walk-job -- du -sh DIR
```

### Where is this file?

**The shape seen**: `find <mount> -name '*.ckpt'` from the mount point;
`find <mount> -maxdepth 6 …` — bounded-looking, still over the allowance,
and unnoticed for days.

**The redirect**: start from the deepest directory you can name and bound the
walk to the allowance the refusal printed:

```sh
find DIR -maxdepth N -name 'PATTERN'
ls DIR
```

When the bound does not answer the question — the file really could be
anywhere under a large tree — submit it:

```sh
walk-job -- find DIR -name 'PATTERN'
```

### Which files under this dataset match X?

**The shape seen**: `grep -rIn PATTERN <mount>`; `ugrep -rln PATTERN /`
launched by an automated agent.

**The redirect**: prefer the dataset's own manifest — whatever wrote the data
knew what it wrote, and a listing emitted at write time answers this without
touching a metadata server. Otherwise change tool, because **`grep -r` has
no depth flag** and cannot be bounded by any argument; `rg` can:

```sh
rg --max-depth N PATTERN DIR
find DIR -maxdepth N -type f -exec grep PATTERN {} +
walk-job -t 4:00:00 -- grep -r PATTERN DIR
```

### I want to stay off the expensive mount entirely

**The shape seen**: `find / …`, `du -sh /`, a walk that starts on the local
root and would descend into a mounted filesystem.

**The redirect**: a device bound stops the walk at a mount boundary, and the
refusal offers it whenever the walk starts on a cheap mount and merely
descends into an expensive one:

| Tool | Device bound |
|---|---|
| `find`, `bfs` | `-xdev` (also `-mount`, `-x`) |
| `du` | `-x`, `--one-file-system` |
| `rg` | `--one-file-system` (and only `rg` can turn it back off, with `--no-one-file-system`; the last one wins) |
| `fd` | `--one-file-system` |
| `tree` | `-x` |

## The per-tool table

Every refusal prints the alternative for the tool that was refused. For
reading before you are refused:

| Refused | Bound the walk with | Or |
|---|---|---|
| `find`, `bfs`, `gfind` | `-maxdepth N`; a device bound when the walk starts on a cheap mount | `walk-job -- find ...` |
| `rg` | `--max-depth N`, `--one-file-system` | `walk-job -- rg ...` |
| `fd`, `fdfind` | `-d N`, `--one-file-system` | `walk-job -- fd ...` |
| `tree` | `-L N`, `-x` | `walk-job -- tree ...` |
| `du` | `-x` only — its `-d` prunes output, not the walk, so no depth flag bounds it | `walk-job -- du -sh DIR` |
| `grep`, `egrep`, `fgrep`, `zgrep`, `rgrep` | no depth flag exists; change tool: `rg --max-depth N`, or `find DIR -maxdepth N -type f -exec grep ... {} +` | `walk-job -- grep ...` |
| `ugrep` | `--depth N` (also spelled `-N`). A bare directory operand is already depth 1, so the common shape is fine as typed; `-r` is what makes it unbounded | `walk-job -- ugrep ...` |
| `fzf`, `sk` | not wrapped (`[shim].unwrapped_tools`): they are reached through a keybinding or an editor plugin, where a refusal is an invisible no-op. They walk the current directory whenever nothing is piped in; pipe something in — `ls DIR \| fzf`, or a manifest — and they filter instead of walking. Layer 2 still sees them | — |

`N` is the depth the refusal prints: `[filesystems].maxdepth_allowed`, or
the mount's own `maxdepth` where the site measured one.

## Shapes Layer 2 has seen

The reaper reads the whole process table every poll and names what the shim
could not stop. These are the shapes its trails have carried, so that a new
one is recognised for what it is:

- **`ssh host 'find A B -name … | head'`** — the founding incident: two
  roots, a reader that never received its lines, orphaned on teardown,
  D state for days. Verdict `orphan_traversal`.
- **`ssh host 'bash -c "find …"'`** — the same walk in a second-level
  shell. The shim is on the login shell's PATH and catches it; a private
  PATH or an absolute path does not go through it.
- **`cd <mount> && find . -name x`** — a walk rooted in the current
  directory. The shim resolves `.` against the cwd, so this is judged like
  any other root.
- **`… | xargs -I{} find {} -name x`** and **`cat dirs | xargs -P16 -I{}
  find {} -maxdepth 2 …`** — a fan-out of bounded walks under one parent.
  Each child passes the shim; together they are one unbounded walk, and the
  reaper counts them as one (`fanout_traversal`, `[reaper].fanout_n`).
- **`grep -rIn … <mount>`** and **`du -sh ~`** — the two ordinary shapes,
  caught by the shim when typed, seen by the reaper when they were not.
- **An agent-bundled `ugrep -rln PATTERN /`**, reached through a shell
  function that runs `exec -a ugrep <bundled binary>`: `argv[0]` forged, PATH
  never consulted. The shim never sees it; the reaper is the only layer for
  that traffic, and the reason it exists. Verdict `runaway_traversal`.
- **`bfs` from a user's own `~/bin`** — a tool installed where no shim is
  linked, and one whose `comm` reads as a version string, which is why the
  reaper keys tool identity on `argv[0]`, never on `comm`. Verdict
  `orphan_traversal` when its parent had gone.
- **`find <mount> … -maxdepth 6`** — bounded-looking, over the allowance,
  three days in D state before anyone looked.
- **`uvx rg …`** — a tool fetched and run from a cache directory, outside
  every PATH shim. Layer 2 only.
- **`python3` in `os.walk`, `rsync`, `tar`** — tools the rule table has not
  modelled. Verdict `opaque_traversal`, never killable, recorded so the
  table can grow; named only when blocked at two consecutive polls
  (ADR-0020).
- **`tail -n0 -F <logs>`** — long-lived, D state, reparented to init, and
  not a traversal: `tail` has no recursion option under any argv. Excluded
  by `[reaper].stream_filters` (ADR-0010), and the reason that list exists.
- **`ugrep --line-buffered -E …`** on a pipe — a log filter, not a walk;
  `ugrep` decides whether to walk by whether stdin is a terminal, which is
  why Layer 2 reads stdin from `/proc` (ADR-0011).

## `walk-job`

Every refusal names it. `walk-job -- COMMAND` submits the command to
`[slurm].partition` under a wall-clock limit the scheduler enforces, always
passing `--mem`. It is linked beside the guard, so it is on your PATH
wherever the guard is and nowhere the guard is not.

- **What it buys**: a bound that is not advisory. `-maxdepth` asks a walk to
  stop; the scheduler kills a job that runs past its limit, so a walk nobody
  is watching still ends. A namespace too large to bound by depth without
  answering a different question is bounded by time instead (ADR-0007).
- **What preemption does to it.** A walk-job is submitted at whatever QoS the
  site configured, often a low one, which is where a preemption policy aims
  first. It is always submitted `--no-requeue`, with no way to turn that off:
  where a site's `PreemptMode` is `REQUEUE`, a preempted job goes back on the
  queue and **starts the walk again**, turning one traversal into as many as
  the scheduler decides, each paying the full metadata cost with nobody
  watching. With `--no-requeue` a preemption costs one dead job and a message
  you can act on. If walk-jobs at your site are preempted often, the QoS is
  the thing to change, not this.
- **What it does not buy**: it moves load off the login node, not off the
  filesystem. The same metadata operations reach the same storage from a
  different client. A walk that should not happen at all is not fixed by
  submitting it.
- **Memory**: raise `-m` only after a job was killed for exceeding it, never
  "to be safe". On a partition whose default is unlimited, a job that names
  no memory is charged the whole node and queues behind an otherwise idle
  machine.
- **The override is journaled on the compute node.** `walk-job` sets the
  escape-hatch variable in the job's environment on purpose, so the
  submitted walk is not refused where it runs; journald is per host, so the
  record lands on the node the job ran on, not the login node.

## Why not `locate`?

The guard never advises an index it cannot back, and no refusal names
`locate`. Where a site keeps a `plocate` database, it answers only for the
roots `updatedb` was allowed to index — normally the local ones — and the
database's own visibility rule is what keeps one user's filenames from
another: `updatedb.plocate` requires a database that is not readable by
others, and `locate` is setgid to the group that may read it. The repair for
a `Permission denied` is to correct that group, never to loosen the database
mode; a database readable by all is a filename listing of every home. Extra
databases are picked up through `LOCATE_PATH`, so plain `locate` searches
all of them.

## The escape hatch

```sh
WALK_BLOCKER_UNSCOPED=1 find DIR ...
```

The guard is advisory: `/usr/bin/find` bypasses it outright. The point of
the variable is that the override is **recorded** — one journal line under
the `walk-blocker` tag, with a uid stamped by journald from the socket
rather than taken from the environment being audited — instead of invisible.
It still runs on the login node and still shares it with everyone there. If
you reach for it routinely, the table above is missing your case: say so
rather than working around it quietly.

## Where the site's own answers are

`docs/what-to-run-instead.md`, beside this file in the installed payload, is
rendered from the site's `site.toml`: its mounts, their classes and depth
allowances, the survey's measurements as of their date, its `walk-job`
partition and defaults, and the documentation and contact lines it chose to
print. Every refusal names that page by its installed path.
