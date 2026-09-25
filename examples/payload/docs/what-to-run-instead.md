# What to run instead on Example HPC login node

A command was refused because it would have walked one of this node's
expensive filesystems with no bound. This page is the short answer for this
node, filled in from its own configuration. The longer one — the reasoning,
and the walk patterns seen at other sites and what replaced them — is
`alternatives.md` in this directory, installed at
`/usr/local/lib/walk-blocker/docs/alternatives.md`.

The rule is about the shape of the walk, not the tool: a walk bounded within
the mount's depth allowance, or one whose root sits at least 2 directory
components below the mount point, is allowed and always was. The refusal
prints the depth that applied.

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

Rendered by walk-blocker 0.2.0 from this node's `site.toml`; the mount table
above is that file's `[[filesystems.mounts]]`, as built.
