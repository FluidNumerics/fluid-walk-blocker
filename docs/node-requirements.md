# What the node must provide

This file lists what the destination node must provide for the payload to
work as documented: the binaries the node code runs, the features of each it
relies on, and the kernel and system interfaces it reads. A fault that
occurs only below a requirement stated here is unsupported, and its issue
is closed by citing the row (ADR-0032).

A requirement comes only from upstream documentation, from a measurement
made on the tool itself, or from an ADR, and the Source column says which.
None comes from a version observed at a site. A row states a version only
where the code relies on a feature with a known first version and there is
a clear need to say it; every other row names the feature and states no
floor. The versions a site observed belong in that site's own repository,
beside its `site.toml`, and only for the rows that state one.

A fallback that runs below a requirement is kept: the pure-`sh` mount reader
and the shim's no-awk record also cover an `awk` that fails or is killed.
What such a fallback does below the floor is tested, not promised.

## The trusted binaries

One row per `[trusted_binaries]` key. Each path is compiled into the node
artifacts, so nothing on the node resolves it through a `PATH` a user
controls.

| Dependency | Feature relied on | Floor | Source | Relied on by |
|---|---|---|---|---|
| `logger` (`[trusted_binaries].logger`) | Accepts `--size`, with `-t`, `-p` and `--`, and writes to journald. A logger that rejects `--size` exits non-zero and sends nothing, so every Layer 1 record is lost. BusyBox `logger` has no `--size` and is unsupported; issue #136 is below this floor. | util-linux 2.27, for `--size` | upstream documentation | `node/shim/guard.sh.in`, `node/shim/install.sh` |
| `awk` (`[trusted_binaries].awk`) | ERE matching, `split`, `ENVIRON`, `printf`, `length`, `substr` and `match` with `RLENGTH`, under `LC_ALL=C`. In a `gsub` replacement, a backslash before `"` stays a literal backslash, so `"\\\""` writes `\"`, and a doubled backslash stays doubled, so `"\\\\"` writes `\\`. BusyBox `awk` writes a bare `"` for the first and is unsupported; issue #137 is below this floor. | none stated | measured on the tool | `node/shim/guard.sh.in` |
| `id` (`[trusted_binaries].id`) | `id -u` prints the numeric uid. | none stated | upstream documentation | `node/shim/guard.sh.in` |
| `date` (`[trusted_binaries].date`) | `-u` with a `+%Y-%m-%dT%H:%M:%SZ` format. | none stated | upstream documentation | `node/shim/guard.sh.in` |
| `timeout` (`[trusted_binaries].timeout`) | `--kill-after=N` before the duration, so a relink that ignores TERM is killed. | none stated | upstream documentation | `node/deploy.py` |
| `sh` (`[trusted_binaries].sh`) | A POSIX shell that runs dash-clean code. Runs `install.sh` for `deploy.py` and for the timer's service. | none stated | ADR-0015 | `node/deploy.py`, `node/shim/install.sh` |
| `python3` (`[trusted_binaries].python3`) | Runs the reaper and `deploy.py`: the 3.9 standard library, with no third-party import. `measure.sh` runs the `python3` on `PATH`, with `-S`, as its floor. | Python 3.9, for the 3.9 standard library | ADR-0015 | `node/deploy.py`, `node/reaper.py`, `node/survey.py`, `node/shim/measure.sh` |
| `bfs` (`[trusted_binaries].bfs`, optional) | When named, `walk-job bfs ...` runs this path on the compute node if it is executable there, with the caller's arguments. | none stated | ADR-0007 | `node/walk-job` |

## Programs called by name through `PATH`

The installer and `deploy.py` run as root, under root's `PATH`; `walk-job`
and the measurement scripts run as the caller.

