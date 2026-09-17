"""The argv matrix: one table of cases, shared by both consumers.

Data only. `tests/test_rules_table.py` asserts every row against the rule
table; `tests/test_shim_agrees.py` (Milestone 4) asserts the generated shim
returns the same verdict, because the whole design rests on those two
agreeing. A shim that blocks what the reaper does not report, or the reverse,
is worse than either layer alone.

Every path here is a FIXTURE path, described by the fixture mount table in
`tests/conftest.py`: `/scratch` and `/home` are the two mounts of one
parallel-filesystem type (the second carries a per-mount depth allowance of
4), `/archive` is an NFS export, `/` is local and cheap. Nothing here is a
site literal (ADR-0014). Rows that depend on the current directory use the
FAST_CWD / FAST_DEEP sentinels rather than a literal mount path: a shell
derives `$PWD` from its actual working directory, so the shim half can only
be driven from a real directory that the fixture table really describes as
expensive, and conftest resolves the sentinels to one.
"""

# A real directory that is an expensive mount point, and one two components
# below it -- resolved by conftest's `node_fs` fixture.
FAST_CWD = "<FAST_CWD>"
FAST_DEEP = "<FAST_DEEP>"

REFUSE = True
ALLOW = False

# (id, argv, cwd, expected, note)
#
# argv is what the shim receives: bash has already tokenized, expanded globs and
# variables, and substituted. That is the whole reason this layer sits above the
# ceiling described in ADR-0001.
CASES = [
    # --- the incident ------------------------------------------------------
    ("incident-verbatim",
     ["find", "/", "/home", "-type", "f", "-name", "slurm-4242_*.out", "-print"],
     "/home/someone", REFUSE,
     "the command that started this, as the shim sees it"),

    # --- bfs, whose flags are find's superset -------------------------------
    # Seen live at a reference deployment: several processes in this shape,
    # one unbounded at / for days. Every one read as walking the CWD, because
    # the operand scan stopped dead at `-S` and never saw the path.
    ("bfs-live-unbounded-root",
     ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/",
      "-name", "script.py", "-path", "*some_data*"],
     "/home/someone", REFUSE,
     "the live invocation: leading flags before the path, unbounded at /"),
    ("bfs-live-bounded-past-ceiling",
     ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/",
      "-maxdepth", "6", "-name", "processing_model*.py"],
     "/home/someone", REFUSE,
     "bounded, but depth 6 is past every mount's ceiling"),
    ("bfs-live-two-roots",
     ["bfs", "-S", "dfs", "-regextype", "findutils-default",
      "/home/someone", "/scratch", "-maxdepth", "4", "-iname", "Dockerfile*"],
     "/home/someone", REFUSE,
     "depth 4 is allowed on /home and not on /scratch, which is also named"),
    ("bfs-glued-thread-flag",
     ["bfs", "-j8", "/scratch", "-name", "x"],
     "/home/someone", REFUSE, "`-jN` is glued, so it consumes nothing"),
    ("bfs-root-flag",
     ["bfs", "-f", "/scratch", "-name", "x"],
     "/home/someone", REFUSE,
     "`-f PATH` supplies a root; skipping it lost the path and fell to the cwd"),
    # A per-profile clusterable-letters set. `-HL` really is `-H`
    # plus `-L` to bfs (GNU find calls it an unknown predicate and never
    # reaches this profile at all, since find never clusters). Before the
    # fix this was read as one opaque token, /scratch was lost, and the
    # walk was judged against the cwd instead -- a bypass from a cheap cwd.
    ("bfs-clustered-boolean-flags-still-charge-the-root",
     ["bfs", "-HL", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "-HL clusters into -H plus -L; losing the cluster lost /scratch too"),
    ("bfs-device-bound-x",
     ["bfs", "-x", "/", "-name", "x"],
     "/var/tmp", ALLOW, "-x is bfs's spelling of -xdev; it never crosses onto /scratch"),
    ("bfs-regextype-is-not-device-bound",
     ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/", "-name", "x"],
     "/var/tmp", REFUSE,
     "-regextype contains an `x`; it must not be scanned as a cluster now that -x is a device flag"),
    # `-f` is bfs's root_flags entry, never find's -- GNU find
    # rejects it outright in every arrangement (findutils 4.8.0, "unknown
    # predicate `-f'") and walks nothing, so the true verdict is always
    # ALLOW. From a cheap cwd, falling back to the cwd happens to agree.
    ("find-rejects-bfss-root-flag-after-dashdash",
     ["find", "--", "-f", "/scratch", "-name", "x"],
     "/var/tmp", ALLOW,
     "GNU find has no -f; it walks nothing, and the cheap-cwd fallback agrees"),
    ("find-rejects-bfss-root-flag-after-leading-dash",
     ["find", "-", "-f", "/scratch", "-name", "x"],
     "/var/tmp", ALLOW,
     "same shape, `-` first: find still has no -f and still walks nothing"),
    ("find-rejects-bfss-root-flag-fast-cwd-residual",
     ["find", "--", "-f", "/var/tmp", "-name", "x"],
     FAST_CWD, REFUSE,
     "known residual: GNU find walks NOTHING here too, so the "
     "true verdict is ALLOW regardless of cwd -- both consumers instead "
     "fall back to judging the cwd, which is wrong here but at least is "
     "not a DIVERGENCE between them. Modelling '-f makes find refuse to "
     "run' would need a general 'this will not run' mechanism the table "
     "declines to build, since -f is otherwise indistinguishable from "
     "any predicate this table does not track. See test_rules_table.py's "
     "test_leading_profiles_differ_only_in_what_dashdash_means."),
    # From a CHEAP cwd on purpose. With an expensive cwd both readings refuse and
    # the row proves nothing: the fallback the bug reached for is itself
    # expensive. /var/tmp is where "charged /scratch" and "fell back to the
    # cwd" give opposite verdicts.
    ("bfs-glued-flag-before-root-flag",
     ["bfs", "-O3", "-f", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "`-O3` is matched by prefix; ending the scan there lost -f's path"),
    ("bfs-attached-root-flag",
     ["bfs", "-f=/tmp/cheap", "-name", "x"],
     FAST_CWD, REFUSE,
     "bfs rejects the attached spelling, so it names no root and the cwd is"
     " the walk"),
    ("bfs-extended-regex-flag",
     ["bfs", "-E", "/scratch", "-regex", ".*x"],
     "/home/someone", REFUSE, "`-E` consumes nothing"),
    ("bfs-leading-flags-cheap-root",
     ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/var/log",
      "-name", "x"],
     "/home/someone", ALLOW,
     "the flags are read, so a cheap root is still a cheap root"),

    # --- every row of ADR-0001's table, post-expansion ----------------------
    ("adr-find-xargs-rm",
     ["find", "/scratch", "-name", "core", "-print"],
     "/home/someone", REFUSE, "`find ... | xargs rm`, the find half"),
    ("adr-find-head",
     ["find", "/", "/home", "-name", "x", "-print"],
     "/home/someone", REFUSE, "`find ... | head -500`, the transcript shape"),
    ("adr-cd-then-find",
     ["find", ".", "-name", "x"],
     FAST_CWD, REFUSE, "`cd /scratch && find . -name x`; cwd is the root"),
    ("adr-xargs-I-substituted",
     ["find", "/scratch/shard0", "-name", "x"],
     "/home/someone", REFUSE,
     "`xargs -I{} find {}` execs with {} already replaced by a real path"),
    ("adr-ssh-remote",
     ["find", "/", "/home", "-type", "f", "-name", "slurm-4242_*.out", "-print"],
     "/home/someone", REFUSE,
     "THE ROW THE TEXT GUARD MISSES: over ssh the shim still gets plain argv"),
    ("adr-nested-find-inner",
     ["find", "/scratch/runs", "-name", "x"],
     "/home/someone", REFUSE,
     "inner find of `find -maxdepth 1 | xargs -I{} find {}`, root one level in"),
    ("adr-xargs-P16-child",
     ["find", "/scratch/a/b", "-maxdepth", "2", "-name", "x"],
     "/home/someone", ALLOW,
     "one of sixteen individually-bounded children; the reaper calls the "
     "aggregate fanout_traversal, the shim cannot see it"),
    ("adr-grep-fast",
     ["grep", "-rIn", "pat", "/scratch"],
     "/home/someone", REFUSE, "recursive grep, the shape with no depth flag"),
    ("adr-rg-fast",
     ["rg", "pat", "/scratch"],
     "/home/someone", REFUSE, "rg walks by default"),
    ("adr-fd-fast",
     ["fd", "pat", "/scratch"],
     "/home/someone", REFUSE, "fd walks by default"),
    ("adr-du-fast",
     ["du", "-sh", "/scratch"],
     "/home/someone", REFUSE, "du walks metadata as hard as find does"),

    # --- shapes sampled on the tool ----------------------------------------
    ("observed-grep-rooted-at-slash",
     ["grep", "-rhoE", "pattern", "/"],
     "/home/someone", REFUSE, "recursive grep rooted at /, the costliest shape"),
    ("observed-grep-home-subtree",
     ["grep", "-rIn", "pat", "/home/someone"],
     "/home/someone", REFUSE,
     "unbounded grep over one home; a user's home on a parallel filesystem is not a bound"),

    # --- the fast path, which must never be paid for -----------------------
    ("fastpath-ps-grep",
     ["grep", "sshd"], "/home/someone", ALLOW, "`ps aux | grep sshd`"),
    ("fastpath-grep-file",
     ["grep", "-n", "pat", "/scratch/one/file.txt"],
     "/home/someone", ALLOW, "non-recursive grep of a named file on the expensive mount"),
    ("fastpath-egrep",
     ["egrep", "pat"], "/home/someone", ALLOW, "no -r, no walk"),

    # --- bounded walks are the point, not collateral -----------------------
    ("bounded-maxdepth-2",
     ["find", "/scratch", "-maxdepth", "2", "-name", "x"],
     "/home/someone", ALLOW, "explicitly bounded at the threshold"),
    ("bounded-maxdepth-1",
     ["find", "/", "-maxdepth", "1"],
     "/home/someone", ALLOW, "shallower still"),
    ("unbounded-maxdepth-9",
     ["find", "/scratch", "-maxdepth", "9", "-name", "x"],
     "/home/someone", REFUSE, "a bound above the threshold is not a bound"),
    ("bounded-fd-short-attached",
     ["fd", "-d2", "pat", "/scratch"],
     "/home/someone", ALLOW, "`-d2`, value attached to a short flag"),
    ("bounded-rg-equals",
     ["rg", "--max-depth=1", "pat", "/scratch"],
     "/home/someone", ALLOW, "`--flag=value` form"),
    ("bounded-tree-L",
     ["tree", "-L", "2", "/scratch"],
     "/home/someone", ALLOW, "tree bounds with -L"),
    # du is NOT here as a bounded row, deliberately: `-d` prunes du's
    # OUTPUT, not its walk, so this enumerates the whole mount (ADR-0007).
    ("du-d-is-not-a-bound",
     ["du", "-d", "1", "-h", "/scratch"],
     "/home/someone", REFUSE, "-d prunes du's output, not the walk"),

    # --- deep enough to be scoped ------------------------------------------
    ("scoped-deep-root",
     ["find", "/scratch/runs/set-01", "-name", "x"],
     "/home/someone", ALLOW,
     "two components below the mount counts as scoped work"),
    ("scoped-home-subdir",
     ["grep", "-rIn", "pat", "/home/someone/proj/repo"],
     "/home/someone", ALLOW, "a project directory, not a filesystem"),

    # --- cheap filesystems are none of our business ------------------------
    ("cheap-ext4",
     ["find", "/var/log", "-name", "*.log"],
     "/home/someone", ALLOW, "/ is ext4 and cheap to walk"),
    ("cheap-ext4-root-of-slash",
     ["du", "-sh", "/var/tmp"],
     "/var/tmp", ALLOW, "local disk, no opinion"),

    # --- nfs4 is expensive too, just less so -------------------------------
    ("nfs4-share",
     ["find", "/archive", "-name", "x"],
     "/home/someone", REFUSE, "/archive is nfs4"),

    # --- pattern-flag grammar ----------------------------------------------
    ("pattern-flag-frees-positional",
     ["grep", "-r", "-e", "pat", "/scratch"],
     "/home/someone", REFUSE,
     "-e supplies the pattern, so /scratch is a root, not the pattern"),
    ("lone-positional-is-pattern",
     ["fd", "somepattern"],
     FAST_DEEP, ALLOW,
     "`fd PATTERN` walks the cwd, which here is deep enough to be scoped"),
    ("lone-positional-pattern-on-fast",
     ["fd", "somepattern"],
     FAST_CWD, REFUSE, "same shape, cwd at the mount point"),

    # --- clustered short options -------------------------------------------
    ("clustered-rIn",
     ["grep", "-rIn", "pat", "/scratch"],
     "/home/someone", REFUSE, "-rIn is one token"),
    ("clustered-rhoE",
     ["grep", "-rhoE", "pat", "/home"],
     "/home/someone", REFUSE, "the flag cluster the recurring shape uses"),
    ("long-flag-not-a-cluster",
     ["grep", "--color", "pat"],
     "/home/someone", ALLOW,
     "--color contains 'r' but is not a clustered short option"),

    # --- a bound by DEVICE, not by depth ------------------------------------
    # Depth was the only bound this table recognised, so the standard way of
    # steering a walk AWAY from an expensive filesystem was invisible to it.
    # Measured on the tools against a local directory holding two network
    # mounts (one parallel filesystem, one NFS export):
    #   find DIR -xdev            DIR and the two mount points, no descent
    #   find DIR -mount           the same, so -mount is a real synonym
    #   du -x -d1 DIR             DIR alone
    #   du --one-file-system -d1  the same
    #   rg --files --one-file-system DIR        (nothing)
    #   fd --one-file-system . DIR              the two mount points
    #   tree -x -L 2 DIR          2 directories, entering neither
    # In the fixture table the cheap parent of every expensive mount is `/`,
    # so that is the root these rows walk from.
    # The cost of the false refusal is not the individual case: a user refused
    # for `find / -xdev` learns the guard is wrong about their command, and
    # the durable fix they adopt is `/usr/bin/find` in an alias -- after which
    # Layer 1 is off for them on every command, silently.
    ("device-bound-find-xdev",
     ["find", "/", "-xdev", "-name", "x"],
     "/var/tmp", ALLOW, "-xdev never crosses onto /scratch or /home"),
    ("device-bound-find-mount-synonym",
     ["find", "/", "-mount", "-name", "x"],
     "/var/tmp", ALLOW, "-mount is the same option under another name"),
    ("device-bound-find-on-fast-still-refused",
     ["find", "/scratch", "-xdev", "-name", "x"],
     "/var/tmp", REFUSE,
     "a walk that STARTS on the expensive mount still enumerates all of it; -xdev answers the "
     "other reason, not this one"),
    ("device-bound-find-home-still-refused",
     ["find", "/home", "-xdev", "-name", "x"],
     "/var/tmp", REFUSE, "...and one home is not a bound either"),
    ("device-bound-one-root-of-two-is-on-fast",
     ["find", "/", "/scratch", "-xdev", "-name", "x"],
     "/var/tmp", REFUSE,
     "the bound is judged per root, and the second root starts on the expensive mount"),
    ("device-bound-du-x",
     ["du", "-x", "/"],
     "/var/tmp", ALLOW, "the canonical `what filled the root disk`"),
    ("device-bound-du-long-spelling",
     ["du", "--one-file-system", "-h", "/"],
     "/var/tmp", ALLOW, "same option spelled long"),
    ("device-bound-du-abbreviated",
     ["du", "--one-file-s", "/"],
     "/var/tmp", ALLOW, "du is getopt_long, and accepts the prefix"),
    ("device-bound-du-in-a-cluster",
     ["du", "-sx", "/"],
     "/var/tmp", ALLOW, "`-sx` is the spelling people actually type"),
    ("device-bound-du-cluster-carrying-a-depth-too",
     ["du", "-xd9", "/"],
     "/var/tmp", ALLOW,
     "the depth is above the threshold and bounds nothing; the -x still does"),
    ("device-bound-du-on-fast-still-refused",
     ["du", "-sx", "/scratch"],
     "/var/tmp", REFUSE, "...and starting on the expensive mount is still starting on the expensive mount"),
    ("device-bound-rg",
     ["rg", "--one-file-system", "pat", "/"],
     "/var/tmp", ALLOW, "rg's own help calls it find's -xdev"),
    ("device-bound-rg-negated-last-wins",
     ["rg", "--one-file-system", "--no-one-file-system", "pat", "/"],
     "/var/tmp", REFUSE,
     "measured: with the negation last, rg descended into /archive"),
    ("device-bound-rg-negated-first-loses",
     ["rg", "--no-one-file-system", "--one-file-system", "pat", "/"],
     "/var/tmp", ALLOW, "...and reversed, it stayed put"),
    ("device-bound-rg-abbreviation-is-not-one",
     ["rg", "--one-file", "pat", "/"],
     "/var/tmp", REFUSE,
     "rg is clap and rejects prefixes, so honouring this would model a "
     "grammar the tool does not have"),
    ("device-bound-fd",
     ["fd", "--one-file-system", "pat", "/"],
     "/var/tmp", ALLOW, "fd 8.3.1 has the long spelling only"),
    ("fd-short-x-is-exec-not-a-device-bound",
     ["fd", "-x", "echo", "{}", ";", "pat", "/"],
     "/var/tmp", REFUSE,
     "fd's -x is --exec; reading it as du's -x would allow a full walk"),
    ("device-bound-tree",
     ["tree", "-x", "/"],
     "/var/tmp", ALLOW, "tree stays on the current filesystem"),
    ("device-bound-tree-cluster",
     ["tree", "-xL", "9", "/"],
     "/var/tmp", ALLOW, "`-xL 9` clusters, and the depth 9 bounds nothing"),

    # ...and the device flag obeys the same grammar as every other scan.
    ("device-bound-after-dashdash-is-a-pattern",
     ["rg", "--", "--one-file-system", "/"],
     "/var/tmp", REFUSE, "past `--` it is the PATTERN, so it bounds nothing"),
    ("device-bound-before-a-dashdash-still-counts",
     ["find", "--", "/", "-xdev", "-name", "x"],
     "/var/tmp", ALLOW,
     "find's predicates follow its operands and stay active across `--`"),
    ("device-flag-as-another-flags-value",
     ["du", "--exclude", "-x", "/"],
     "/var/tmp", REFUSE, "`-x` is --exclude's pattern, not a bound"),
    ("device-flag-inside-an-exec-body-is-data",
     ["find", "/", "-exec", "echo", "-xdev", ";"],
     "/var/tmp", REFUSE,
     "the -xdev is an argument to echo -- find(1) printed it once per entry"),

    # The COLLISION spellings, not the clean ones. A device flag consumed as
    # another flag's value is that flag's value: `rg -e --one-file-system /`
    # searches for the literal string `--one-file-system`, so nothing bounds
    # the walk. A scanner that matched device flags without honouring a
    # pending value would read it as bounded -- the same shape as an earlier review's
    # `rg -ne --max-depth 1`, which is why it gets a row rather than an
    # assumption.
    ("device-flag-eaten-by-a-pattern-flag-is-not-a-bound",
     ["rg", "-e", "--one-file-system", "/"],
     "/var/tmp", REFUSE, "the flag is the PATTERN; nothing bounds this"),
    ("device-flag-eaten-by-a-value-flag-cannot-turn-the-bound-off",
     ["rg", "--one-file-system", "-e", "--no-one-file-system", "pat", "/"],
     "/var/tmp", ALLOW,
     "the inverse: the negation is -e's pattern, so the real bound stands"),
    ("device-flag-eaten-by-an-exclude-is-not-a-second-bound",
     ["du", "-x", "--exclude", "-x", "/"],
     "/var/tmp", ALLOW, "one real -x, one that is --exclude's pattern"),
    ("device-flag-restated",
     ["du", "-x", "-x", "/"],
     "/var/tmp", ALLOW,
     "restating a setting changes nothing -- the repeated-occurrence row a "
     "last-wins scan needs"),
    ("device-letter-glued-as-a-value-is-not-a-flag",
     ["du", "-Bx", "/"],
     "/var/tmp", REFUSE, "`-Bx` is --block-size `x`, not -B plus -x"),
    ("device-letter-after-a-value-taking-letter-is-not-a-flag",
     ["tree", "-Lx", "2", "/"],
     "/var/tmp", ALLOW,
     "`-Lx` is -L with level `x`; the x is not a device flag, AND a level "
     "tree rejects outright means tree walks nothing, so "
     "the corrected verdict is ALLOW, not merely `not device-bound`"),

    # ...and the at_or_near_root inverse for the three tools whose device
    # bound is not otherwise paired with one.
    ("device-bound-rg-on-fast-still-refused",
     ["rg", "--one-file-system", "pat", "/scratch"],
     "/var/tmp", REFUSE, "a walk that starts on the expensive mount stays refused"),
    ("device-bound-fd-on-fast-still-refused",
     ["fd", "--one-file-system", "pat", "/home"],
     "/var/tmp", REFUSE, "...and one home is not a bound"),
    ("device-bound-tree-on-fast-still-refused",
     ["tree", "-x", "/scratch"],
     "/var/tmp", REFUSE, "...and tree is no different"),

    # --- end of options -----------------------------------------------------
    # `--` ends the options, not the arguments. The shim used to read it as
    # end-of-arguments: it dropped every operand after it and fell back to the
    # cwd, so each REFUSE row here was a two-character bypass of Layer 1 that
    # the reaper went on reporting -- the "worse than either layer alone" case.
    ("end-of-options-grep",
     ["grep", "-r", "--", "pat", "/home"],
     "/var/tmp", REFUSE, "the operand after `--` is still a root"),
    ("end-of-options-fd",
     ["fd", "--", "pat", "/home"],
     "/var/tmp", REFUSE, "same, for a tool whose first positional is the pattern"),
    ("end-of-options-find",
     ["find", "--", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "findutils accepts `--`; it precedes the operands rather than ending them"),
    ("end-of-options-fake-depth-flag",
     ["rg", "--", "--max-depth", "1", "/home"],
     "/var/tmp", REFUSE,
     "`--max-depth` after `--` is the PATTERN, so it bounds nothing"),
    ("end-of-options-literal-dash-r",
     ["grep", "--", "-r", "/scratch"],
     "/var/tmp", ALLOW,
     "`-r` after `--` is the pattern: this reads /scratch as a file, no walk"),

    # --- a bare `-` is a path operand, not an expression token --------------
    # Measured: with a real directory named `-` present, `find - -name x` and
    # `bfs - -name x` both walk it and exit 0. With no such file they error on
    # the dash and walk every REMAINING operand anyway, so `find d1 - d2`
    # walks BOTH d1 and d2. Leading mode used to end the operand list here and
    # throw the rest away -- hiding a NAMED expensive path from Layer 1 at
    # every cwd, and from the reaper entirely, since the argv parses and the
    # charged root is cheap.  Unlike an empty operand, `-` is
    # CHARGED: it is a path that may exist, and whether `./-` does is not
    # asked, exactly as for `find runs`.
    ("bare-dash-then-a-root-find",
     ["find", "-", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "the operand after `-` is still a root; find walks it and errors on `-`"),
    ("bare-dash-mid-operand-list-find",
     ["find", "/tmp/cheap", "-", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "a `-` between two operands truncated the list and lost /scratch"),
    ("bare-dash-mid-operand-list-bfs",
     ["bfs", "/tmp/cheap", "-", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "same, for the other leading-mode tool"),
    ("bare-dash-does-not-over-refuse",
     ["find", "-", "/tmp/cheap", "-name", "x"],
     "/var/tmp", ALLOW,
     "`-` resolves against a cheap cwd, so charging it refuses nothing"),
    ("bare-dash-with-an-empty-operand-and-a-root-flag",
     ["bfs", "", "-", "-f", "/scratch", "-name", "x"],
     FAST_CWD, REFUSE,
     "empty charges no root, `-` charges the cwd, and -f's value is still a root"),
    ("bare-dash-then-an-empty-root-flag-value",
     ["bfs", "-", "-f", "", "/scratch", "-name", "x"],
     FAST_CWD, REFUSE,
     "an empty -f value walks nothing, but the positional after it is a root"),

    # A `--` consumed BY a flag is that flag's value, not a marker. Verified
    # against the real tools: `grep -e -- -r DIR` descends, and
    # `find -- DIR -maxdepth 1` stays at one level.
    ("dashdash-as-a-pattern-value",
     ["grep", "-e", "--", "-r", "/scratch"],
     "/var/tmp", REFUSE,
     "pattern is `--`, so the -r after it is a REAL recursive flag"),
    ("dashdash-as-a-pattern-value-long",
     ["grep", "--regexp", "--", "--recursive", "/home"],
     "/var/tmp", REFUSE, "same, spelled long"),
    ("dashdash-as-a-value-then-real-bound",
     ["rg", "-e", "--", "--max-depth", "1", "/home"],
     "/var/tmp", ALLOW,
     "pattern is `--`; --max-depth 1 after it is a REAL bound, not a pattern"),
    ("find-dashdash-then-maxdepth",
     ["find", "--", "/scratch", "-maxdepth", "1", "-name", "x"],
     "/var/tmp", ALLOW,
     "find's predicates follow its operands and stay active across `--`"),
    ("find-dashdash-then-deep-maxdepth",
     ["find", "--", "/scratch", "-maxdepth", "9", "-name", "x"],
     "/var/tmp", REFUSE, "...and are still read, so a deep bound is no bound"),

    # --- optional-argument flags --------------------------------------------
    # grep spells it `--color[=WHEN]`: the argument is OPTIONAL and is never
    # the following token. Listing it as value-consuming made the
    # pending-value rule swallow the next element, so these two read as
    # non-recursive and took the fast path -- a Layer 1 bypass, verified
    # against grep(1), which descends.
    ("optional-arg-color-then-recursive",
     ["grep", "--color", "-r", "pat", "/home"],
     "/var/tmp", REFUSE, "--color takes no following token, so -r is live"),
    ("optional-arg-color-equals-then-recursive",
     ["grep", "--colour=auto", "-rIn", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the =WHEN form, with a clustered -rIn after it"),
    ("optional-arg-color-non-recursive",
     ["grep", "--color", "pat", "/scratch/one/file.txt"],
     "/var/tmp", ALLOW, "still no walk when nothing asked for one"),
    ("required-arg-color-rg",
     ["rg", "--color", "never", "pat", "/scratch/a/b"],
     "/var/tmp", ALLOW,
     "rg's --color DOES take a value, so `never` is not a root"),

    # --- attached pattern arguments -----------------------------------------
    # A pattern flag can carry its pattern inline. Matching pat_flags only as
    # exact tokens left `saw_pattern_flag` false, so the first PATH operand
    # was charged against skip_pos as if it were the pattern -- these lost
    # their root and fell back to a cheap cwd. Both consumers agreed, wrongly.
    ("attached-pattern-long-equals",
     ["grep", "--regexp=pat", "-r", "--", "/home"],
     "/var/tmp", REFUSE, "`--regexp=pat` supplies the pattern; /home is a root"),
    ("attached-pattern-short-glued",
     ["grep", "-epat", "-r", "/home"],
     "/var/tmp", REFUSE, "`-epat` likewise"),
    ("attached-pattern-rg-file",
     ["rg", "--file=/tmp/pats", "/scratch"],
     "/var/tmp", REFUSE, "-f/--file is a pattern flag too"),
    ("long-flag-is-not-an-attached-short",
     ["grep", "--exclude-dir=.git", "-r", "pat", "/home/someone/proj"],
     "/var/tmp", ALLOW,
     "an attached VALUE flag is not a pattern flag; the positional still is"),

    # --- an attached pattern is not a cluster of option letters --------------
    # An earlier review taught roots() about `-epat` but left the fast traversal scan
    # matching `-*[rR]*`, so the `r` INSIDE `-eerror` read as -r and a command
    # that walks nothing was refused. Both consumers agreed on the false
    # refusal, which is how an advisory layer teaches people to alias past it.
    ("attached-pattern-containing-r",
     ["grep", "-eroot", "/home"],
     "/var/tmp", ALLOW, "pattern is `root`; not recursive, /home read as a file"),
    ("attached-pattern-containing-r-long",
     ["grep", "--regexp=recurse", "/scratch/f"],
     "/var/tmp", ALLOW, "same, and the long form has an r in it too"),
    ("attached-pattern-with-a-real-r-flag",
     ["grep", "-eroot", "-r", "/home"],
     "/var/tmp", REFUSE, "a real -r after the attached pattern still counts"),

    # --- a flag whose VALUE turns the walk on --------------------------------
    # `grep -d recurse` is `-r` spelled as an action, verified against grep(1).
    # -d is value-consuming, so the generic value skip discarded `recurse` and
    # the invocation read as non-recursive: a Layer 1 bypass in three
    # spellings. `-d skip` and `-d read` must stay allowed.
    ("directories-recurse-separate",
     ["grep", "-d", "recurse", "pat", "/home"],
     "/var/tmp", REFUSE, "`-d recurse` walks exactly like -r"),
    ("directories-recurse-attached-long",
     ["grep", "--directories=recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the attached long spelling"),
    ("directories-recurse-glued-short",
     ["grep", "-drecurse", "pat", "/home"],
     "/var/tmp", REFUSE, "the glued short spelling"),
    ("directories-skip-is-not-recursive",
     ["grep", "-d", "skip", "pat", "/scratch"],
     "/var/tmp", ALLOW, "any other action walks nothing"),
    ("directories-read-is-not-recursive",
     ["grep", "--directories=read", "pat", "/home"],
     "/var/tmp", ALLOW, "same, attached"),

    # --- a glued short value is one option, not a cluster --------------------
    # `-d` takes a REQUIRED argument, so `-dread` is one option plus one
    # value. Reading it as a cluster found the `r` in `read` and refused a
    # command that does not descend -- verified against grep(1), which reports
    # "is a directory" for -dread and descends only for -drecurse. `-dskip`
    # escaped only because "skip" contains no r, which is why the fix is
    # general rather than a special case for this value.
    ("glued-short-value-containing-r",
     ["grep", "-dread", "pat", "/home"],
     "/var/tmp", ALLOW, "the r is in the VALUE `read`, not a flag"),
    ("glued-short-value-without-r",
     ["grep", "-dskip", "pat", "/scratch"],
     "/var/tmp", ALLOW, "same shape, and it passed before only by luck"),
    ("glued-short-value-that-does-recurse",
     ["grep", "-drecurse", "pat", "/home"],
     "/var/tmp", REFUSE, "...while the one value that DOES walk still refuses"),
    ("glued-short-value-max-count",
     ["grep", "-m5", "-r", "pat", "/home"],
     "/var/tmp", REFUSE, "a glued value must not mask a later real -r"),

    # --- a pattern flag inside a cluster ------------------------------------
    # getopt scans a short cluster left to right; the first letter that takes
    # an argument consumes the rest of the token, or the next element. So
    # `-nefoo` is `-n` plus `-e foo` -- verified against grep(1), which
    # matched `foo` recursively. Matching pattern flags only at the START of a
    # token missed this, /home was charged against skip_pos, and the walk was
    # allowed from a cheap cwd.
    ("cluster-pattern-flag-glued",
     ["grep", "-nefoo", "-r", "/home"],
     "/var/tmp", REFUSE, "-e takes `foo` mid-cluster; /home is still a root"),
    ("cluster-pattern-flag-separate",
     ["grep", "-ne", "foo", "-r", "/scratch"],
     "/var/tmp", REFUSE, "same, with the pattern in the next element"),
    ("cluster-pattern-flag-no-recursion",
     ["grep", "-nefoo", "/scratch/one/file.txt"],
     "/var/tmp", ALLOW, "...and without a -r it still walks nothing"),
    ("cluster-value-flag-swallows-r",
     ["grep", "-er", "/scratch/one/file.txt"],
     "/var/tmp", ALLOW, "`-er` is -e with pattern `r`, not -e plus -r"),

    # --- a cluster's pending value is not a depth bound ---------------------
    # `rg -ne --max-depth 1 /home` is `-n` plus `-e --max-depth`: the pattern
    # is the literal string `--max-depth`, and rg(1) proves it by reporting
    # `1: No such file or directory` -- it had taken `1` as a PATH. So the
    # walk is unbounded, while the bounded scan read `--max-depth 1` as a
    # bound and allowed it.
    ("cluster-value-eats-the-depth-flag",
     ["rg", "-ne", "--max-depth", "1", "/home"],
     "/var/tmp", REFUSE, "the depth flag is the PATTERN, so nothing bounds it"),
    ("cluster-value-eats-the-depth-flag-grep",
     ["grep", "-ne", "--include", "-r", "/scratch"],
     "/var/tmp", REFUSE, "same shape where -r still follows"),
    ("real-depth-bound-survives-the-fix",
     ["rg", "--max-depth", "1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "an actual bound must still be honoured"),
    # fd, not du: du's `-d` stopped being a bound when du lost its depth flag. Verified against fd
    # on the tool -- `fd -d2 . top` returned the depth-2 entries.
    ("real-depth-bound-attached-survives",
     ["fd", "-d2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "...including the attached short spellings"),

    # --- but bounded() must NOT resolve abbreviations -----------------------
    # An earlier review asked for the getopt_long treatment here too, on the strength
    # of `rg --rege --max-depth 1 /home`. rg is clap and rejects `--rege`
    # outright, and the only wrapped tool that both abbreviates and has a
    # depth flag is du, whose abbreviatable value flags all validate their
    # values. Applying it regressed this row: `--time` is du's OWN
    # optional-argument option, but resolve_long_flag sees only a unique
    # prefix of `--time-style` and would skip the real bound.
    ("abbrev-prefix-of-a-value-flag-is-not-one",
     ["du", "--time", "--max-depth", "1", "/tmp/cheap"],
     FAST_CWD, ALLOW,
     "--time consumes nothing, so --max-depth eats its own 1 and /tmp/cheap"
     " is the only root; charging 1 would refuse against the expensive cwd"),
    # ...but a REAL abbreviation does consume its value, and that was a
    # bypass. Measured on the tool:
    #   du --time-s --max-depth 1 du42  ->  du42/a/b/c/d  (four levels: the
    #       bound became --time-style's string, which du does NOT validate,
    #       unlike --threshold or --block-size)
    #   du --exclude-f --max-depth 1 du42  ->  "--max-depth: No such file"
    #       (consumed as a filename, and du exits)
    # An earlier review refuted this fix and was right THEN; exact_flags, added in
    # an earlier review for a different finding, is what retired the objection.
    ("abbrev-value-flag-eats-the-depth-bound",
     ["du", "--time-s", "--max-depth", "1", "/scratch"],
     "/var/tmp", REFUSE, "--time-style ate the bound and du walked unbounded"),
    ("abbrev-value-flag-attached-leaves-the-bound",
     ["du", "--time-s=full-iso", "--max-depth", "1", "/tmp/cheap"],
     FAST_CWD, ALLOW, "the inverse: an attached value consumes nothing"),
    ("abbrev-value-flag-eats-the-bound-exclude-from",
     ["du", "--exclude-f", "--max-depth", "1", "/scratch"],
     "/var/tmp", REFUSE, "same shape via --exclude-from"),
    ("abbrev-prefix-still-refused-when-truly-unbounded",
     ["du", "--time", "/scratch"],
     "/var/tmp", REFUSE, "...and without a bound it is still a full walk"),
    # grep is the family that really does abbreviate, and both directions
    # are checked against grep(1) on the tool: `grep --regex -r sgp` says
    # "sgp: Is a directory" -- the -r became the PATTERN, so there is no
    # walk -- while a second -r survives and descends.
    ("abbrev-value-flag-eats-the-only-r",
     ["grep", "--regex", "-r", "/scratch"],
     "/var/tmp", ALLOW, "the -r is the pattern; grep(1) does not descend"),
    ("abbrev-value-flag-leaves-the-second-r",
     ["grep", "--regex", "-r", "-r", "/scratch"],
     "/var/tmp", REFUSE, "...but the second -r survives and it does"),

    # --- an EXACT option is not an abbreviation of a longer one -------------
    # getopt_long matches exactly before considering prefixes, and two of
    # these collide with a value flag in the table. Both were live defects.
    # `--binary` is a real no-argument grep option and only a prefix of
    # `--binary-files`; skipping its "value" hid the -r and grep(1) recursed,
    # matching sgp/a/b/c/deep.txt.
    ("exact-option-is-not-an-abbreviation-grep",
     ["grep", "--binary", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "--binary consumes nothing, so the -r is live"),
    ("exact-option-longer-form-does-consume",
     ["grep", "--binary-files", "-r", "needle", "/scratch"],
     "/var/tmp", ALLOW,
     "the full --binary-files DOES take the -r as its TYPE; grep(1) then "
     "says 'unknown binary-files type' and walks nothing"),
    # du's `--time` is spelled `--time[=WORD]` -- an optional argument, so it
    # never consumes the following token -- and is only a prefix of
    # `--time-style`. Same class as grep's --color.
    ("exact-option-is-not-an-abbreviation-du",
     ["du", "--time", "--max-depth", "1", "/tmp/cheap"],
     FAST_CWD, ALLOW, "same rule, read through the operand scan rather than"
     " the depth scan: a swallowed --max-depth leaves 1 as a root"),

    # --- find's -exec body is data, not predicates -------------------------
    # `find /scratch -exec echo visited -maxdepth 1 ';'` walks the whole
    # tree: find(1) printed "visited -maxdepth 1" once per entry at every
    # depth, so the -maxdepth was an argument to echo.
    ("exec-body-maxdepth-is-not-a-bound",
     ["find", "/scratch", "-exec", "echo", "visited", "-maxdepth", "1", ";"],
     "/var/tmp", REFUSE, "the -maxdepth is an argument to echo"),
    ("exec-body-maxdepth-is-not-a-bound-plus",
     ["find", "/scratch", "-execdir", "echo", "-maxdepth", "1", "+"],
     "/var/tmp", REFUSE, "...same for -execdir and the + terminator"),
    ("real-bound-before-exec-survives",
     ["find", "/scratch", "-maxdepth", "1", "-exec", "echo", "x", ";"],
     "/var/tmp", ALLOW, "a bound BEFORE the -exec is still a bound"),
    ("real-bound-after-exec-terminator-survives",
     ["find", "/scratch", "-exec", "echo", "x", ";", "-maxdepth", "1"],
     "/var/tmp", ALLOW,
     "and after the terminator it is a predicate again -- find(1) applied "
     "it, printing one line per top-level entry"),

    # --- `+` terminates ONLY the `-exec/-execdir ... {} +` form ------------
    # Treating every `+` as a terminator was itself a bypass, and all four
    # rules below were checked against find(1) on the tool:
    #   `find sgp -exec echo + -maxdepth 1 ';'`  -> "+ -maxdepth 1" once per
    #       entry at EVERY depth, so the + and the bound were echo's data
    #   `find sgp -maxdepth 1 -exec echo x +`    -> "missing argument to
    #       `-exec'", so a bare + is not a terminator
    #   `find sgp -maxdepth 1 -ok echo {} +`     -> "missing argument to
    #       `-ok'", so -ok/-okdir accept no + at all
    #   `find sgp -maxdepth 1 -exec echo {} +`   -> "sgp sgp/a", bounded
    ("literal-plus-in-an-exec-body-is-not-a-terminator",
     ["find", "/scratch", "-exec", "echo", "+", "-maxdepth", "1", ";"],
     "/var/tmp", REFUSE, "the + is an argument to echo; the walk is full"),
    ("plus-without-braces-is-not-a-terminator",
     ["find", "/scratch", "-exec", "echo", "x", "+", "-maxdepth", "1", ";"],
     "/var/tmp", REFUSE, "only `{} +` terminates, so this is all echo data"),
    ("ok-does-not-accept-a-plus-terminator",
     ["find", "/scratch", "-ok", "echo", "{}", "+", "-maxdepth", "1", ";"],
     "/var/tmp", REFUSE,
     "-ok/-okdir support only `;`, and find(1) prompted at every depth"),
    ("valid-braces-plus-terminator-with-no-bound",
     ["find", "/scratch", "-exec", "echo", "{}", "+"],
     "/var/tmp", REFUSE, "a valid terminator, but nothing bounds the walk"),
    ("valid-braces-plus-terminator-after-a-bound",
     ["find", "/scratch", "-maxdepth", "1", "-exec", "echo", "{}", "+"],
     "/var/tmp", ALLOW, "find(1) listed only `sgp sgp/a`, so it is bounded"),
    ("bound-after-a-valid-braces-plus-terminator",
     ["find", "/scratch", "-exec", "echo", "{}", "+", "-maxdepth", "1"],
     "/var/tmp", ALLOW,
     "past a REAL terminator the predicates resume; find(1) listed only "
     "`sgp sgp/a`"),

    # --- an abbreviated rec-VALUE flag consumes its value in the ROOT scan --
    # `grep --di recurse hay` is `--directories recurse`, so grep recurses the
    # CWD and `hay` is the pattern -- verified on the tool, where it matched
    # deep/deeper/f.txt from the cwd. Reading `recurse` as the pattern and the
    # operand as a path charged the wrong root, and from a expensive cwd with a
    # cheap-looking operand that ALLOWED an unbounded walk.
    ("abbrev-recvalue-charges-the-cwd-not-the-operand",
     ["grep", "--di", "recurse", "/tmp/cheap"],
     FAST_CWD, REFUSE, "the operand is the PATTERN; the cwd is the root"),
    ("abbrev-recvalue-from-a-cheap-cwd-is-fine",
     ["grep", "--di", "recurse", "hay"],
     "/var/tmp", ALLOW, "...and the same command on ext4 walks something cheap"),
    ("abbrev-recvalue-attached-spelling",
     ["grep", "--dir=recurse", "hay"],
     FAST_CWD, REFUSE, "`--dir=recurse` is the same walk, attached"),

    # --- abbreviation is gated to the tools that actually do it ------------
    # Only GNU getopt_long accepts a unique prefix. `grep --rege hay DIR` is
    # `--regexp=hay` with DIR as a FILE operand and no -r, so grep(1) says
    # "Is a directory" and walks nothing -- which is why this is ALLOW and not
    # a bypass.
    ("abbrev-pattern-flag-without-r-walks-nothing",
     ["grep", "--rege", "hay", "/scratch"],
     "/var/tmp", ALLOW, "no -r, so grep(1) reports 'Is a directory'"),
    ("abbrev-pattern-flag-with-r-still-refused",
     ["grep", "--rege", "hay", "-r", "/scratch"],
     "/var/tmp", REFUSE, "...but with a live -r it is the incident shape"),

    # --- every required-value long option must be in the table -------------
    # `grep --group-separator -- -r needle DIR`: the first `--` is the
    # SEPARATOR's value, so -r stays live. grep(1) recursed and matched
    # sgp/a/b/f, while the scan read that `--` as end-of-options and reported
    # no traversal. Found by an earlier review; the whole option set was then audited
    # against `--help` on the tool for every wrapped tool.
    ("required-value-option-eats-the-dashdash",
     ["grep", "--group-separator", "--", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "the `--` is the separator's value; -r survives"),
    ("required-value-option-devices",
     ["grep", "--devices", "read", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "same for --devices, also missing before the audit"),
    ("required-value-option-exclude-from",
     ["grep", "--exclude-from", "f", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "...and --exclude-from"),
    ("required-value-option-still-allows-a-cheap-root",
     ["grep", "--group-separator", "SEP", "-r", "needle", "/tmp/cheap"],
     "/var/tmp", ALLOW, "the inverse: a cheap root is still fine"),

    # The CWD-DEFAULT shape, which the rows above do not reach because they
    # all name a root explicitly. `grep -r --exclude-from FILE PATTERN` has
    # no path operand at all: FILE is the option value, PATTERN is charged
    # against skip_pos, and grep walks the CWD. Before the audit the unknown
    # `--exclude-from` made FILE the pattern and PATTERN a root, so a cheap
    # operand hid a expensive cwd walk -- Layer 1 allowed it while Layer 2 would
    # have reported it.
    ("required-value-option-leaves-the-cwd-as-the-root",
     ["grep", "-r", "--exclude-from", "/tmp/globs", "/tmp/needle"],
     FAST_CWD, REFUSE, "no path operand: the walk is the cwd, which is expensive"),
    ("required-value-option-cwd-default-on-a-cheap-cwd",
     ["grep", "-r", "--exclude-from", "/tmp/globs", "/tmp/needle"],
     "/var/tmp", ALLOW, "...and the same shape on ext4 is fine"),

    # --- fd's -x/-X are command TEMPLATES, not one-token values ------------
    # An earlier review listed them as value_flags and argued that understating the
    # template errs toward refusing. It does not: a template token charged as
    # a root STOPS the fall-back to the cwd. Verified on the tool --
    # `fd -x echo {} /tmp/cheap` printed `./deep /tmp/cheap` once per CWD
    # entry, so fd walked the cwd and /tmp/cheap was data.
    ("fd-exec-template-token-is-not-a-root",
     ["fd", "-x", "echo", "{}", "/tmp/cheap"],
     FAST_CWD, REFUSE, "the walk is the cwd; /tmp/cheap is template data"),
    ("fd-exec-template-ends-at-a-semicolon",
     ["fd", "-x", "echo", "{}", ";", "target", "/tmp/cheap"],
     FAST_CWD, ALLOW,
     "`fd -x echo {} ';' target` made target the pattern, so the root after "
     "it is real -- and cheap"),
    ("fd-exec-template-swallows-a-depth-bound",
     ["fd", "-x", "echo", "{}", "--max-depth", "1"],
     FAST_CWD, REFUSE,
     "fd(1) walked the cwd to depth 2 with the bound as template data"),
    ("fd-bound-before-the-exec-template-still-counts",
     ["fd", "--max-depth", "1", "-x", "echo", "{}"],
     FAST_CWD, ALLOW, "the inverse: a real bound before -x is honoured"),
    ("fd-exec-batch-gets-the-same-treatment",
     ["fd", "-X", "echo", "/tmp/cheap"],
     FAST_CWD, REFUSE, "-X is the same shape as -x"),

    # --- fd's --base-directory is where the walk STARTS --------------------
    # `fd --base-directory /var/tmp/sgp/deep target` found ./deeper/target.txt
    # -- relative to the base, not the cwd. Skipping it as a passive value
    # fell back to the caller's cwd and let a walk beginning on the expensive mount look
    # cheap.
    ("fd-base-directory-is-the-root",
     ["fd", "--base-directory", "/scratch", "pat"],
     "/var/tmp", REFUSE, "the walk starts on the expensive mount, whatever the cwd is"),
    ("fd-base-directory-cheap-is-allowed",
     ["fd", "--base-directory", "/tmp/cheap", "pat"],
     "/var/tmp", ALLOW, "the inverse: a cheap base directory is fine"),

    # --- --base-directory is a COORDINATE SYSTEM, not a root --------------
    # An earlier review made it a root_flag, which is wrong in both directions: with
    # an explicit operand fd walks the OPERAND resolved against the base, not
    # the base. Verified on the tool -- from /var/tmp/sgp,
    # `fd --base-directory /tmp/basetest patfile sub` found `sub/patfile`,
    # which exists only under the base.
    ("fd-base-directory-relative-operand-escapes",
     ["fd", "--base-directory", "/tmp", "pat", "../scratch"],
     "/var/tmp", REFUSE,
     "`..` from /tmp reaches /, so this walks /scratch whatever the cwd is"),
    ("fd-base-directory-absolute-operand-wins",
     ["fd", "--base-directory", "/scratch", "pat", "/tmp/cheap"],
     "/var/tmp", ALLOW,
     "fd walks the operand, not the base -- this was a false refusal"),
    ("fd-base-directory-relative-operand-stays-cheap",
     ["fd", "--base-directory", "/tmp/cheap", "pat", "sub"],
     "/var/tmp", ALLOW, "resolved under the base, and the base is cheap"),
    ("fd-base-directory-attached-spelling",
     ["fd", "--base-directory=/scratch", "pat"],
     "/var/tmp", REFUSE, "the attached spelling is the same walk"),
    ("fd-relative-operand-without-a-base",
     ["fd", "pat", "../scratch"],
     "/tmp", REFUSE, "no base: still resolved against the caller's cwd"),

    # --- the base scan obeys the same grammar as every other scan ---------
    # The first version of effective_cwd() was a plain `for token in argv`,
    # which was a bypass: `--base-directory` inside an -x template is command
    # DATA -- fd walks the original cwd -- so switching the base to a cheap
    # path while roots() correctly skipped the template let a expensive cwd walk
    # through. The -x grammar is an earlier review's measurement: the separate form
    # consumes everything up to `;`.
    ("fd-base-directory-inside-an-exec-template-is-data",
     ["fd", "-x", "echo", "--base-directory", "/tmp/cheap"],
     FAST_CWD, REFUSE, "template data cannot move the coordinate system"),
    ("fd-base-directory-past-the-exec-terminator-is-real",
     ["fd", "-x", "echo", ";", "--base-directory", "/tmp/cheap", "pat"],
     FAST_CWD, ALLOW, "...but past the `;` it is an option again"),
    ("fd-base-directory-as-another-flags-value-is-data",
     ["fd", "--exclude", "--base-directory", "pat", "/tmp/cheap"],
     FAST_CWD, ALLOW,
     "--exclude eats it, so the base stays the cwd and /tmp/cheap is the "
     "root fd walks"),
    ("fd-base-directory-still-works-as-a-real-option",
     ["fd", "--base-directory", "/tmp/cheap", "pat"],
     FAST_CWD, ALLOW, "the inverse: the real option is unaffected"),

    # --- a CLUSTER's value eats the base flag too --------------------------
    # `fd -HE --base-directory /tmp/cheap pat` has -E (exclude) consume the
    # flag, so the base stays the cwd. The base scan skipped only EXACT value
    # flags, never saw `-HE`, and switched to the cheap path -- while roots()
    # parsed the cluster correctly and resolved the real relative root
    # against that false base.
    # FAST_CWD, not a literal /scratch: run_shim falls back to `/` for a
    # cwd that does not exist on the machine running the tests, which makes
    # the row prove something else. conftest says so at the top.
    ("fd-cluster-value-eats-the-base-flag",
     ["fd", "-HE", "--base-directory", "/tmp/cheap", "pat"],
     FAST_CWD, REFUSE, "-E eats the flag, so the base is still the expensive mount"),
    ("fd-cluster-without-a-value-flag-leaves-the-base",
     ["fd", "-H", "--base-directory", "/tmp/cheap", "pat"],
     FAST_CWD, ALLOW, "the inverse: -H eats nothing, so the base is real"),

    # --- every occurrence resolves against the ORIGINAL cwd ---------------
    # fd resolves the base against its own process directory, so from
    # /scratch `--base-directory /tmp --base-directory sub` is
    # /scratch/sub, not /tmp/sub. Resolving them in sequence made the table
    # disagree with the shim, which had it right.
    ("fd-repeated-relative-base-uses-the-original-cwd",
     ["fd", "--base-directory", "/tmp", "--base-directory", "sub", "pat"],
     FAST_CWD, REFUSE, "the second is relative to the cwd, not to /tmp"),
    ("fd-repeated-absolute-base-last-wins",
     ["fd", "--base-directory", "/scratch", "--base-directory", "/tmp/cheap",
      "pat"],
     "/var/tmp", ALLOW, "...and an absolute one still simply wins"),

    # --- grep's directory action is LAST-WINS, and -r is one of them -------
    # Every row measured against grep(1) on the tool. Output means it
    # descended; blank means it did not.
    #   grep -r -d skip           ->            grep -d skip -r      -> match
    #   grep -d recurse -d skip   ->            grep -d skip -d recurse -> match
    #   grep -rd skip             ->            rgrep -d skip        ->
    #   grep -r -dskip            ->            rgrep                -> match
    #   grep -r --directories=skip ->           grep --directories=skip -r -> match
    #   grep -r --dir=skip        ->
    # Returning on the first -r refused commands that walk nothing.
    ("dir-action-skip-after-r-wins",
     ["grep", "-r", "-d", "skip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "the later -d skip undoes the -r"),
    ("dir-action-r-after-skip-wins",
     ["grep", "-d", "skip", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "...and the other order really does descend"),
    ("dir-action-recurse-then-skip",
     ["grep", "-d", "recurse", "-d", "skip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "two actions, the last one wins"),
    ("dir-action-skip-then-recurse",
     ["grep", "-d", "skip", "-d", "recurse", "needle", "/scratch"],
     "/var/tmp", REFUSE, "...and reversed"),
    ("dir-action-inside-a-cluster",
     ["grep", "-rd", "skip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "`-rd skip` is -r then -d skip, left to right"),
    ("dir-action-glued-value",
     ["grep", "-r", "-dskip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "the glued short spelling"),
    ("dir-action-attached-long",
     ["grep", "-r", "--directories=skip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "the attached long spelling"),
    ("dir-action-abbreviated-long",
     ["grep", "-r", "--dir=skip", "needle", "/scratch"],
     "/var/tmp", ALLOW, "...and abbreviated, which grep accepts"),
    ("dir-action-attached-long-then-r",
     ["grep", "--directories=skip", "-r", "needle", "/scratch"],
     "/var/tmp", REFUSE, "order still decides for the long spelling"),
    ("dir-action-rgrep-honours-skip",
     ["rgrep", "-d", "skip", "needle", "/scratch"],
     "/var/tmp", ALLOW,
     "rgrep is grep -r, so it starts recursive AND honours -d skip -- "
     "`always=True` said otherwise and made this a false refusal"),
    ("dir-action-rgrep-plain-still-descends",
     ["rgrep", "needle", "/scratch"],
     "/var/tmp", REFUSE, "the inverse: plain rgrep is the incident shape"),
    ("dir-action-after-dashdash-is-a-pattern",
     ["grep", "--", "-r", "-d", "skip", "/scratch"],
     "/var/tmp", ALLOW, "after `--` these are patterns, not actions"),

    # --- a dir-action flag with NO value leaves the action alone ----------
    # getopt rejects it outright -- real grep says "option requires an
    # argument -- 'd'", and `grep -r -d needle` is "invalid argument 'needle'
    # for '--directories'" -- so nothing is applied. Reading the absent value
    # as "skip" invented one, and made the table ALLOW while the shim
    # REFUSED. Neither verdict can matter for a command that will not run;
    # the disagreement is what mattered.
    ("dir-action-with-no-value-leaves-the-action",
     ["grep", "-r", "-d"],
     FAST_CWD, REFUSE, "the -r still stands, because -d applied nothing"),
    ("dir-action-with-no-value-cluster-form",
     ["grep", "-rd"],
     FAST_CWD, REFUSE, "...and the same in a cluster"),
    ("dir-action-with-no-value-on-rgrep",
     ["rgrep", "-d"],
     FAST_CWD, REFUSE, "rgrep starts recursive, so it stays recursive"),

    # --- ...including the ABBREVIATED long spelling -----------------------
    # getopt resolves the abbreviation FIRST and then finds no argument for
    # it, so an abbreviated `--directories` in final position is the same
    # fact as the exact one, not a different one. The table used to read the
    # absent value as "skip" here while reading it as "nothing applied" for
    # the exact spelling, which made it ALLOW where the shim REFUSED -- the
    # last shape in this matrix where the two consumers disagreed.
    #
    # Measured, GNU grep 3.6, relative operands in a scratch tree:
    #   grep -r pat d1 --di        "option '--directories' requires an
    #   grep -r pat d1 --dir        argument", rc 2, walks nothing
    #   grep -r pat d1 --directories  the same sentence, rc 2
    #   grep -r pat d1 --di d2     "invalid argument 'd2' for
    #                               '--directories'" -- so the abbreviation
    #                               really does eat the next token when
    #                               there is one
    #   grep -r pat d1 --di skip   rc 1, no output: does not descend
    #   grep -r pat d1 --di recurse  descends
    #   grep -r pat d1 --d         "option '--d' is ambiguous", rc 2
    ("dir-action-with-no-value-abbreviated-long",
     ["grep", "-r", "pat", "/scratch", "--di"],
     "/var/tmp", REFUSE,
     "the -r still stands, because the abbreviation applied nothing"),
    ("dir-action-with-no-value-abbreviated-long-longer",
     ["grep", "-r", "pat", "/scratch", "--dir"],
     "/var/tmp", REFUSE, "a longer abbreviation of the same flag"),
    ("dir-action-abbreviated-long-with-a-value-still-allows",
     ["grep", "-r", "pat", "/scratch", "--di", "skip"],
     "/var/tmp", ALLOW,
     "the inverse: the abbreviation with its value really does turn the "
     "walk off, so the rows above are not a blanket refusal of `--di`"),
    ("dir-action-abbreviated-long-repeated-last-has-no-value",
     ["grep", "-r", "pat", "/scratch", "--di", "skip", "--dir"],
     "/var/tmp", ALLOW,
     "the repeated occurrence: a later valueless one applies nothing, so "
     "the earlier skip is still what is in force"),
    ("dir-action-with-no-value-abbreviated-on-rgrep",
     ["rgrep", "pat", "/scratch", "--directorie"],
     "/var/tmp", REFUSE,
     "rgrep is grep -r and resolves the abbreviation the same way, so it "
     "stays recursive"),
    ("dir-action-ambiguous-long-prefix-applies-nothing",
     ["grep", "-r", "pat", "/scratch", "--d"],
     "/var/tmp", REFUSE,
     "`--d` is ambiguous between --devices, --directories and "
     "--dereference-recursive, so getopt resolves nothing and the -r stands"),

    # --- a token that is another flag's VALUE is not an exec introducer ----
    # The shim tested its exec-introducer list before honouring a pending
    # value, so `-x` as --exclude's value started a template that swallowed
    # the real bound: the shim refused while the table allowed. clap rejects
    # this exact command ("--exclude <pattern>... requires a value but none
    # was supplied"), so it is not a live bypass -- but a disagreement
    # between the two consumers is the one condition this repo calls worse
    # than either layer alone, and it had no row.
    ("value-that-looks-like-an-exec-flag",
     ["fd", "--exclude", "-x", "--max-depth", "1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-x is --exclude's value; the bound after it is real"),
    ("value-that-looks-like-an-exec-batch-flag",
     ["fd", "--exclude", "-X", "--max-depth", "1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "...and the -X spelling"),
    ("real-exec-introducer-still-swallows-the-bound",
     ["fd", "-x", "echo", "{}", "--max-depth", "1"],
     FAST_CWD, REFUSE,
     "the inverse: a REAL -x still hides the bound, so this stays refused"),

    # --- fd's ATTACHED --exec= takes only its attached value ---------------
    # An earlier review asked for `--exec=echo` to be treated as a template introducer
    # consuming the rest. It is not: `fd --exec=echo {} DIR` made `{}` the
    # PATTERN and hit fd's regex parser ("repetition operator missing
    # expression"), and `fd --exec=echo f /tmp/cheapdir` walked
    # /tmp/cheapdir -- the given search path, not the cwd. Treating the rest
    # as template data would have dropped the real root.
    ("fd-attached-exec-does-not-eat-the-root",
     ["fd", "--exec=echo", "pat", "/scratch"],
     "/var/tmp", REFUSE, "/scratch is the search path, and it is expensive"),
    ("fd-attached-exec-cheap-root-allowed",
     ["fd", "--exec=echo", "pat", "/tmp/cheap"],
     "/var/tmp", ALLOW, "...and a cheap search path is still cheap"),

    # The GLUED SHORT form behaves like the attached long one, measured with a
    # positive control so "no output" cannot be mistaken for "no match":
    #   fd --exec=echo tfile                 -> ./deep/deeper/tfile
    #   fd --exec=echo tfile --max-depth 1   -> (nothing: the bound applied)
    #   fd -xecho      tfile --max-depth 1   -> (nothing: same)
    #   fd -x echo     tfile --max-depth 1   -> 3 entries, unbounded
    # So only the SEPARATE form swallows what follows.
    ("fd-glued-short-exec-honours-a-following-bound",
     ["fd", "-xecho", "tfile", "--max-depth", "1"],
     FAST_CWD, ALLOW, "-xecho takes only `echo`; the bound after it is real"),
    ("fd-attached-exec-honours-a-following-bound",
     ["fd", "--exec=echo", "tfile", "--max-depth", "1"],
     FAST_CWD, ALLOW, "...and the attached long spelling"),
    ("fd-separate-exec-still-swallows-the-bound",
     ["fd", "-x", "echo", "tfile", "--max-depth", "1"],
     FAST_CWD, REFUSE,
     "the inverse: the separate form really does take it as template data"),

    # --- the GLUED short exec form takes only its attached value ----------
    # `-x` is both a value flag and an exec flag, and that is the grammar
    # rather than a contradiction. Measured on the tool, from a tree with
    # a/b/c/patfile:
    #   fd -xd1 patfile    ->  [fd error]: Command not found: "d1"
    #                          "./a/b/c/patfile"   -- it WALKED to depth 3,
    #                          so `d1` was the command and nothing bounded it
    #   fd -xecho patfile  ->  ./a/b/c/patfile     (control)
    #   fd -xe patfile .   ->  Command not found: "e" -- `e` is the command,
    #                          patfile the pattern, `.` the root
    # parse_short_cluster read `-xd1` as `-x` plus `-d 1` and called the walk
    # bounded, and `-xe` as `-e` eating the next token, which lost the root.
    ("fd-glued-exec-is-not-a-depth-bound",
     ["fd", "-xd1", "pat"],
     FAST_CWD, REFUSE, "`d1` is the command, so nothing bounds the cwd walk"),
    ("fd-glued-exec-does-not-eat-the-root",
     ["fd", "-xe", "pat", "/scratch"],
     "/var/tmp", REFUSE, "`e` is the command; /scratch is still the root"),
    ("fd-glued-exec-leaves-a-following-bound-real",
     ["fd", "-xecho", "pat", "--max-depth", "1"],
     FAST_CWD, ALLOW, "the inverse: the glued form consumes nothing further"),
    ("fd-real-short-value-flag-still-consumes-its-value",
     ["fd", "-e", "txt", "pat", "/scratch"],
     "/var/tmp", REFUSE,
     "and a genuine -e is unaffected -- it takes `txt`, not the root"),

    # --- the LAST depth flag wins, like the directory action --------------
    # Measured on the tool:
    #   find sgp -maxdepth 1 -maxdepth 9 -name deep.txt -> found the depth-6 file
    #   find sgp -maxdepth 9 -maxdepth 1 -name deep.txt -> (nothing)
    #   du -d 1 -d 9 sgp -> 6 lines      du -d 9 -d 1 sgp -> 2 lines
    #   tree -L 1 -L 4 sgp -> 4 directories
    # Stopping at the first one read `-maxdepth 1 -maxdepth 9` as bounded
    # while find walks to depth 9. Same shape as an earlier review's directory action,
    # and the same tell: an option whose value can be RESTATED is a setting,
    # not a switch.
    ("depth-last-wins-deep-second",
     ["find", "/scratch", "-maxdepth", "1", "-maxdepth", "9", "-name", "x"],
     "/var/tmp", REFUSE, "the later -maxdepth 9 is the one in force"),
    ("depth-last-wins-shallow-second",
     ["find", "/scratch", "-maxdepth", "9", "-maxdepth", "1", "-name", "x"],
     "/var/tmp", ALLOW, "...and reversed, it really is bounded"),
    # du used to witness this pair and cannot since du lost its depth flag -- `-d` is not a
    # bound, so there is no "last" for it to win. fd cannot take its place
    # either: `fd -d 1 -d 9` exits with "The argument '--max-depth <depth>'
    # was provided more than once, but cannot be used multiple times"
    # (verified on the tool), so a repeated depth flag is not even a spelling
    # fd accepts. find and tree carry the rule.
    ("depth-last-wins-tree",
     ["tree", "-L", "1", "-L", "4", "/scratch"],
     "/var/tmp", REFUSE, "tree -L 1 -L 4 printed 4 directories"),
    # The ALLOW direction, which lost its only other witness when the du rows
    # went. Verified on the tool: `tree -L 4 -L 1` printed 1 directory.
    ("depth-last-wins-tree-shallow-second",
     ["tree", "-L", "4", "-L", "1", "/scratch"],
     "/var/tmp", ALLOW, "...and reversed, tree -L 4 -L 1 printed 1"),
    ("depth-restated-with-the-same-value",
     ["find", "/scratch", "-maxdepth", "1", "-maxdepth", "1", "-name", "x"],
     "/var/tmp", ALLOW, "restating the same bound changes nothing"),
    # A flag whose value is missing or non-conforming means the
    # tool exits and walks nothing, so ALLOW is correct regardless of the
    # root. Measured, findutils 4.8.0: `find -maxdepth '' 2 -f d1` ->
    # "Expected a positive decimal integer argument to -maxdepth", rc=1.
    ("find-maxdepth-empty-value-is-allowed",
     ["find", "/scratch", "-maxdepth", "", "2", "-f", "/tmp/cheap"],
     "/var/tmp", ALLOW,
     "find validates the argument and exits before walking anything"),
    ("find-maxdepth-flag-shaped-value-is-allowed",
     ["find", "/scratch", "-maxdepth", "-L", "-name", "x"],
     "/var/tmp", ALLOW,
     "the value is not consumed BECAUSE it looks like a flag; it is simply "
     "not a positive decimal integer, same as any other non-numeric word"),
    ("find-maxdepth-ordinary-word-is-allowed",
     ["find", "/scratch", "-maxdepth", "word", "-name", "x"],
     "/var/tmp", ALLOW, "an ordinary word is no more numeric than a flag spelling"),
    # tree v2.0.2, measured: `tree -L 2 d1 -L` -> "Missing argument to -L
    # option", rc=1 -- the SECOND -L has nothing after it at all. The table
    # used to read the last occurrence, find no value, and call it
    # unbounded (REFUSE); the shim kept the first -L's value and allowed --
    # right verdict, wrong reason, since tree never walks either way.
    ("tree-dangling-last-L-is-allowed",
     ["tree", "-L", "2", "/scratch", "-L"],
     "/var/tmp", ALLOW, "the dangling -L has no value; tree exits before walking"),
    ("depth-bound-before-a-dashdash-still-counts",
     ["rg", "--max-depth", "1", "--", "--max-depth", "9", "/scratch"],
     "/var/tmp", ALLOW,
     "the second is a PATTERN, so the real bound before it is what holds"),


    # --- a clustered DEPTH flag is still a bound ----------------------------
    # `du -hd1 /scratch` is `-h` plus `-d1`: a real depth-1 walk. Round
    # eleven taught the bounded scanner to SKIP a cluster's value so a
    # pattern could not masquerade as a bound, and skipping discarded genuine
    # bounds with it -- the mirror image of the bypass it fixed.
    # fd carries the BOUND half; verified on the tool, `fd -Hd1 . top` and
    # `fd -Hd 1 . top` both returned exactly the depth-1 entries.
    ("clustered-depth-flag-glued",
     ["fd", "-Hd1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-H plus -d1 is bounded at one level"),
    ("clustered-depth-flag-separate",
     ["fd", "-Hd", "1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "same, with the value in the next element"),
    # du carries the VALUE-SKIPPING half, which outlived its bound: `-d` stays
    # in du's value_flags precisely so the cluster's 1 is not charged as a
    # path. Dropping it there charges `<cwd>/1` and refuses a phantom.
    ("clustered-output-only-flag-still-eats-its-value",
     ["du", "-hd", "1", "/tmp/cheap"],
     FAST_CWD, ALLOW, "-d bounds nothing for du and still consumes its 1"),
    ("clustered-depth-flag-too-deep",
     ["fd", "-Hd9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "...and a bound above the threshold is still no bound"),
    ("clustered-depth-flag-fd",
     ["fd", "-Hd2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the same shape in fd's grammar"),

    # --- unambiguous long-option abbreviations ------------------------------
    # GNU getopt_long takes any unique prefix, so these all recurse -- checked
    # against grep(1), which accepts from `--di` up and calls `--d`
    # ambiguous. Exact-match tables missed every one of them.
    #
    # Applied only where a miss is a BYPASS (rec, rec-value, pattern flags).
    # A missed abbreviation of a VALUE or DEPTH flag errs toward refusing,
    # which is safe, and covering those correctly would need grep's whole
    # option list to judge ambiguity -- see resolve_long_flag().
    ("abbrev-recursive",
     ["grep", "--rec", "pat", "/home"],
     "/var/tmp", REFUSE, "`--rec` is a unique prefix of --recursive"),
    ("abbrev-directories-recurse",
     ["grep", "--dir=recurse", "pat", "/home"],
     "/var/tmp", REFUSE, "`--dir=recurse` is -r spelled two abbreviations deep"),
    ("abbrev-directories-shortest",
     ["grep", "--di=recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the shortest spelling grep still accepts"),
    ("abbrev-ambiguous-is-not-matched",
     ["grep", "--d=recurse", "pat", "/home"],
     "/var/tmp", ALLOW,
     "`--d` is ambiguous to grep too, so it errors rather than walking"),
    ("abbrev-directories-other-action",
     ["grep", "--dir=skip", "pat", "/home"],
     "/var/tmp", ALLOW, "only `recurse` turns the walk on"),
    ("abbrev-pattern-flag",
     ["grep", "--rege", "pat", "/home/someone/proj"],
     "/var/tmp", ALLOW,
     "an abbreviated -e still supplies the pattern, so the root is the path"),

    # --- find's other leading options ---------------------------------------
    # A CLOSED set, unlike grep's abbreviations: -H/-L/-P, plus -O<level>
    # attached and -D taking the next token. `find -O3 /scratch -name x` and
    # `find -D search /scratch -name x` are both accepted by find(1), and
    # both used to end the operand scan on the first token and fall back to
    # the cwd -- a bypass needing no `--` at all.
    ("find-optimise-level",
     ["find", "-O3", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "-O3 is a leading option, not the end of the operands"),
    ("find-optimise-level-with-dashdash",
     ["find", "-O3", "--", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "and it composes with the end-of-options marker"),
    ("find-debug-flag-eats-its-value",
     ["find", "-D", "search", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "-D takes `search`; /scratch is still the root"),
    ("find-leading-options-then-a-real-bound",
     ["find", "-L", "-O2", "/scratch", "-maxdepth", "2"],
     "/var/tmp", ALLOW, "...and a genuine bound after them still allows"),

    # --- a pattern flag AFTER the path  --------------------------
    # `grep -r DIR -e pat` searches DIR: a pattern flag ANYWHERE makes EVERY
    # positional a path, verified on the tool -- `grep -r d1 -e needle d2`
    # searches both. So which positional is the pattern cannot be known until
    # argv has been read to the end. The shim used to decide streaming and
    # threw the path away three tokens before the flag that proved it was one,
    # then fell back to the cwd: ALLOWED, while the table refused.
    #
    # Of the many grep/rgrep/rg rows in this matrix before these, exactly ONE
    # put the path before a pattern flag -- and it only passed because its cwd
    # made the fallback refuse anyway. That is why many reviews never saw it.
    # All four spellings get a row, because the fix has to resolve each.
    ("path-first-pattern-flag-exact",
     ["grep", "-r", "/scratch", "-e", "pat"],
     "/var/tmp", REFUSE, "the bug: /scratch is a path, not the pattern"),
    ("path-first-pattern-flag-glued-short",
     ["grep", "-r", "/scratch", "-epat"],
     "/var/tmp", REFUSE, "...and in FINAL position, where a scan that only"
     " re-checks on the next token never gets one"),
    ("path-first-pattern-flag-attached-long",
     ["grep", "-r", "/scratch", "--regexp=pat"],
     "/var/tmp", REFUSE, "same, attached long spelling"),
    ("path-first-pattern-flag-abbreviated",
     ["grep", "-r", "/scratch", "--rege", "pat"],
     "/var/tmp", REFUSE, "same, unique-prefix abbreviation (grep abbreviates)"),
    ("path-first-pattern-flag-abbreviated-attached",
     ["grep", "-r", "/scratch", "--rege=pat"],
     "/var/tmp", REFUSE, "...and abbreviated WITH an attached value"),
    ("path-first-pattern-flag-in-a-cluster",
     ["grep", "-r", "/scratch", "-nepat"],
     "/var/tmp", REFUSE, "reached mid-cluster: -n then -e with pat glued on"),
    ("path-first-pattern-flag-in-a-cluster-separate",
     ["grep", "-r", "/scratch", "-ne", "pat"],
     "/var/tmp", REFUSE, "same, value in the next element"),
    ("path-first-pattern-file-flag",
     ["grep", "-r", "/scratch", "-f", "/tmp/pats"],
     "/var/tmp", REFUSE, "-f supplies the pattern from a file, same as -e"),
    ("path-first-pattern-flag-with-no-value",
     ["grep", "-r", "/scratch", "-e"],
     "/var/tmp", REFUSE, "a dangling -e still frees the positional"),
    ("path-first-pattern-flag-rg",
     ["rg", "/scratch", "-e", "pat"],
     "/var/tmp", REFUSE, "rg has the same grammar and the same bug"),
    ("path-first-pattern-flag-rg-glued",
     ["rg", "/scratch", "-epat"],
     "/var/tmp", REFUSE, "rg, final position"),
    # The same bug wearing the other face -- a FALSE REFUSAL. The cluster
    # `-xdev` runs up to `-e` with value `v`, and `-e` is a pattern flag, so
    # the table frees /tmp/cheap to be a path and allows; the shim had already
    # discarded it and fell back to the expensive cwd.
    ("path-first-cluster-frees-a-cheap-root",
     ["rg", "/tmp/cheap", "-xdev"],
     FAST_CWD, ALLOW, "-xdev reaches -e, so /tmp/cheap is a path and cheap"),
    # Two paths and a late flag: both are roots, and both consumers refuse.
    ("path-first-two-paths-then-the-flag",
     ["grep", "-r", "/scratch", "/home", "-e", "pat"],
     "/var/tmp", REFUSE, "a late flag frees EVERY positional, not just one"),
    # --- ...and the shapes that must NOT gain a root ------------------------
    # Each of these is a way a naive "does argv contain -e" pre-scan gets it
    # wrong, which is why the fix re-runs the real scanner instead.
    ("late-pattern-flag-eaten-as-another-flags-value",
     ["grep", "-r", "/scratch", "--label", "-e", "pat"],
     "/var/tmp", ALLOW, "--label consumes the -e, so there is no pattern flag"
     " and /scratch is the pattern"),
    ("late-pattern-flag-after-a-dashdash",
     ["grep", "-r", "/scratch", "--", "-e", "pat"],
     "/var/tmp", ALLOW,
     "`--` ends the options, so -e is a FILE: real grep reports"
     " `-e: No such file or directory`, and /scratch is the pattern"),
    ("pattern-flag-eaten-by-include-before-the-path",
     ["grep", "-r", "--include", "-e", "/scratch"],
     "/var/tmp", ALLOW, "same rule, flag-first: /scratch is the pattern"),
    ("path-first-pattern-flag-cheap-root",
     ["grep", "-r", "/tmp/cheap", "-e", "pat"],
     FAST_CWD, ALLOW, "the freed path is cheap; it must not fall to the cwd"),
    ("path-first-inert-without-pattern-flags",
     ["fd", "pat", "/tmp/cheap"],
     FAST_CWD, ALLOW, "fd skips a positional but has NO pattern flags, so the"
     " two-pass resolution must leave it exactly as it was"),

    # --- an EMPTY argv element is a positional, never a flag ---------------
    #  sg_in() padded both sides before matching, so an empty
    # needle matched an empty HAYSTACK -- and `sg_execflags` is empty for
    # grep, rgrep, rg, tree, du and fzf. The empty token therefore read as an
    # exec introducer and swallowed every REMAINING token as an exec body, so
    # the real path vanished and the walk fell back to the cwd: ALLOWED by
    # Layer 1, refused by the table. (fd reaches the pattern-flag arm
    # instead; find and bfs were never affected, leading mode returning
    # before these arms.)
    #
    # The matrix had no empty-string argv anywhere, which is why many reviews
    # and a deploy-readiness pass all missed it. It is the ordinary
    # shape of `find "$dir" ...` with dir unset, not an exotic argv.
    ("empty-element-does-not-swallow-the-root-tree",
     ["tree", "", "/scratch"],
     "/var/tmp", REFUSE, "the reported bypass: /scratch was being lost"),
    ("empty-element-does-not-swallow-the-root-du",
     ["du", "", "1", "/scratch"],
     "/var/tmp", REFUSE, "same shape; everything after it was swallowed"),
    ("empty-element-does-not-swallow-the-root-rg",
     ["rg", "", "pat", "/scratch"],
     "/var/tmp", REFUSE, "same shape; rg's exec list is empty too"),
    # An empty element is a legitimate POSITIONAL, so the fix cannot simply
    # skip it. `grep -r "" DIR` has an empty PATTERN -- it matches every line
    # -- and dropping the token would promote DIR into the pattern slot and
    # lose the root a second way. This row fails in that direction.
    ("empty-pattern-is-a-pattern-not-a-skipped-token",
     ["grep", "-r", "", "/scratch"],
     "/var/tmp", REFUSE, "the empty pattern is consumed; /scratch is the root"),
    # ...and the over-correction guard: with a CHEAP operand the empty element
    # must not cost the root either, or this falls back to the expensive cwd and
    # refuses a walk that is genuinely cheap.
    ("empty-pattern-leaves-a-cheap-root-cheap",
     ["grep", "-r", "", "/tmp/cheap"],
     FAST_CWD, ALLOW, "the root survives the empty element in both directions"),
    # An empty operand walks NOTHING and does not stop the others -- measured
    # on the tool: `find real "" -name '*.txt'` returns real's hits and errors
    # only on the empty one; `du -sh real ""` still prints real's total. So it
    # charges no root AND must not trigger the cwd fallback, because the
    # caller did name operands. Closing the bypass by charging the cwd instead
    # traded it for a false refusal in the TRAILING position: these four were
    # allowed before the empty-operand fix, and refusing a walk the tool performs entirely on a
    # cheap root is how an advisory layer gets aliased around.
    ("trailing-empty-operand-does-not-invent-the-cwd-tree",
     ["tree", "/tmp/cheap", ""],
     FAST_CWD, ALLOW,
     "tree opens /tmp/cheap and then dies on the empty operand -- measured on"
     " the tool, `tree real \"\"` SEGFAULTS (v2.0.2); the walk is still of a"
     " cheap root, which is what the verdict is about"),
    ("trailing-empty-operand-does-not-invent-the-cwd-du",
     ["du", "-sh", "/tmp/cheap", ""],
     FAST_CWD, ALLOW, "du -sh real '' still prints real's total"),
    ("trailing-empty-operand-does-not-invent-the-cwd-grep",
     ["grep", "-r", "pat", "/tmp/cheap", ""],
     FAST_CWD, ALLOW, "same, with the pattern already supplied"),
    # An empty operand ALONE walks nothing, so it is not the cwd either. This
    # suppression was reverted once as a BYPASS and is restored now that the late-pattern-flag defect
    # is fixed, which was its only cause: the shim decided skip_pos streaming
    # while the table decided it after the whole scan, so with the fallback
    # suppressed `grep -r /scratch "" -e pat` had the shim skip /scratch as
    # the pattern, decline the empty, decline the cwd, and judge nothing at
    # all. Both consumers now resolve the pattern flag first, so they count
    # the same operands and flip together.
    ("a-lone-empty-operand-walks-nothing",
     ["tree", ""],
     FAST_CWD, ALLOW, "an operand was named; inventing the cwd for it refuses"
     " a directory the tool never opens -- and tree segfaults on it"),
    # The case that made the suppression a bypass, kept as its regression pin.
    # Still REFUSE, but now for the right reason: /scratch is charged,
    # because -e frees it to be a path. Before the late-pattern-flag fix it
    # refused only because the cwd happened to be expensive too.
    ("empty-operand-beside-a-path-the-shim-skips",
     ["grep", "-r", "/scratch", "", "-e", "pat"],
     FAST_CWD, REFUSE, "-e frees /scratch to be a path; the empty charges"
     " nothing and neither consumer reaches the cwd"),
    ("empty-operand-in-the-skip-slot",
     ["rg", "", "-e", "pat"],
     FAST_CWD, ALLOW, "the empty is the only operand and walks nothing"),
    # The leading-mode `--` arm reads the operand COUNT mid-loop, mirroring
    # the table's `if token == "--" and not positionals`. While an empty
    # operand went uncounted those two stopped meaning the same thing and the
    # shim refused this while the table allowed it. Real find errors with
    # `unknown predicate '--'` and walks nothing, so ALLOW is right.
    ("empty-operand-before-a-dashdash-in-leading-mode",
     ["find", "", "--", "/scratch"],
     "/var/tmp", ALLOW, "find errors on `--` here and walks nothing"),
    # ...and bfs is NOT the twin, which is what the bfs dashdash measurement turned out to show. This row
    # asserted ALLOW on the assumption that it was; measured on bfs 2.1 and
    # 4.0.4, `bfs "" -- d1 -name x` errors on the empty operand and then walks
    # d1. Both consumers shared find's reading, so the matrix agreed with
    # itself on the wrong answer -- the same way the `--` rows below were
    # invisible until someone ran the tool.
    ("empty-operand-before-a-dashdash-bfs",
     ["bfs", "", "--", "/scratch"],
     "/var/tmp", REFUSE,
     "bfs carries on through `--`, so /scratch is still an operand"),

    # --- `--` means different things to find and to bfs  --------------
    # Measured, identical on bfs 2.1, 2.3.1 (a packaged build) and 4.0.4:
    #
    #   bfs  d1 -- d2 -name x   rc=0, walks BOTH d1 and d2
    #   find d1 -- d2 -name x   rc=1, "unknown predicate `--'", walks NOTHING
    #
    # Both consumers modelled find, so for bfs every operand after a `--` went
    # uncharged. They AGREED, so no argv row and no differential sweep could
    # see it. And bfs is user-installed here, so `link_farm` links no shim for
    # it  -- the table is bfs's only consumer, which made this a live gap
    # in the audit trail rather than a shim-only asymmetry.
    ("bfs-dashdash-does-not-end-the-operands",
     ["bfs", "/tmp/cheap", "--", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE,
     "bfs walks both operands; charging only the cheap one allowed the expensive mount"),
    ("find-dashdash-after-an-operand-does-end-them",
     ["find", "/tmp/cheap", "--", "/scratch", "-name", "x"],
     "/var/tmp", ALLOW,
     "the same argv to find is an unknown predicate and walks nothing"),
    ("bfs-repeated-dashdash",
     ["bfs", "--", "/tmp/cheap", "--", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "bfs tolerates it twice and still walks both"),
    ("bfs-dashdash-then-an-empty-and-a-root-flag",
     ["bfs", "--", "", "-f", "/scratch", "-name", "x"],
     FAST_CWD, REFUSE,
     "the first pass must not stop at `--`, or -f's value goes uncharged"),
    ("bfs-root-flag-empty-value-then-dashdash",
     ["bfs", "-f", "", "--", "/scratch", "-name", "x"],
     FAST_CWD, REFUSE,
     "the shim used to end the operands here and allow a walk bfs performs"),
    ("bfs-dashdash-then-bare-dash-and-root-flag",
     ["bfs", "--", "-", "-f", "/scratch", "-name", "x"],
     "/var/tmp", REFUSE, "`--` skipped, `-` charged, -f's value still a root"),
    ("bfs-dashdash-then-root-flag-then-empty",
     ["bfs", "--", "-f", "/scratch", "", "-name", "x"],
     FAST_CWD, REFUSE, "same, with the empty operand after the root"),
    # ...and it must not over-refuse. bfs still honours its own options after
    # `--`: measured, `bfs -- -f cheap -name x` exits 0 and walks ONLY cheap,
    # so `-f` is a root flag there and not a path called "-f". Charging its
    # value is what keeps these allowed from a expensive cwd -- the failure mode of
    # the first-pass change would have been to reach the cwd fallback instead.
    ("bfs-dashdash-then-a-cheap-root-flag-value",
     ["bfs", "--", "-f", "/tmp/cheap", "-name", "x"],
     FAST_CWD, ALLOW, "-f's value is charged, so the expensive cwd is never reached"),
    ("bfs-dashdash-then-a-cheap-operand",
     ["bfs", "--", "/tmp/cheap", "-name", "x"],
     FAST_CWD, ALLOW, "the operand after `--` is charged, cheap, and allowed"),
    # The fallback itself is untouched: no operand at all still means the cwd.
    ("no-operand-at-all-still-falls-back-to-the-cwd",
     ["tree"],
     FAST_CWD, REFUSE, "the cwd fallback still fires when nothing was named"),

    # --- path cleaning ------------------------------------------------------
    ("double-slash",
     ["find", "//scratch", "-name", "x"],
     "/home/someone", REFUSE, "normpath keeps a leading // without the collapse"),
    ("dotdot-escape",
     ["find", "/scratch/runs/../..", "-name", "x"],
     "/home/someone", REFUSE, "`..` resolves back up to /, which descends into everything"),
    ("relative-root",
     ["find", "runs", "-name", "x"],
     FAST_CWD, REFUSE, "relative operand joined to cwd"),
    ("tilde-unexpanded",
     ["find", "~", "-name", "x"],
     "/var/tmp", REFUSE,
     "a quoted ~ reaches the shim unexpanded; HOME is on the expensive mount"),

    # --- ugrep -------------------------------------------------------------
    # Every row below was EXECUTED against ugrep 7.8.4 -- the build a widely
    # used coding agent bundles, which is what produced the live shape -- over
    # a five-level tree, and the note says what it did.
    #
    # The live shape first: a long-running unbounded walk of / at a reference
    # deployment, recordable only as `opaque_traversal` with root/mount/fs
    # all null until this profile existed. The pattern is a stand-in.
    ("ugrep-live-incident-argv",
     ["ugrep", "-G", "--ignore-files", "--hidden", "-I",
      "--exclude-dir=.git", "--exclude-dir=.svn", "--exclude-dir=.hg",
      "--exclude-dir=.bzr", "--exclude-dir=.jj", "--exclude-dir=.sl",
      "-rln", "SomeProcessor|class SomeImage", "/"],
     "/var/tmp", REFUSE, "the audit trail's one real finding, in shape"),

    # --- a bare directory operand already recurses, at depth 1 -------------
    # `ugrep -l PAT .` listed ./f.txt and NOT ./a/f.txt. Modelling this as
    # "not a traversal" gives ALLOW for the wrong reason, and the reason is
    # what the next row depends on.
    ("ugrep-bare-operand-is-a-depth-1-walk",
     ["ugrep", "-l", "pat", "/scratch"],
     "/var/tmp", ALLOW, "one level is cheap on every mount"),
    ("ugrep-r-removes-the-default-bound",
     ["ugrep", "-r", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the inverse: -r walks all five levels"),
    ("ugrep-R-removes-it-too",
     ["ugrep", "-R", "pat", "/scratch"],
     "/var/tmp", REFUSE, "-R dereferences and still recurses"),
    ("ugrep-long-recursive",
     ["ugrep", "--recursive", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the long spelling"),
    ("ugrep-long-dereference-recursive",
     ["ugrep", "--dereference-recursive", "pat", "/scratch"],
     "/var/tmp", REFUSE, "and its dereferencing twin"),
    ("ugrep-file-operand-on-fast",
     ["ugrep", "pat", "/scratch/x.txt"],
     "/var/tmp", ALLOW, "an operand that is one file is a depth-1 walk too"),

    # --- one flag, TWO on-values ------------------------------------------
    # The collision the (flag, value) shape could not express: every consumer
    # short-circuits on the first pair whose FLAG matches, so a second pair
    # for -d was never compared and `dereference-recurse` read as "skip".
    # Measured: both values walk all five levels.
    ("ugrep-d-recurse",
     ["ugrep", "-d", "recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "-d recurse is -r spelled as an action"),
    ("ugrep-d-dereference-recurse",
     ["ugrep", "-d", "dereference-recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "COLLISION: the SECOND on-value for the same flag"),
    ("ugrep-directories-equals-dereference-recurse",
     ["ugrep", "--directories=dereference-recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "same collision, attached long spelling"),
    ("ugrep-glued-d-dereference-recurse",
     ["ugrep", "-ddereference-recurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "same collision, glued short spelling"),
    ("ugrep-glued-d-recurse",
     ["ugrep", "-drecurse", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the first on-value, glued"),

    # --- the action really does turn traversal off, and it is last-wins ----
    ("ugrep-d-skip-walks-nothing",
     ["ugrep", "-d", "skip", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-d skip: measured, no output at all"),
    ("ugrep-d-read-walks-nothing",
     ["ugrep", "-d", "read", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-d read warns `is a directory` and walks nothing"),
    ("ugrep-r-then-d-skip",
     ["ugrep", "-r", "-d", "skip", "pat", "/scratch"],
     "/var/tmp", ALLOW, "REPEATED setting, last wins: measured, no output"),
    ("ugrep-d-skip-then-r",
     ["ugrep", "-d", "skip", "-r", "pat", "/scratch"],
     "/var/tmp", REFUSE, "...and reversed, it walks all five levels"),
    ("ugrep-cluster-rd-skip",
     ["ugrep", "-rd", "skip", "pat", "/scratch"],
     "/var/tmp", ALLOW, "WITHIN the token: -r then -d skip, so -d wins"),

    # --- a depth flag outranks the action, and is NOT last-wins ------------
    # `-d skip` alone walks nothing; `-d skip --depth=9` and `--depth=9 -d
    # skip` BOTH walked all five levels. Reading this as last-wins allows an
    # unbounded the expensive mount walk in one of the two orders.
    ("ugrep-depth-beats-a-later-skip",
     ["ugrep", "--depth=9", "-d", "skip", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the depth flag wins even though skip came after"),
    ("ugrep-depth-beats-an-earlier-skip",
     ["ugrep", "-d", "skip", "--depth=9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "...and in the other order"),
    ("ugrep-skip-with-a-bounded-depth",
     ["ugrep", "-d", "skip", "--depth=2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse: it traverses, but only two levels"),

    # --- --depth enables recursion on its own ------------------------------
    # "Enables -r if -R or -r is not specified" -- and measured doing so.
    # Without depth_enables_traversal this is the live bypass: no rec flag
    # anywhere, so nothing else in the table makes it a traversal.
    ("ugrep-depth-alone-enables-recursion",
     ["ugrep", "--depth", "9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "no -r anywhere: --depth turns recursion on"),
    ("ugrep-depth-alone-bounded",
     ["ugrep", "--depth", "2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse, at a depth within the ceiling"),
    ("ugrep-depth-beats-r",
     ["ugrep", "-r", "--depth=2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "measured: two levels, not five"),
    ("ugrep-depth-beats-r-reversed",
     ["ugrep", "--depth=2", "-r", "pat", "/scratch"],
     "/var/tmp", ALLOW, "...whatever the order"),
    ("ugrep-depth-past-the-ceiling",
     ["ugrep", "-r", "--depth=9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "bounded, and past every mount's ceiling"),
    ("ugrep-home-allowance",
     ["ugrep", "-r", "--depth=4", "pat", "/home"],
     "/var/tmp", ALLOW, "/home's fixture allowance is 4"),
    ("ugrep-home-allowance-is-not-scratchs",
     ["ugrep", "-r", "--depth=4", "pat", "/scratch"],
     "/var/tmp", REFUSE, "and the other mount of the same type stays at the global ceiling"),

    # --- a repeated --depth forms a RANGE, and it is NOT last-wins ---------
    # ugrep is the one tool here whose repeats are not last-wins. A later
    # occurrence supplies the MAX and may not fall below a floor set by the
    # first: the MIN when the first expression carries one, the value itself
    # when it does not. Measured on a NINE-level tree, stdout and stderr read
    # separately, because a five-level tree cannot tell 3..5 from 3..7:
    #
    #   --depth=1 --depth=5      1..5      --depth=5 --depth=1   ERROR
    #   --depth=3,5 --depth=4    3..4      --depth=3,5 --depth=1 ERROR
    #   --depth=,9 --depth=5     ERROR     -3-5-7                3..7
    #
    # An earlier version of this branch took the LAST value and said that
    # gave the right verdict either way -- "a coincidence of the verdict
    # function". It does not, and the round-1 rows below are why: it holds
    # only while the smaller value is within MAXDEPTH_ALLOWED (= 2).
    # `--depth=9 --depth=5` errors and walks nothing, while last-wins read it
    # as a bound of 5, still above the ceiling, and REFUSED it. The claim had
    # been checked against exactly one pair.
    ("ugrep-repeated-depth-increasing",
     ["ugrep", "-r", "--depth=1", "--depth=9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "REPEATED: measured as the range 1..9"),
    ("ugrep-repeated-depth-decreasing",
     ["ugrep", "-r", "--depth=9", "--depth=1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "...reversed, `invalid argument -1`, walks nothing"),
    ("ugrep-repeated-depth-reversed-above-the-ceiling",
     ["ugrep", "-r", "--depth=9", "--depth=5", "pat", "/scratch"],
     "/var/tmp", ALLOW,
     "the row that disproved last-wins: ERROR, so ALLOW -- last-wins read a "
     "bound of 5, still over the ceiling, and refused a command that walks "
     "nothing"),
    ("ugrep-repeated-depth-increasing-is-still-refused",
     ["ugrep", "-r", "--depth=1", "--depth=5", "pat", "/scratch"],
     "/var/tmp", REFUSE,
     "the control that rules out `any repeat is malformed`: this really does "
     "walk 1..5, and calling it an error would be a bypass"),
    ("ugrep-repeated-depth-floor-is-the-min-when-there-is-one",
     ["ugrep", "-r", "--depth=3,5", "--depth=4", "pat", "/scratch"],
     "/var/tmp", REFUSE, "measured 3..4, VALID -- the floor is 3, not 5"),
    ("ugrep-repeated-depth-floor-is-the-value-when-there-is-no-min",
     ["ugrep", "-r", "--depth=,9", "--depth=5", "pat", "/scratch"],
     "/var/tmp", ALLOW,
     "measured ERROR: `,9` sets the floor to 9, so a later 5 is rejected -- "
     "the half that shows an absent MIN is not the same as a MIN of 1"),
    ("ugrep-reversed-range-in-one-value",
     ["ugrep", "-r", "--depth=5,3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid argument -5,3`: MIN above MAX is refused"),
    ("ugrep-reversed-range-in-a-cluster",
     ["ugrep", "-l9-3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid argument -9-3`, walks nothing"),
    ("ugrep-truncated-range-in-a-cluster",
     ["ugrep", "-l5-", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid argument -5-`: no MAX, so no bound"),
    ("ugrep-two-ranges-in-one-cluster",
     ["ugrep", "-l3-5-7", "pat", "/scratch"],
     "/var/tmp", REFUSE,
     "measured 3..7 on a nine-level tree -- two occurrences, floor 3, MAX 7"),
    # A malformed depth means the tool errors before opening anything, so it
    # is NOT a traversal -- and that must not depend on WHICH malformed
    # spelling it was. Both of these reach Layer 2 through is_traversal();
    # the first kept a stale bound until an earlier review, and the two disagreed.
    ("ugrep-skip-with-a-reversed-repeat-is-not-a-traversal",
     ["ugrep", "-d", "skip", "--depth=9", "--depth=5", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-d skip plus a rejected range: walks nothing"),
    ("ugrep-skip-with-a-reversed-cluster-range-is-not-a-traversal",
     ["ugrep", "-d", "skip", "-l9-3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the other spelling, and it must agree with it"),

    # --- [MIN,]MAX, and the asymmetry in it --------------------------------
    ("ugrep-depth-range-takes-the-max",
     ["ugrep", "-r", "--depth=1,9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "measured 1..9; reading it as malformed ALLOWS it"),
    ("ugrep-depth-range-within-the-ceiling",
     ["ugrep", "-r", "--depth=1,2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse"),
    ("ugrep-depth-range-empty-min-is-valid",
     ["ugrep", "-r", "--depth=,5", "pat", "/scratch"],
     "/var/tmp", REFUSE, "measured: `,5` walks 1..5, MIN is the optional half"),
    ("ugrep-depth-range-empty-min-bounded",
     ["ugrep", "-r", "--depth=,2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "and `,2` really walks only 1..2"),
    ("ugrep-depth-range-empty-max-is-malformed",
     ["ugrep", "-r", "--depth=2,", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid argument -2,`: the asymmetry is real"),
    ("ugrep-depth-three-fields-is-malformed",
     ["ugrep", "-r", "--depth=3,5,7", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid argument --depth=3,5,7`, walks nothing"),

    # --- digits cluster with letters --------------------------------------
    ("ugrep-digit-in-a-cluster-is-a-depth",
     ["ugrep", "-l9", "pat", "/scratch"],
     "/var/tmp", REFUSE, "-l9 is -l plus depth 9; without the scan, depth 1"),
    ("ugrep-digit-in-a-cluster-within-the-ceiling",
     ["ugrep", "-l1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse"),
    ("ugrep-digits-before-the-letters",
     ["ugrep", "-9l", "pat", "/scratch"],
     "/var/tmp", REFUSE, "measured: order within the token does not matter"),
    ("ugrep-digits-among-the-letters",
     ["ugrep", "-2ln", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-2ln is depth 2, and 2 is within the ceiling"),
    ("ugrep-rec-flag-and-a-digit-in-one-token",
     ["ugrep", "-rl2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "measured two levels: the digit bounds the -r"),
    ("ugrep-two-digit-run-is-one-number",
     ["ugrep", "-l10", "pat", "/scratch"],
     "/var/tmp", REFUSE, "COLLISION: -l10 is TEN, not -1 then -0"),
    ("ugrep-short-range-takes-the-max",
     ["ugrep", "-l3-5", "pat", "/scratch"],
     "/var/tmp", REFUSE, "measured 3..5; the MAX is what has to be judged"),
    ("ugrep-short-range-within-the-ceiling",
     ["ugrep", "-l1-2", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse"),
    ("ugrep-short-comma-range",
     ["ugrep", "-l3,5", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the comma spelling of the same range"),
    ("ugrep-zero-is-null-not-a-depth",
     ["ugrep", "-r0", "pat", "/scratch"],
     "/var/tmp", REFUSE,
     "-0 is --null: reading it as depth 0 makes an unbounded -r look bounded"),
    ("ugrep-zero-alone-leaves-the-default",
     ["ugrep", "-l0", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the inverse: still the depth-1 default, NUL-separated"),

    # --- the collision the digit scan must NOT fire on ---------------------
    # A numeric argument belonging to an earlier flag. Two different
    # mechanisms protect these, and both need a row: -A/-C are value_flags,
    # so parse_short_cluster hands the digits back as `glued` and they are
    # never scanned; -K/-m/-Z take OPTIONAL numeric arguments and so cannot
    # be value_flags at all (grep's --color rule), and are kept out of the
    # scan by the depth_digits charset instead.
    ("ugrep-context-value-is-not-a-depth",
     ["ugrep", "-lC3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "measured depth 1: the 3 is -C's context"),
    ("ugrep-after-context-value-is-not-a-depth",
     ["ugrep", "-lA3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "same, -A"),
    ("ugrep-optional-numeric-arg-m",
     ["ugrep", "-lm3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-m takes [MIN,][MAX]: cannot be a value_flag"),
    ("ugrep-optional-numeric-arg-K",
     ["ugrep", "-lK3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-K takes [MIN,][MAX] too"),
    ("ugrep-optional-numeric-arg-Z",
     ["ugrep", "-lZ3", "pat", "/scratch"],
     "/var/tmp", ALLOW, "-Z is --fuzzy[=[best][+-~][MAX]]"),
    ("ugrep-a-glued-context-value-then-a-real-depth",
     ["ugrep", "-C9", "-l1", "pat", "/scratch"],
     "/var/tmp", ALLOW,
     "the 9 is -C's and the 1 is the bound; taking the 9 refuses a bounded walk"),
    ("ugrep-a-separate-context-value-then-a-real-depth",
     ["ugrep", "-C", "9", "-l1", "pat", "/scratch"],
     "/var/tmp", ALLOW, "the separate spelling of the same collision"),

    # --- operand grammar, and the optional-argument trap -------------------
    ("ugrep-pattern-flag-frees-the-first-positional",
     ["ugrep", "-r", "/scratch", "-e", "pat"],
     "/var/tmp", REFUSE, "a pattern flag AFTER the path; the late-pattern-flag defect's shape"),
    ("ugrep-exclude-dir-value-is-not-a-root",
     ["ugrep", "-r", "--exclude-dir", "/scratch", "pat", "/var/log"],
     "/var/tmp", ALLOW, "--exclude-dir takes a required value"),
    ("ugrep-repeated-exclude-dir",
     ["ugrep", "-r", "--exclude-dir=.git", "--exclude-dir=.svn",
      "pat", "/scratch"],
     "/var/tmp", REFUSE, "REPEATED: attached values change no root"),
    ("ugrep-ignore-files-consumes-nothing",
     ["ugrep", "-r", "--ignore-files", "pat", "/scratch"],
     "/var/tmp", REFUSE,
     "OPTIONAL argument, like grep's --color: listing it eats `pat`, then "
     "spends /scratch on skip_pos and allows the walk"),
    ("ugrep-ignore-files-with-its-attached-value",
     ["ugrep", "-r", "--ignore-files=x", "pat", "/scratch"],
     "/var/tmp", REFUSE, "the attached spelling is the only one it takes"),
    ("ugrep-does-not-abbreviate",
     ["ugrep", "--recurs", "pat", "/scratch"],
     "/var/tmp", ALLOW, "`invalid option --recurs`: the command walks nothing"),
    ("ugrep-depth-after-dashdash-is-a-pattern",
     ["ugrep", "-r", "--", "--depth=1", "/scratch"],
     "/var/tmp", REFUSE, "after `--` the depth flag is the PATTERN"),
    ("ugrep-cheap-root-is-untouched",
     ["ugrep", "-r", "pat", "/var/log"],
     "/var/tmp", ALLOW, "ext4 is cheap and unbounded is fine there"),
    ("ugrep-relative-operand-on-a-fast-cwd",
     ["ugrep", "-r", "pat", "runs"],
     FAST_CWD, REFUSE, "relative operand joined to the cwd sentinel"),
    ("ugrep-relative-operand-bounded-on-a-fast-cwd",
     ["ugrep", "-l", "pat", "runs"],
     FAST_CWD, ALLOW, "...and the same operand at the depth-1 default"),
]