| Dependency | Feature relied on | Floor | Source | Relied on by |
|---|---|---|---|---|
| `/bin/sh` | The literal interpreter line of the shim, `install.sh`, `walk-job` and the measurement scripts: a POSIX shell that runs dash-clean code. | none stated | ADR-0015 | `node/shim/guard.sh.in`, `node/shim/install.sh`, `node/walk-job`, `node/shim/measure.sh`, `node/shim/measure-flags.sh` |
| coreutils, for the installer | `id -u`; `dirname`; `stat -c` with `%u`, `%a`, `%d:%i`, `%g` and `%F` (GNU); `mktemp` with a template in the target's directory; `chmod` with a four-digit octal mode, `2750` setting setgid; `chgrp`; `mv -f -T` (GNU); `rm -f`; `rmdir`; `ln -sfn`; `install -d -m`; `cat`; `touch`. | none stated | upstream documentation | `node/shim/install.sh` |
| `awk`, on `PATH` | `-v` assignments and line matching, to remove the hook block from a startup file. | none stated | upstream documentation | `node/shim/install.sh` |
| `sed` | `s///g` with a quoted replacement, to quote a value for the shell. In `measure-flags.sh`, `s/./& /g`. | none stated | upstream documentation | `node/shim/install.sh`, `node/walk-job`, `node/shim/measure-flags.sh` |
| `systemctl` | `is-active`; `show --property=UnitFileState --value`; `disable --now`; `stop`; `daemon-reload`; `enable --now`. | none stated | upstream documentation | `node/shim/install.sh`, `node/deploy.py` |
| `bash`, `zsh`, `fish` | `-c "command -v NAME"`, to prove a hook fires for a remote command. Each is probed only where its `[hooks.*]` table enables it. | none stated | ADR-0008 | `node/shim/install.sh` |
| coreutils, for `deploy.py` | `install -d -m` and `install -m`; `cp -a --no-preserve=ownership` (GNU); `rm -f` and `rm -rf`; `chmod` with an octal mode and `chmod -R` with `a-s` and `a+rX,go-w`; `chown -R root:root`. | none stated | upstream documentation | `node/deploy.py` |
| `setfacl` | `setfacl -R -P -x`, to revoke a journal grant. | none stated | upstream documentation | `node/deploy.py` |
| `systemd-tmpfiles` | `--create` on one drop-in, with the `a+` line type and the `%m` specifier. | none stated | upstream documentation | `node/deploy.py` |
| coreutils, for `measure.sh` | `date +%s%N` (GNU, nanoseconds); `sort -n`; `mktemp -d`; `mkdir -p`; `cp`; `ln -sf`; `chmod`; `rm`; `dirname`; `basename`. | none stated | upstream documentation | `node/shim/measure.sh` |
| `awk`, for `measure.sh` | Floating-point arithmetic, and `printf "%.2f"` printing a `.` decimal that the next `awk -v` reads back. gawk honours `LC_NUMERIC` in POSIX mode, so `POSIXLY_CORRECT` must be unset or the numeric locale must be C; issue #131 is below this floor. | none stated | upstream documentation | `node/shim/measure.sh` |
| coreutils, for `measure-flags.sh` | `wc -l`; `tr -d`; `mktemp -d`; `mkdir -p`. | none stated | upstream documentation | `node/shim/measure-flags.sh` |
| `sbatch` | `--parsable`, printing `jobid` or `jobid;cluster`, with `--job-name`, `--partition`, `--qos`, `--account`, `--time`, `--no-requeue`, `--nodes`, `--ntasks`, `--cpus-per-task`, `--mem`, `--output`, `--export` and `--wrap`. Found on `PATH` or under `[slurm].sbatch_glob`. | none stated | upstream documentation | `node/walk-job` |
| `nice`, on the compute node | `nice -n N COMMAND`, in the job `walk-job` submits. | none stated | upstream documentation | `node/walk-job` |

## Kernel and system interfaces

| Dependency | Feature relied on | Floor | Source | Relied on by |
|---|---|---|---|---|
| `/proc/mounts` | Six whitespace-separated fields, with whitespace in a mount point octal-escaped. Read at `[filesystems].mount_table`. | none stated | upstream documentation | `node/shim/guard.sh.in`, `node/shim/install.sh`, `node/reaper.py`, `node/survey.py` |
| `/proc/<pid>/` | `stat`, `cmdline`, `status`, `cwd`, `fd/0` and `cgroup`, readable for other users' processes where the kernel allows. | none stated | upstream documentation | `node/reaper.py` |
| `/proc/uptime`, `/proc/sys/kernel/random/boot_id` | Seconds since boot, and an identifier that changes on each boot, for the reconcile's clocks and memories. | none stated | upstream documentation | `node/shim/install.sh` |
| cgroup v2 with PSI | `io.pressure`, `cpu.pressure` and `cpu.stat` at each `user-*.slice`, under the unified hierarchy with PSI enabled. | none stated | ADR-0002 | `node/reaper.py` |
| journald | Accepts records from `logger` on `/dev/log`, for an unprivileged writer. | none stated | upstream documentation | `node/shim/guard.sh.in`, `node/shim/install.sh` |
| POSIX ACL extended attributes | `system.posix_acl_access` and `system.posix_acl_default`, read with `os.getxattr` in the kernel's on-disk layout. | none stated | upstream documentation | `node/deploy.py` |
| systemd units | A `Type=oneshot` service with a `-`-prefixed `ExecStartPre`, `ExecStart`, `SuccessExitStatus` and `TimeoutStartSec`; a timer with `OnCalendar`, `RandomizedDelaySec`, `AccuracySec` and `Persistent`. | none stated | upstream documentation | `node/deploy.py` |
| `/etc/login.defs` | `UID_MIN`, `UID_MAX` and `GID_MIN`, as `shadow-utils` reads them. | none stated | upstream documentation | `node/deploy.py` |
| NSS | `getgrnam`, `getgrgid`, `getpwnam`, `getpwuid` and `getpwall`, through Python's `grp` and `pwd`. | none stated | upstream documentation | `node/deploy.py` |
