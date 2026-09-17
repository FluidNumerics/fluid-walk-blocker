"""Rule table for both guardrail layers: which invocations walk an expensive
filesystem without a bound, and which processes on the node are doing so now.

This module is the single source of truth. Two consumers read it:

  * `walk-blocker build` compiles the argv profiles below, together with the
    site's `site.toml`, into the POSIX `sh` dispatcher that runs on the node's
    PATH (ADR-0013, ADR-0015). The shim cannot import Python, so the table is
    *generated* into it and a test asserts the two agree row by row. A shim
    that blocks what the reaper does not report -- or the reverse -- is worse
    than either layer alone.
  * the reaper imports this module directly, for the tool-name sets, the
    transparent-wrapper list and the traversal grammar.

Because the reaper imports it, this file is copied verbatim into the node
payload and obeys the node's constraint (ADR-0015): stdlib-only, Python 3.9
syntax, no import from the `walk_blocker` package, no PEP 723 header.

Nothing here is a site fact. Every threshold, filesystem type and per-mount
override arrives in a `Policy`, which the build compiles from `site.toml` and
the suite constructs directly. The only environment this module reads is the
two audited test seams in `Policy.from_env` (ADR-0001, ADR-0013).

Layer 1 -- the shim generated from this table -- is ADVISORY. A PATH shim is
bypassed by an absolute path, a private PATH, a container, a scheduler job
script or a shell function; that is why Layer 2 exists (ADR-0001). The grammar
below is nonetheless held to the standard of being right about the user's
command, because a refusal that is wrong teaches people to alias around the
guard, after which it is off for them on every command.

Provenance: the subtree test, `clean_path`, the "bounded" threshold and
`GENERIC_WRAPPERS` are ported from Fluid Numerics' own session-level guard --
the fn-guardrails plugin's `block-unscoped-find` and `command_parse` hooks --
where each was added after a specific miss. The per-tool profiles are a
revived and much extended form of that guard's per-tool specs. Deliberately
not ported: the shell-quoting, heredoc and newline machinery. The shim is
handed argv after the shell has expanded it and needs none of it; copying it
in would import a false-positive surface for no gain -- and see ADR-0001 for
why reading commands as text has a ceiling this layer sits above.
"""

import fnmatch
import os
import re

# --------------------------------------------------------------------------
# Fixed constants -- properties of the design, not of any site
# --------------------------------------------------------------------------

# Refusals exit 2, never 1: grep uses 1 for "no match", and a caller must never
# read a block as an empty result.
EXIT_REFUSED = 2

# Environment variable that turns a refusal into an audited allow. A block with
# no override gets routed around with `\find`, and then nothing is measurable.
# This is the documented escape hatch of an advisory guard (ADR-0001), not a
# test seam.
ESCAPE_HATCH = "WALK_BLOCKER_UNSCOPED"

# The two runtime test seams this module honours, and the only environment it
# reads (in Policy.from_env). Both are audited by the shim when they change an
# outcome, and neither gains a sibling that carries a site value (ADR-0013).
FSTYPES_SEAM = "WALK_BLOCKER_FSTYPES"
DEPTH_BY_MOUNT_SEAM = "WALK_BLOCKER_DEPTH_BY_MOUNT"

# The directory actions that mean "this invocation walks a tree".
# "recurse_default" is never set BY a flag -- only a profile's
# dir_action_default carries it -- so an action still reading it is the signal
# that nothing has overridden the tool's own behaviour. See Profile.
RECURSE_ACTIONS = ("recurse", "recurse_default")

# Wrappers that run their remaining arguments as another command and never
# change who it runs as. Ported from the session guard's command parser; the
# reaper walks the parent chain through these so `xargs -P16 find` attributes
# to the real parent. sudo/doas/su are deliberately absent: a caller for whom
# the wrapper itself is interesting must pass its own list.
GENERIC_WRAPPERS = ("env", "command", "builtin", "exec", "time",
                    "nice", "ionice", "nohup", "stdbuf", "xargs")

GENERIC_WRAPPER_VALUE_FLAGS = {
    "xargs": ("-n", "-P", "-I", "-d", "-s", "-E", "--max-args", "--replace"),
    "nice": ("-n",),
    "ionice": ("-c", "-n", "-p"),
    "stdbuf": ("-i", "-o", "-e"),
}


def is_stream_filter(argv, stream_filters):
    """Is this a tool that provably cannot walk a tree?

    `stream_filters` is the site's `[reaper].stream_filters` set -- tools
    that cannot walk a directory tree under ANY argv, consulted by the
    reaper's `opaque_traversal` arm so that waiting is not mistaken for
    walking. `tail -F` is the canonical member: GNU tail has no recursion
    option at all (`tail --help` names none, and a directory operand
    produces nothing -- measured, coreutils 8.32), so no spelling of it is a
    traversal. See ADR-0010. Membership is evidence-backed, one name at a
    time, and lives in site config because which tools appear in a site's
    audit trail is a fact about that site.

    Keyed on argv[0] alone, never on `comm` -- the rule tool_from_argv()'s
    docstring states. argv[0] is itself forgeable (`exec -a tail ...`), and
    that is acceptable HERE and would not be in a killing decision: the only
    arm that consults this never kills, so the cost of a forged exclusion is
    one missing audit record, never a wrongful kill. An exclusion keyed on
    the narrower field is also the narrower exclusion, which is the
    direction this one should err.
    """
    if not argv or not argv[0]:
        return False
    return argv[0].rsplit("/", 1)[-1] in stream_filters


# --------------------------------------------------------------------------
# Policy -- everything a site decides, carried as one value
# --------------------------------------------------------------------------

class Policy(object):
    """The site's answers to the questions the grammar cannot decide alone.

    remote_fstypes      -- filesystem types whose mounts are expensive by
                           default: a tuple of `fnmatch` patterns, so
                           `fuse.*` covers every FUSE gateway. Order is
                           severity, worst first; offending_mount() uses it
                           to choose which mount a refusal NAMES when a walk
                           descends into several. `[filesystems].remote_fstypes`.
    remote_proxy        -- when True, a mount whose type matches nothing is
                           still expensive if its /proc/mounts fields say it
                           is remote: a `host:` source, or `_netdev`/`addr=`
                           among its options. `[filesystems].remote_proxy`.
    maxdepth_allowed    -- the global ceiling: a walk bounded to this depth
                           or shallower is allowed on EVERY mount.
                           `[filesystems].maxdepth_allowed`.
    unscoped_depth      -- how far below an expensive mount point a root must
                           sit before an UNBOUNDED walk from it is scoped
                           work. `[filesystems].unscoped_depth`. 1 -- refuse
                           only the mount point itself -- is the tempting
                           value and was wrong at a reference deployment: an
                           unbounded `grep -rIn ... /home/someone` blocked in
                           D state is a recurring shape, and one user's home
                           on a parallel filesystem is not a bound.
    depth_allowance_max -- the most any per-mount maxdepth may be; an
                           override above it is dropped, not clamped, so a
                           fat-fingered value fails toward the restrictive
                           answer. `[filesystems].depth_allowance_max`.
    mounts              -- {mount_path: (class, maxdepth_or_None)}, the
                           compiled `[[filesystems.mounts]]` table: an exact
                           mount-point override of the class tier-one would
                           assign, optionally with the deepest -maxdepth
                           that still counts as bounded on that mount.
                           Absence is safe -- an unlisted mount takes the
                           tier-one default and the global ceiling -- which
                           is what keeps this an override rather than a
                           dependency (ADR-0007, ADR-0016).

    A plain value object. It is constructed by the build from `site.toml`
    and by the suite directly; nothing on the node reads configuration
    (ADR-0013). Ordering within `mounts` is irrelevant; every lookup is by
    exact mount point.
    """

    __slots__ = ("remote_fstypes", "remote_proxy", "maxdepth_allowed",
                 "unscoped_depth", "depth_allowance_max", "mounts")

    def __init__(self, remote_fstypes, remote_proxy, maxdepth_allowed,
                 unscoped_depth, depth_allowance_max, mounts):
        self.remote_fstypes = tuple(remote_fstypes)
        self.remote_proxy = bool(remote_proxy)
        self.maxdepth_allowed = int(maxdepth_allowed)
        self.unscoped_depth = int(unscoped_depth)
        self.depth_allowance_max = int(depth_allowance_max)
        self.mounts = {}
        for mount, (cls, maxdepth) in dict(mounts).items():
            assert cls in ("expensive", "cheap"), (mount, cls)
            self.mounts[mount.rstrip("/") or "/"] = (
                cls, None if maxdepth is None else int(maxdepth))

    def replace(self, **changes):
        """A copy with the named fields replaced."""
        fields = {name: getattr(self, name) for name in self.__slots__}
        fields.update(changes)
        return Policy(**fields)

    def __repr__(self):
        return "Policy(%s)" % ", ".join(
            "%s=%r" % (name, getattr(self, name)) for name in self.__slots__)

    @classmethod
    def from_env(cls, defaults):
        """`defaults` with the two runtime TEST SEAMS applied, as a copy.

        These are the audited seams of ADR-0001 and ADR-0013, not
        configuration: they exist so the suite can drive both consumers
        against a fixture without a real mount table, and the shim audits
        either when it changes an outcome. Neither carries a site value
        into the node -- a site's values arrive compiled, in `defaults`.

          WALK_BLOCKER_FSTYPES         colon-separated fnmatch patterns that
                                       REPLACE `remote_fstypes` when set and
                                       non-empty.
          WALK_BLOCKER_DEPTH_BY_MOUNT  `/mount=N` pairs, colon-separated,
                                       that REPLACE every per-mount maxdepth
                                       when the variable is present at all
                                       (an empty value clears them). Parsed
                                       by _parse_depth_by_mount(): an entry
                                       out of range is DROPPED toward the
                                       restrictive answer, exactly as the
                                       runtime shim treats it. The build-time
                                       config fails loudly on the same input
                                       instead; that is config.py's job, and
                                       the difference is deliberate -- a
                                       seam must never widen what a compiled
                                       value permits.

        This is the ONLY place in this module that reads the environment.
        """
        policy = defaults
        raw_types = os.environ.get(FSTYPES_SEAM, "")
        types = tuple(f for f in raw_types.split(":") if f)
        if types:
            policy = policy.replace(remote_fstypes=types)
        if DEPTH_BY_MOUNT_SEAM in os.environ:
            parsed = _parse_depth_by_mount(os.environ[DEPTH_BY_MOUNT_SEAM],
                                           policy.depth_allowance_max)
            mounts = {m: (c, None) for m, (c, _d) in policy.mounts.items()}
            for mount, depth in parsed.items():
                cls_, _old = mounts.get(mount, ("expensive", None))
                mounts[mount] = (cls_, depth)
            policy = policy.replace(mounts=mounts)
        return policy


def _parse_depth_by_mount(raw, allowance_max):
    """{mount: depth} from `/mount=N` pairs, colon-separated.

    An override outside `0..allowance_max` is a typo, not a policy, and it is
    DROPPED rather than clamped, so the mount falls back to the global
    ceiling: a fat-fingered `/home=444` must fail toward the restrictive
    answer, never toward an effectively unbounded one. Above the maximum the
    ceiling stops bounding anything that matters on a large filesystem --
    measured at a reference deployment -- and below 0 is meaningless. If a
    genuinely deeper walk is wanted, that is what walk-job is for.
    """
    out = {}
    for pair in raw.split(":"):
        if not pair:
            continue
        mount, sep, depth = pair.partition("=")
        # An absolute path or nothing: the key is compared against a mount
        # point from /proc/mounts, so anything else can never match and is a
        # typo worth dropping rather than storing.
        if not sep or not mount.startswith("/"):
            continue
        try:
            value = int(depth)
        except (TypeError, ValueError):
            continue
        if not 0 <= value <= allowance_max:
            continue
        out[mount.rstrip("/") or "/"] = value
    return out


def depth_allowance(mount, policy):
    """The deepest -maxdepth that counts as bounded on this mount.

    The allowance is keyed on the MOUNT, so it loosens the mount point itself
    as well as directories inside it: with `/home` at 4, `find /home -maxdepth
    4` is allowed, not just `find /home/someone -maxdepth 4`. That is
    deliberate (ADR-0007): bounded and finite is the property this guard
    cares about, and a depth-bounded walk of a whole home mount, measured at
    a reference deployment, finished in minutes rather than the hours an
    unbounded walk takes. The alternative was to gate the override on
    depth_below >= 1; it is pinned by a test whose name says the mount point
    is covered on purpose.
    """
    entry = policy.mounts.get(mount)
    if entry is not None and entry[1] is not None:
        return entry[1]
    return policy.maxdepth_allowed


_REMOTE_SOURCE = re.compile(r"^[^/]+:")


def classify_mount(entry, policy):
    """"expensive" or "cheap" for one /proc/mounts row, from its fields alone.

    `entry` is `(source, mountpoint, fstype, options)` -- the first four
    fields of a /proc/mounts line, in that order. Three tiers (ADR-0016), of
    which only the first two run here:

      1. an exact `policy.mounts[mountpoint]` override wins outright;
      2. else the type matches one of `policy.remote_fstypes` (fnmatch, so
         `fuse.*` works) -> expensive;
      3. else, with `policy.remote_proxy`, a source spelled `host:...` or a
         `_netdev` / `addr=` option marks the mount remote -> expensive;
      4. else cheap.

    So an UNKNOWN REMOTE type is guarded -- a new parallel filesystem, a
    vendor's renamed client, a FUSE gateway in front of an object store each
    appears with a type no list anticipated, and each is exactly the resource
    an unbounded walk hurts -- while an unknown LOCAL type (`tmpfs`, an
    overlay, a loop mount) is cheap. The false refusal on a small remote
    export that this produces is a configuration omission, fixed by an
    override in `[[filesystems.mounts]]`, never by narrowing the type list.

    This function reads the policy and contains no path. It is generated into
    the shim as well, and a test asserts the two consumers agree on every row
    of the fixture table, branch by branch. No measurement happens here: a
    `statfs` would block on exactly the filesystem being judged (ADR-0015).
    """
    source, mountpoint, fstype, options = entry[0], entry[1], entry[2], entry[3]
    override = policy.mounts.get(mountpoint.rstrip("/") or "/")
    if override is not None:
        return override[0]
    for pattern in policy.remote_fstypes:
        if fnmatch.fnmatchcase(fstype, pattern):
            return "expensive"
    if policy.remote_proxy:
        if _REMOTE_SOURCE.match(source):
            return "expensive"
        for opt in options.split(","):
            if opt == "_netdev" or opt.startswith("addr="):
                return "expensive"
    return "cheap"


def _severity(fstype, policy):
    """Rank of `fstype` in `policy.remote_fstypes`, worst first; unlisted
    types (remote by proxy, or an override) rank after every listed one."""
    for i, pattern in enumerate(policy.remote_fstypes):
        if fnmatch.fnmatchcase(fstype, pattern):
            return i
    return len(policy.remote_fstypes)


# --------------------------------------------------------------------------
# Per-tool argv profiles
# --------------------------------------------------------------------------

class Profile(object):
    """How one tool's argv names the thing it is about to walk.

    always      -- the tool traverses whether or not a flag says so
    rec_flags   -- flags that turn a non-traversing tool into a traversing one
    rec_value_flags -- (flag, values) pairs whose VALUE turns traversal on.
                   `values` is a TUPLE, because one flag can have more than
                   one on-value: ugrep's -d takes `recurse` AND
                   `dereference-recurse`, and modelling that as two (flag,
                   value) pairs is not equivalent -- every consumer here
                   short-circuits on the first pair whose FLAG matches, so
                   the second value is never compared and reads as "skip".
    depth_flags -- flags that bound the walk; value is compared to the
                   policy's ceiling for the mount
    device_flags -- flags that bound the walk by DEVICE instead of by depth:
                   the walk never leaves the filesystem each root sits on
    device_off_flags -- spellings that turn that back off; last one wins
    value_flags -- flags consuming the NEXT argv element, which is therefore not a root
    pat_flags   -- flags supplying the search pattern, so no positional is spent on it
    skip_pos    -- leading positionals that are the pattern, not a root
    root_mode   -- "leading"  : operands before the first flag (find's grammar)
                   "positional": operands anywhere, after skip_pos
                   "stdin"    : walks cwd only when stdin is a terminal (fzf)
    root_flags  -- flags whose value IS a root (fzf --walker-root)
    default_root-- what the tool walks when given no path
    exact_flags -- real options of THIS tool that consume no following token
                   and are a proper prefix of one of its value_flags, so
                   abbreviation resolution must not treat them as one
    abbreviates -- does the tool accept unique-prefix long options at all?
                   Only GNU getopt_long does. Measured on the tools.
    exec_flags  -- flags introducing a COMMAND TEMPLATE, whose tokens are
                   data rather than predicates, roots or bounds
    cwd_flags   -- flags that change the directory everything else is
                   resolved against (fd's --base-directory), which is NOT the
                   same as supplying a root
    exec_plus_flags -- the subset that also ends at a `{} +`
    dir_action_default -- for tools with an order-sensitive directory ACTION
                   (grep's -d/--directories, and -r which sets it): the action
                   in force before any flag. None for tools without the
                   grammar. Three legal values. "skip" and "recurse" are the
                   actions a flag can set; "recurse_default" is a third that
                   only ever appears as a DEFAULT -- it means this tool
                   recurses a directory operand because it is that tool and
                   not because a flag said so, which is ugrep. Traversal is
                   "recurse" OR "recurse_default"; the default depth below is
                   in force only while the action is still "recurse_default",
                   i.e. while nothing has set it. That is why there is no
                   separate list of flags that clear the default: which flags
                   those are is exactly what _last_dir_action() already
                   parses, in both orders, for every spelling, with last-wins.
    default_depth -- the depth this tool applies to a directory operand on its
                   own, with no depth flag and nothing having set the action.
                   None (every other profile) means unbounded, which is what
                   "no depth flag" has always meant. 1 for ugrep: `ugrep PAT
                   DIR` lists DIR's own files and does not descend (measured
                   on 7.8.4). Modelling that as "not a traversal" gives the
                   right verdict for the wrong reason and leaves
                   `ugrep --depth=9 PAT /big` allowed.
    depth_enables_traversal -- a depth flag makes this a traversal whatever
                   the directory action says, and is NOT subject to its
                   last-wins. Measured on ugrep 7.8.4: `-d skip --depth=9`
                   and `--depth=9 -d skip` both walk the whole tree, while
                   `-d skip` alone walks nothing. ugrep --help says --depth
                   "Enables -r if -R or -r is not specified"; what it does
                   not say, and what was measured, is that it also overrides
                   an explicit skip.
    depth_range -- a depth VALUE may be spelled `[MIN,]MAX`, and the MAX is
                   what bounds the walk. `--depth=3,5` and `--depth=,5` are
                   valid and walk five levels; `--depth=2,` is rejected by
                   the tool and walks nothing. Without this the long range
                   spelling reads as malformed (ALLOW) while the short one
                   `-3-5` is caught by depth_digits -- one spelling of a rule
                   implemented and its twin not.
    depth_digits -- the characters that may appear in a short cluster whose
                   DIGIT RUNS are depths: ugrep spells `--depth=N` as `-N`,
                   and the digits cluster with letters (`-l9`, `-9l`, `-rl2`,
                   `-l10` which is TEN and not `-1` then `-0`). A frozenset
                   and not a bool, because a digit run is only a depth when
                   nothing earlier in the token could have eaten it: `-K3`,
                   `-m3`, `-Z3` and `-Q3` are optional-argument flags whose
                   argument is numeric, so they cannot be listed in
                   value_flags (the --color rule) and parse_short_cluster
                   leaves their digits among the letters. Any character
                   outside this set means the token is not scanned at all, so
                   an unaudited flag loses a bound rather than inventing one.
                   Required-value flags need no entry here: those ARE in
                   value_flags, so their argument arrives as `glued`, which
                   is never scanned.
    fallback_needs_tty -- the default_root fallback applies only when stdin is
                   a terminal. ugrep with no FILE operand reads stdin on a
                   pipe and walks nothing, but recurses the working directory
                   -- UNBOUNDED, an implicit -r, not the depth-1 default --
                   when stdin is a terminal. Distinct from root_mode="stdin"
                   (fzf), which never charges positional operands at all.
    cluster_letters -- the single letters that may appear together in a
                   getopt-style cluster (`-HL`), or None if this tool's
                   clustering is unrestricted (every wrapped tool but
                   find/bfs/gfind today). Restricting it is what lets a
                   token be split into flags at all without a long predicate
                   that happens to start with one of the same letters
                   (`-regextype`) being scanned letter-by-letter too. See
                   parse_short_cluster().
    dashdash    -- what `--` means to a LEADING-mode tool, which is the one
                   place find and bfs genuinely disagree:
                     "predicate"      -- find. `--` before any operand is
                        skipped, but after one it is an unknown predicate:
                        `find d1 -- d2` exits 1 with "unknown predicate `--'"
                        and walks NOTHING.
                     "end_of_options" -- bfs. `--` is a real end-of-options
                        marker wherever it appears and operands CONTINUE
                        through it: `bfs d1 -- d2` exits 0 and walks BOTH.
                   Measured on bfs 2.1, 2.3.1 and 4.0.4 and identical in
                   all three. (An earlier note here inferred a bfs version
                   from a process's comm. That was wrong: the string in comm
                   belonged to the agent whose shell wrapper had exec'd bfs,
                   not to bfs -- see tool_from_argv().) Ignored outside
                   leading mode.
    wrapped     -- does Layer 1 get a PATH shim for this tool by default?
                   False for fzf/sk: fzf judges the cwd on every interactive
                   invocation (`always=True`, `root_mode="stdin"`), which is
                   usually reached through a shell keybinding or an editor
                   plugin, where an `exit 2` refusal is an invisible no-op
                   rather than a teaching moment -- unlike a refusal typed at
                   a prompt. This is the table's default; the site's
                   `[shim].unwrapped` list is what wrapped_names() actually
                   applies, and it never touches PROFILES/PROFILE_BY_NAME, so
                   Layer 2 (the reaper, keyed on PROFILE_BY_NAME) sees every
                   profile regardless.
    """

    def __init__(self, names, always=False, rec_flags=(), rec_value_flags=(),
                 depth_flags=(), value_flags=(), pat_flags=(), skip_pos=0,
                 root_mode="positional", root_flags=(), default_root=".",
                 exact_flags=(), abbreviates=False, exec_flags=(),
                 exec_plus_flags=(), dir_action_default=None, cwd_flags=(),
                 device_flags=(), device_off_flags=(),
                 output_only_depth_flags=(), dashdash="predicate",
                 cluster_letters=None, wrapped=True, default_depth=None,
                 depth_enables_traversal=False, depth_range=False,
                 depth_digits=None, fallback_needs_tty=False):
        # A bound by DEVICE rather than by depth. Depth was the only bound this
        # table recognised, so the standard way of steering a walk AWAY from an
        # expensive filesystem was invisible to it and `find / -xdev` -- which
        # never enters the expensive mounts at all -- was refused for
        # descending into them. A refusal that is wrong about the user's
        # command is what teaches people to alias `/usr/bin/find` permanently,
        # after which Layer 1 is off for them on every command.
        #
        # NOT a global "bounded": it holds only for a root that is not itself
        # on an expensive mount. `find /big -xdev` enumerates the whole mount
        # and is still refused, which is why this is applied per root in
        # judge_root() rather than short-circuiting like depth does.
        #
        # `-prune` is deliberately absent. It bounds a walk too, but only as
        # part of an expression whose effect depends on the operators around
        # it (`-path X -prune -o -print`), and modelling that means parsing
        # find's expression grammar rather than scanning argv. Refusing a
        # correctly pruned walk is a false refusal this does not fix.
        self.device_flags = device_flags
        # Last-wins, like every other setting in this table. Measured with rg
        # 13.0.0 against a local directory holding two network mounts:
        #   rg --files --one-file-system DIR                       (nothing)
        #   rg --files --one-file-system --no-one-file-system DIR  descended
        #       into a mount
        #   rg --files --no-one-file-system --one-file-system DIR  (nothing)
        # Only rg has the negation; fd 8.3.1, du 8.32, tree and findutils 4.8
        # have no spelling that turns theirs back off.
        # Flags a tool accepts that look like a depth bound and are not,
        # because they prune OUTPUT rather than traversal. Kept as data because
        # it is a fact about the tool, and because the refusal has to name them:
        # a user who typed `-d 2` and got refused is owed the reason, and "your
        # tool has no depth flag" reads like a lie to someone holding one.
        self.output_only_depth_flags = output_only_depth_flags
        self.device_off_flags = device_off_flags
        # A COORDINATE SYSTEM, not a root. An earlier review modelled fd's
        # --base-directory as a root_flag, which is wrong in both directions:
        # with an explicit operand fd walks the OPERAND (resolved against the
        # base) and not the base, so recording the base allowed
        # `fd --base-directory /tmp/cheap pat ../big` -- which fd walks as
        # /big -- and refused `fd --base-directory /big pat /tmp/cheap`, which
        # fd walks as /tmp/cheap. Verified on the tool: `fd --base-directory
        # BASE patfile sub` found `sub/patfile`, which existed only under the
        # base.
        self.cwd_flags = cwd_flags
        # GNU grep's directory handling is LAST-WINS, not first-wins, and -r
        # is one of the actions rather than a separate switch. Measured:
        #   grep -r -d skip            does NOT descend
        #   grep -d recurse -d skip    does NOT descend
        #   grep -d skip -r            DOES descend
        #   grep -rd skip              does NOT descend
        # Returning True on the first -r therefore refused commands that walk
        # nothing, which on an advisory layer is the failure that teaches
        # people to alias around it.
        self.dir_action_default = dir_action_default
        # A command template is neither a predicate nor a root nor a bound,
        # and every scanner that walks argv has to step over it. find's
        # -exec/-ok and fd's -x/-X are the same shape with different
        # terminators, which is why this is a profile field and not a
        # find-specific branch: `fd -x echo {} /tmp/cheap` walks the CWD with
        # /tmp/cheap as template data (verified on the tool), so charging it
        # as a root hid a walk of an expensive cwd.
        self.exec_flags = exec_flags
        self.exec_plus_flags = exec_plus_flags
        self.names = names
        # Whether `--rec` is a spelling of `--recursive` for THIS tool.
        # getopt_long does that; clap and hand-rolled parsers do not, and
        # modelling a grammar the tool lacks is not free -- it made
        # `rg --rege pat /home` a refusal for walking /home when real rg only
        # answers "error: Found argument '--rege' which wasn't expected".
        # Measured on the tools:
        #   accept a unique prefix : grep egrep fgrep zgrep rgrep du
        #   reject one             : find (own predicate parser), rg and fd
        #                            (clap), tree, fzf, ugrep
        self.abbreviates = abbreviates
        # See resolve_long_flag(). Enumerated mechanically from `--help`
        # rather than by hand: for each value_flag, every real option that
        # is a proper prefix of it and does NOT consume the next token. Across
        # every abbreviating tool that is exactly two -- grep's `--binary`
        # (prefix of --binary-files) and du's `--time` (prefix of
        # --time-style, and an OPTIONAL-argument option, which is why it never
        # consumes). Both were also hit as live defects before being
        # enumerated.
        self.exact_flags = exact_flags
        self.always = always
        self.rec_flags = rec_flags
        # (flag, value) pairs where that VALUE is what enables the walk:
        # grep's `-d recurse` is exactly `-r`, spelled as an action.
        self.rec_value_flags = rec_value_flags
        self.depth_flags = depth_flags
        self.value_flags = tuple(value_flags) + tuple(pat_flags) + tuple(depth_flags) + tuple(root_flags)
        self.pat_flags = pat_flags
        self.skip_pos = skip_pos
        self.root_mode = root_mode
        self.root_flags = root_flags
        # Asserted rather than trusted: a typo here would silently pick find's
        # reading for bfs, which is the bug this field exists to fix.
        assert dashdash in ("predicate", "end_of_options"), dashdash
        assert dir_action_default in (
            None, "skip", "recurse", "recurse_default"), dir_action_default
        # `always` is unreachable behind a directory action on this side --
        # is_traversal() tests the action FIRST -- while the shim skips its
        # whole action ladder when its always flag is set. Setting both makes
        # the two consumers disagree about `-d skip`, which the argv-grammar
        # instructions call worse than the parsing bug underneath it. rgrep
        # learned this once already; the assert is so ugrep cannot relearn it.
        assert not (always and dir_action_default), (
            names, "always is unreachable behind a directory action")
        # A default depth only means anything for a tool whose traversal is
        # something it does by default, which is what the third action value
        # records. Without the action there is nothing to tell "the tool's own
        # behaviour" from "a flag asked for this".
        assert (default_depth is None) or (
            dir_action_default == "recurse_default"), names
        self.default_depth = default_depth
        self.depth_enables_traversal = depth_enables_traversal
        self.depth_range = depth_range
        self.depth_digits = depth_digits
        self.fallback_needs_tty = fallback_needs_tty
        self.dashdash = dashdash
        self.default_root = default_root
        self.cluster_letters = cluster_letters
        self.wrapped = wrapped


# find(1) and CLI-compatible relatives, as ONE set of kwargs used by two
# profiles. They were a single Profile until `--` turned out to mean different
# things to find and to bfs, which a shared object cannot express. Kept as one
# dict rather than two literals precisely because this table's recurring
# defect is two consumers drifting apart: the ONLY things find and bfs may
# differ on are what is passed explicitly beside `**_LEADING` -- `dashdash`
# and `root_flags` (`-f` is bfs's, not find's).
_LEADING = dict(
            exec_flags=("-exec", "-execdir", "-ok", "-okdir"),
            # `+` ends only `-exec/-execdir ... {} +`; -ok/-okdir take only
            # `;`, verified as "find: missing argument to `-ok'".
            exec_plus_flags=("-exec", "-execdir"),
            always=True,
            depth_flags=("-maxdepth",),
            # Measured with findutils 4.8.0: `find DIR -xdev` and `find DIR
            # -mount`, on a local directory holding network mounts, both
            # printed the mount points and descended into none of them. Both
            # are global options, so position in the expression does not
            # matter.
            # -x is bfs's spelling of -xdev (`bfs --help`). It used to be
            # deliberately absent here: a single-letter `-x` made every
            # `bfs -regextype ... /` read as device-bounded and ALLOWED,
            # because device letters are scanned out of short clusters and
            # `-regextype` contains an `x`. `cluster_letters` below closes
            # that -- `-regextype` no longer parses as a cluster at all, so
            # its embedded `x` is never scanned.
            device_flags=("-xdev", "-mount", "-x"),
            # A getopt-style cluster (`-HL`) may combine these letters and
            # only these -- taken from FIND_PREFIX_FLAGS below, as a literal
            # because this dict is built before that constant exists in file
            # order; test_cluster_letters_matches_find_prefix_flags pins the
            # two together. Restricting clustering to real boolean prefix
            # flags is what lets `-HL` split into `-H`+`-L` (both real, bfs
            # accepts clusters where GNU find calls `-HL` an unknown
            # predicate) while a long predicate that happens to start with
            # the same letters, `-regextype`, is read as one opaque token
            # instead of nine single-letter ones.
            cluster_letters=frozenset("HLPEXdsx"),
            # So _scan_bounds() steps over its value too. Nothing here is a
            # depth flag, but a value that happens to look like one must not
            # be read as a bound. -maxdepth is folded in by Profile from
            # depth_flags, so it is deliberately not repeated.
            value_flags=("-D", "-S", "-regextype"),
            root_mode="leading")

PROFILES = (
    # gfind is GNU find under a different argv[0]. `--` is an unknown
    # PREDICATE to find once an operand has been seen -- `find d1 -- d2` exits
    # 1 and walks nothing -- so the operand list ends there.
    #
    # root_flags=() (the default): `-f` is bfs's, not find's -- GNU find
    # rejects it in every arrangement (`find -f d1`, `find - -f d1`,
    # `find -- -f d1`, all "unknown predicate `-f'", rc=1, findutils 4.8.0)
    # and walks nothing. The two profiles shared `_LEADING`'s root_flags
    # once, so find silently inherited bfs's flag.
    Profile(("find", "gfind"), dashdash="predicate", **_LEADING),

    # bfs is an intentional superset including -maxdepth, and it is the only
    # wrapped tool that really implements `--`: `bfs d1 -- d2` exits 0 and
    # walks BOTH operands, so the list continues through it. Measured
    # identically on bfs 2.1, 2.3.1 and 4.0.4.
    #
    # `-f PATH` -- "treat PATH as a path to search (useful if begins with a
    # dash)" -- really is bfs's own root flag (`bfs --help`), so it stays
    # here as the one field, besides `dashdash`, allowed to differ from
    # find/gfind. A root the scan never charged, so the walk fell back to the
    # cwd and the named path went unjudged.
    #
    # It is also a tool people install THEMSELVES, into homes the installer
    # cannot read. The installer links only names on its own PATH, so where
    # bfs is a private build no shim is ever linked and Layer 1 does not
    # cover it -- which makes this table the only consumer that sees it, and
    # this the table half of a live gap rather than a symmetry nicety.
    Profile(("bfs",), dashdash="end_of_options", root_flags=("-f",), **_LEADING),

    # grep has NO depth flag. A recursive grep of an expensive mount
    # therefore cannot be bounded and is always refused; the refusal names
    # `rg --max-depth` instead. This is the shape that recurs, not a
    # hypothetical.
    Profile(("grep", "egrep", "fgrep", "zgrep"),
            rec_flags=("-r", "-R", "--recursive", "--dereference-recursive"),
            # `grep -d recurse` walks exactly like -r -- verified against
            # grep(1). Because -d is value-consuming, the generic value skip
            # discarded `recurse` and the invocation read as non-recursive.
            rec_value_flags=(("-d", ("recurse",)),
                             ("--directories", ("recurse",))),
            pat_flags=("-e", "--regexp", "-f", "--file"),
            # NB: --color/--colour are absent on purpose. grep spells them
            # `--color[=WHEN]` -- an OPTIONAL argument, which is never the
            # following token. Listing them here made the pending-value rule
            # swallow the next argv element, so `grep --color -r pat /home`
            # read as non-recursive and took the fast path: a Layer 1 bypass,
            # verified against grep(1), which descends. rg and fd spell their
            # equivalents with a REQUIRED value, so theirs stay listed.
            value_flags=("-m", "--max-count", "-A", "--after-context",
                         "-B", "--before-context", "-C", "--context",
                         "--include", "--exclude", "--exclude-dir",
                         "-d", "--directories",
                         "--binary-files", "--label",
                         # Audited against `grep --help` after a review found
                         # `--group-separator` missing: every long option grep
                         # documents with a REQUIRED `=VAL` is here now. The
                         # gap was a bypass, not a cosmetic omission --
                         # `grep --group-separator -- -r needle DIR` has the
                         # first `--` as the separator VALUE, so -r stays live
                         # and grep(1) recursed, while the scan read that `--`
                         # as end-of-options and reported no traversal.
                         "--group-separator", "--exclude-from",
                         "-D", "--devices"),
            # `--binary` is a REAL no-argument grep option (-U's long spelling)
            # and only a prefix of `--binary-files`. Without this,
            # `grep --binary -r needle DIR` had the `-r` skipped as
            # --binary-files' value and read as non-recursive -- a Layer 1
            # bypass, verified against grep(1), which recursed.
            exact_flags=("--binary",),
            abbreviates=True,
            # grep does not descend unless a directory action says so.
            dir_action_default="skip",
            skip_pos=1),

    # rgrep is grep -r under another name, so it starts recursive -- but the
    # action is still order-sensitive and `rgrep -d skip` really does not
    # descend (verified on the tool). `always=True` said otherwise and made
    # that a false refusal, so the default action carries it instead.
    Profile(("rgrep",),
            dir_action_default="recurse",
            # The same order-sensitive action grep has -- rgrep is grep -r,
            # and `rgrep -d skip needle DIR` produced no output.
            rec_flags=("-r", "-R", "--recursive", "--dereference-recursive"),
            rec_value_flags=(("-d", ("recurse",)),
                             ("--directories", ("recurse",))),
            pat_flags=("-e", "--regexp", "-f", "--file"),
            value_flags=("-m", "--max-count", "-A", "--after-context",
                         "-B", "--before-context", "-C", "--context",
                         "--include", "--exclude", "--exclude-dir",
                         # Same audit: rgrep takes grep's options, and this
                         # profile had been carrying a shorter list than
                         # grep's for no reason anyone recorded.
                         "--group-separator", "--exclude-from",
                         "-D", "--devices", "-d", "--directories",
                         "--binary-files", "--label"),
            exact_flags=("--binary",),
            abbreviates=True,
            skip_pos=1),

    Profile(("rg",),
            always=True,
            depth_flags=("--max-depth", "--maxdepth"),
            # rg's own help calls it "similar to find's -xdev or -mount".
            # Measured on the tool; the negation is what makes this last-wins.
            device_flags=("--one-file-system",),
            device_off_flags=("--no-one-file-system",),
            pat_flags=("-e", "--regexp", "-f", "--file"),
            value_flags=("-t", "--type", "-T", "--type-not", "--type-add",
                         "-g", "--glob", "--iglob",
                         "-m", "--max-count", "-A", "--after-context",
                         "-B", "--before-context", "-C", "--context",
                         "--color", "--colors", "-j", "--threads",
                         "--pre", "--path-separator", "-M", "--max-columns",
                         "-E", "--encoding", "--ignore-file", "--sort",
                         "--sortr", "-r", "--replace"),
            skip_pos=1),

    # fd [OPTS] [PATTERN] [PATH...] -- a lone positional is the pattern, so the
    # walk is rooted at cwd.
    Profile(("fd", "fdfind"),
            always=True,
            depth_flags=("-d", "--max-depth", "--maxdepth"),
            # Long spelling only -- fd's `-x` is --exec, not a device bound,
            # and fd 8.3.1 has no --no-one-file-system. Measured on the tool:
            # `fd --one-file-system . DIR` listed the mount points under DIR
            # without entering either.
            device_flags=("--one-file-system",),
            value_flags=("--min-depth", "--exact-depth",
                         "-e", "--extension", "-E", "--exclude",
                         "-t", "--type", "-c", "--color", "-j", "--threads",
                         "-S", "--size", "-o", "--owner",
                         "--changed-within", "--changed-before",
                         "--max-buffer-time", "--search-path", "--path-separator",
                         "--format",
                         "--batch-size", "--ignore-file", "--max-results",
                         "--base-directory",
                         # -x/-X are BOTH value flags and exec_flags, and the
                         # pair is not a contradiction -- it is the grammar.
                         # The SEPARATE spelling consumes the rest of argv as
                         # a template (`fd -x echo {} DIR`), which exec_flags
                         # handles and every scanner tests first, by exact
                         # match. The GLUED spelling takes only its attached
                         # value: `fd -xd1 patfile` errored with
                         # `Command not found: "d1"` AFTER walking to depth 3,
                         # so `d1` was the command and nothing bounded the
                         # walk. Without these here, parse_short_cluster read
                         # `-xd1` as `-x` plus `-d 1` and called it bounded,
                         # and `-xe` as `-e` eating the next token, which lost
                         # the real root. Both measured on the tool.
                         "-x", "--exec", "-X", "--exec-batch"),
            # NOT value_flags only. An earlier review listed -x/-X as
            # one-token consumers and argued that understating the template
            # "errs toward refusing". It does not: a template token charged
            # as a root STOPS the fall-back to the cwd, so `fd -x echo {}
            # /tmp/cheap` from an expensive cwd was allowed while fd walked
            # the cwd. Verified on the tool -- the output was `./deep
            # /tmp/cheap`, once per cwd entry. fd takes `;` and no `+`:
            # `fd -x echo {} ';' target` made `target` the pattern.
            exec_flags=("-x", "--exec", "-X", "--exec-batch"),
            # --search-path is where the walk STARTS, not a passive value.
            # Skipping it as a value fell back to the caller's cwd and let a
            # walk that really begins on an expensive mount look cheap.
            root_flags=("--search-path",),
            # A coordinate system, not a root -- see Profile.cwd_flags. Still
            # value-consuming, so its argument is not read as an operand.
            cwd_flags=("--base-directory",),
            skip_pos=1),

    Profile(("tree",),
            always=True,
            depth_flags=("-L",),
            # `tree -x -L 2 DIR` and the clustered `tree -xL 2 DIR` both
            # stopped at the mount points. tree does not abbreviate, and `-x`
            # is the only spelling its help gives.
            device_flags=("-x",),
            value_flags=("-H", "-T", "-o", "-P", "-I", "--charset",
                         "--filelimit", "--timefmt", "--sort"),
            skip_pos=0),

    # du walks metadata as hard as find does and is routinely reached for on a
    # filesystem whose size is the question being asked.
    Profile(("du",),
            always=True,
            # NO depth_flags, deliberately, and this is the one profile where
            # that is a statement rather than an omission. `du -d N` prunes
            # OUTPUT, not traversal: from du(1), it prints the total for a
            # directory "only if it is N or fewer levels below the command
            # line argument", and a directory's total cannot be known without
            # walking everything beneath it. Measured, getdents64 calls over a
            # six-level tree:
            #
            #   du -d 1            12      du -d 9   12      find (unbounded)  12
            #   find -maxdepth 1    2      tree -L 1  2      rg --max-depth 1   2
            #
            # du reads the same number of directories at every -d. Every other
            # wrapped tool's depth flag genuinely prunes, `tree --du` included,
            # so this is du alone and not a property of depth flags. Modelling
            # -d as a bound made `du -d 2 /big` a false ALLOW that walked the
            # whole mount -- and a false-allow is the kind nobody reports,
            # because it inconveniences no one. See ADR-0007.
            #
            # The flags stay in value_flags below: they are not a BOUND, but
            # they do still take a value, and the operand scan has to step over
            # it. Profile folds depth_flags into value_flags, so dropping the
            # first without restoring the second charges `2` in `du -d 2 DIR`
            # as a path operand -- identically in both consumers, so the
            # agreement tests would not have caught it.
            # `du -x /` is the canonical "what filled the root disk", so
            # refusing it was refusing the question. Measured: `du -x -d1 DIR`
            # and `du --one-file-system -d1 DIR` both reported DIR alone,
            # entering neither of the mounts under it.
            #
            # du abbreviates, so `--one-file-s` and even `--o` are accepted
            # (measured, both bounded). resolve_long_flag() requires two
            # characters after the `--`, so `--o` is not resolved here and the
            # walk reads as unbounded -- a false refusal for a spelling nobody
            # types, and the safe direction. Widening the shared resolver to
            # three-character tokens for it would loosen every other family.
            device_flags=("-x", "--one-file-system"),
            output_only_depth_flags=("-d", "--max-depth"),
            value_flags=("-B", "--block-size", "-t", "--threshold",
                         "--exclude", "--exclude-from", "--files0-from",
                         "--time-style", "-d", "--max-depth"),
            # `--time` is du's own option with an OPTIONAL argument
            # (`--time[=WORD]`), so it consumes nothing, and it is only a
            # prefix of `--time-style`. Same class as grep's --color.
            exact_flags=("--time",),
            abbreviates=True,
            skip_pos=0),

    # ugrep. Often not installed system-wide and not reachable by a PATH
    # lookup in the traffic that made it matter: at least one widely used
    # coding agent bundles its own ugrep and reaches it through a shell
    # FUNCTION running `exec -a ugrep <agent binary> ...`, which beats $PATH
    # and forges argv[0]. Where that is the only ugrep, the installer links
    # no shim for it, exactly as for a private bfs, and this table is the
    # live consumer. It is wrapped anyway, for a packaged install.
    #
    # Why it could not be modelled in the grep vocabulary: a bare directory
    # operand ALREADY recurses, at depth 1, so `always=False` + rec_flags
    # gives the right verdict for the wrong reason ("it does not traverse"),
    # and --depth enables recursion on its own, so `ugrep --depth=9 PAT
    # /big` would be a nine-level walk that no field made a traversal at
    # all. Hence the third directory action and default_depth.
    #
    # Every line of the grammar below was executed against ugrep 7.8.4 on a
    # five-level tree, not read off the manual. See ADR-0011 for the half
    # that is not argv.
    Profile(("ugrep",),
            dir_action_default="recurse_default",
            default_depth=1,
            rec_flags=("-r", "-R", "--recursive", "--dereference-recursive"),
            # -d and --directories each take BOTH on-values. ugrep's own help
            # says -d recurse is equivalent to -r and -d dereference-recurse
            # to -R; measured, both walk the whole tree.
            rec_value_flags=(
                ("-d", ("recurse", "dereference-recurse")),
                ("--directories", ("recurse", "dereference-recurse"))),
            depth_flags=("--depth",),
            depth_enables_traversal=True,
            depth_range=True,
            # The boolean short letters, plus the digits and the two range
            # separators. Built from `ugrep --help`'s own option headers --
            # 13 take a required argument, 5 (-? -K -Q -Z -m) take an OPTIONAL
            # one, 44 take none -- and cross-checked against the tool by
            # re-deriving the set from its behaviour: 31 letters definitely
            # clusterable, 13 definitely not, 0 disagreements with this set.
            # The seven that derivation calls INCONCLUSIVE (-g -q -v -L -M -O
            # -V) write no file list, so a hit count cannot answer for them;
            # they are settled from --help. THREE of those seven take a
            # required value (-g, -M, -O) and are in value_flags, which is
            # what keeps their digits out of the scan; the other FOUR (-q,
            # -v, -L, -V) are boolean and are in the set below.
            depth_digits=frozenset("%+,-.0123456789@EFGHILPRSTUVWXY^abchijklnopqrsuvwxyz"),  # site-literal-ok: the ten decimal digits, not an identifier
            fallback_needs_tty=True,
            pat_flags=("-e", "--regexp", "-f", "--file",
                       "-N", "--neg-regexp"),
            # Required-value flags only. Every OPTIONAL-argument spelling is
            # deliberately absent, which is grep's --color rule: ugrep writes
            # them `-K [MIN,][MAX]`, `-m [MIN,][MAX]`, `-Q[=DELAY]`,
            # `-Z[best][+-~][MAX]`, `--ignore-files[=FILE]`, `--color[=WHEN]`,
            # `--sort[=KEY]`, `--tabs[=NUM]`, `--width[=NUM]`, `--mmap[=MAX]`,
            # `--pager[=COMMAND]`, `--view[=...]`, `--config[=FILE]`,
            # `--pretty[=WHEN]`, `--hyperlink[=...]`, `--hexdump[=...]`,
            # `--tag[=TAG[,END]]`, `--separator[=SEP]`,
            # `--group-separator[=SEP]`, `--save-config[=FILE]`. Listing any
            # of them would eat the following token -- and the incident argv
            # that motivated this profile spelled `--ignore-files --hidden`,
            # so listing that one alone loses `--hidden`, then spends the real
            # root on skip_pos.
            value_flags=("-A", "--after-context", "-B", "--before-context",
                         "-C", "--context", "-D", "--devices",
                         "-d", "--directories", "-g", "--glob", "--iglob",
                         "-J", "--jobs", "-M", "--file-magic",
                         "-O", "--file-extension", "-t", "--file-type",
                         "--binary-files", "--colors", "--colours",
                         "--delay", "--encoding",
                         "--exclude", "--exclude-dir", "--exclude-from",
                         "--exclude-fs", "--filter", "--filter-magic-label",
                         "--format", "--from",
                         "--include", "--include-dir", "--include-from",
                         "--include-fs", "--label",
                         "--max-count", "--min-count", "--max-files",
                         "--max-size", "--min-size", "--range",
                         "--replace", "--zmax"),
            # `--recurs` is rejected outright: "invalid option --recurs, did
            # you mean --range=, --recursive, ...". No getopt_long, no
            # unique-prefix resolution.
            abbreviates=False,
            skip_pos=1),

    # locate, plocate and updatedb are all UNWRAPPED. updatedb's only real
    # caller on a typical node is its own systemd timer, which runs as root
    # under systemd's PATH and never sees a shim; its configuration commonly
    # prunes network filesystem types and the mounts they sit on, so the walk
    # this table would refuse is one updatedb is already not doing; and a
    # wrapped name is not free -- it is refusal surface on a command that
    # cannot cost what the refusal claims. locate/plocate read that database
    # rather than building it, but the refusal text does not RECOMMEND them
    # either, because a database that prunes the expensive mounts holds
    # nothing under any path the guard refuses.
    #
    # Two pieces of grammar lost their only wrapped user when updatedb went,
    # and both are kept deliberately rather than deleted -- they belong to
    # getopt_long and to the table, not to updatedb. Written down because a
    # mechanism with no live user is one nobody notices breaking:
    #
    #   * abbreviated root flags (`--database-r DIR`). Pinned at table level
    #     by test_an_abbreviated_root_flag_still_supplies_a_root; the shim's
    #     arm is unreachable, since reaching it needs a profile that both
    #     abbreviates and takes a root flag.
    #   * a default_root other than `.` -- every remaining profile walks the
    #     cwd when given no operand. Same test covers the table side.

    # fzf has no path grammar. It walks cwd only when nothing is piped in; with
    # a pipe on stdin it is a filter over someone else's output and walks
    # nothing.
    #
    # wrapped=False: fzf is normally reached through a shell keybinding
    # (Ctrl-R/Ctrl-T) or an editor plugin, not typed at a prompt, so Layer
    # 1's `exit 2` refusal is an invisible no-op there rather than the
    # teaching moment it is for `find`/`grep`. The Profile stays in PROFILES
    # either way -- this only drops it from the default wrapped set, so Layer
    # 2 (the reaper, keyed on PROFILE_BY_NAME) still sees it exactly as
    # before.
    Profile(("fzf", "sk"),
            always=True,
            root_mode="stdin",
            wrapped=False,
            depth_flags=("--walker-max-depth",),
            value_flags=("--walker", "--walker-skip", "-q", "--query",
                         "--prompt", "--preview", "--bind", "--expect",
                         # Audited by ASKING fzf, not by reading its help:
                         # each candidate was run as `fzf <flag> --filter=aaa`
                         # to see whether --filter survived. 23 of 24 consume
                         # their value; `--color` does NOT -- `fzf --color
                         # --filter=aaa` still filtered -- so it is the same
                         # optional-argument shape as grep's --color and du's
                         # --time, and is deliberately absent. Listing it
                         # would swallow the next token.
                         "--algo", "--delimiter", "--filter", "--header",
                         "--header-lines", "--height", "--history",
                         "--history-size", "--hscroll-off", "--info",
                         "--jump-labels", "--layout", "--margin", "--marker",
                         "--min-height", "--nth", "--padding", "--pointer",
                         "--preview-window", "--scroll-off", "--tabstop",
                         "--tiebreak", "--with-nth"),
            root_flags=("--walker-root",)),
)

PROFILE_BY_NAME = {name: p for p in PROFILES for name in p.names}

# The table's own default for which tools Layer 1 leaves alone; a site's
# `[shim].unwrapped` list replaces it at build time.
DEFAULT_UNWRAPPED = frozenset(
    name for p in PROFILES if not p.wrapped for name in p.names)


def wrapped_profiles(unwrapped):
    """The subset of PROFILES that gets a Layer 1 shim at all.

    `unwrapped` is a set of tool names the site leaves alone (fzf/sk by
    default -- see Profile.wrapped). The build's case-arm generation filters
    on THIS function, not on PROFILES directly, so the two derived artifacts
    (wrapped_names.sh and guard.sh's case arms) cannot drift from each other
    the way this table's tests already guard against everywhere else two
    consumers are built from one source. A profile is wrapped only if NONE
    of its names is excluded: fzf and sk are one profile and one decision.
    """
    unwrapped = set(unwrapped)
    return tuple(p for p in PROFILES
                 if not any(name in unwrapped for name in p.names))


def wrapped_names(unwrapped):
    """Every name this table knows how to reason about that Layer 1 wraps.

    The installer symlinks only the ones that actually resolve on the node: a
    shim named `ag` where ag is absent makes `command -v ag` succeed and
    silently changes how scripts probe for tools.
    """
    return tuple(name for p in wrapped_profiles(unwrapped) for name in p.names)


# There is deliberately no name-set for /proc/<pid>/comm here. The reaper used
# to hold one and match against it, which is how several live `bfs`
# traversals -- whose comm read a version string rather than a tool name --
# stayed invisible to Layer 2. Resolution goes through tool_from_argv(),
# argv[0] first, and a set named for comm sitting beside the wrapped names is
# an invitation to key on it again.

# Tools whose walk cannot be bounded by any flag they accept, so the refusal
# has to offer something other than a flag: a different tool for the grep
# family, and for du -- whose depth flags prune output rather than the walk --
# no in-place alternative at all, only walk-job.
#
# Documentation, not machinery: nothing reads this. Both consumers decide the
# same question from whether the profile HAS depth flags -- the shim from an
# empty depth-flag list, the table from depth_bound() returning None -- so a
# name added here changes no behaviour on its own. It is kept because "which
# tools can never be bounded" is a fact worth being able to read off the table.
UNBOUNDABLE = frozenset(
    name for p in PROFILES if not p.depth_flags for name in p.names
)

# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------


def clean_path(path, cwd):
    """Absolute, normalized form of a path operand as the shell would see it."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    # normpath keeps a leading `//` on POSIX, so `//big` would not compare
    # equal to `/big` without this.
    return re.sub(r"/{2,}", "/", os.path.normpath(path)) or "/"


def read_mounts(path, policy):
    """The EXPENSIVE rows of a mount table, longest mount point first.

    Each row is `(mountpoint, fstype, source, options)`: mount point first so
    that every consumer that only ever cared about the point and the type
    keeps reading `row[0]` and `row[1]`, with the two fields classify_mount()
    needs carried behind them. Which rows are expensive is classify_mount()'s
    decision, from the fields and the policy alone (ADR-0016); cheap rows are
    not returned, so a table with no expensive mount is an empty list.

    Longest-first is what makes the containing-mount lookup correct: `/big`
    must win over `/` for a path under it.
    """
    found = []
    try:
        with open(path, "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                # /proc/mounts octal-escapes spaces and tabs in the mountpoint.
                mnt = parts[1].replace("\\040", " ").replace("\\011", "\t")
                mnt = mnt.rstrip("/") or "/"
                entry = (parts[0], mnt, parts[2], parts[3])
                if classify_mount(entry, policy) == "expensive":
                    found.append((mnt, parts[2], parts[0], parts[3]))
    except OSError:
        # A cost guard must not wedge the caller. No mount table means no
        # opinion, which means allow.
        return []
    found.sort(key=lambda row: len(row[0]), reverse=True)
    return found


def depth_below(path, mount):
    """How many path components `path` sits below `mount`, or None if outside."""
    mount = mount.rstrip("/")
    if not mount:  # the mount is /
        return len([c for c in path.split("/") if c])
    if path == mount:
        return 0
    if path.startswith(mount + "/"):
        return len([c for c in path[len(mount):].split("/") if c])
    return None


def descended_mounts(path, mounts):
    """Every expensive mount a walk from `path` would descend INTO.

    All of them, not the worst one. `offending_mount()` picks a single mount
    to NAME in the refusal, and that pick is a message concern: which mount
    is cited is not which mounts get enumerated. Anything deciding how
    expensive the walk is -- the per-mount depth allowance -- has to see the
    whole set, because the walk enters all of it.

    Rows are returned as read_mounts() produced them.
    """
    prefix = path.rstrip("/") + "/"
    return [row for row in mounts
            # Not below it and underneath it: the two are separate tests, and
            # a mount BELOW an already-deep-enough root is reached this way --
            # `/home/u/runs` is scoped for /home and still descends into a
            # `/home/u/runs/scratch` mount.
            if depth_below(path, row[0]) is None
            and (row[0].startswith(prefix) or path == "/")]


def offending_mount(path, mounts, policy):
    """(mountpoint, fstype, reason) this walk would enumerate, or None.

    Two cases, because both pay the same cost -- this is the session guard's
    ancestor-and-descendant test, generalized from a hardcoded subtree list to
    the live mount table:

      * the walk starts at or inside an expensive mount, too near its root
      * the walk starts ABOVE one and will descend into it (`/`)
    """
    for row in mounts:
        mount, fstype = row[0], row[1]
        depth = depth_below(path, mount)
        # `mounts` is longest-first, so the most specific mount containing
        # this path is decided first and wins -- and it is also the NEAREST,
        # since a mount containing that one is further above the path still.
        # So the first containing mount settles this case for all of them.
        if depth is not None and depth < policy.unscoped_depth:
            return (mount, fstype, "at_or_near_root")
    descends = descended_mounts(path, mounts)
    if not descends:
        return None
    # A walk from a cheap parent may descend into mounts of several types.
    # Longest-first would name whichever mount point is deepest, and the
    # refusal could cite the cheaper mount as the reason. Rank by
    # `policy.remote_fstypes` order instead, which is worst-first, then by
    # shallowest mount.
    #
    # This decides the MESSAGE and nothing else. The tie-break -- shallowest
    # among equally-severe mounts -- exists so the refusal is deterministic,
    # not so it can be read as "the mount that matters": two mounts of one
    # type tie for a walk from `/`, and the shim's own ranking once broke that
    # tie by /proc/mounts order instead. While the two consumers only NAMED a
    # mount, that difference was invisible; the moment the named mount decided
    # a verdict, the same command was allowed or refused depending on the
    # order two lines appear in /proc/mounts. Hence root_depth_allowance(),
    # which asks descended_mounts() rather than this.
    def rank(row):
        return (_severity(row[1], policy), len(row[0]))

    row = min(descends, key=rank)
    return (row[0], row[1], "descends_into")


# --------------------------------------------------------------------------
# argv analysis
# --------------------------------------------------------------------------

# Accepted before the path operands and consuming nothing; everything else
# starting with `-` ends the operand list.
#
# find's own leading options are a CLOSED set -- -H/-L/-P, -O<level> attached,
# -D <debugopts> separate -- which is why modelling them is a few lines rather
# than the open-ended job grep's abbreviations would be. `find -O3 DIR -name
# x` and `find -D search DIR -name x` are both accepted by find(1), verified,
# and both used to break the operand scan on the first token, losing DIR and
# falling back to the cwd.
#
# bfs shares this profile and its flag set is a SUPERSET, which the profile
# honoured for predicates (-maxdepth) and not for flags. That gap was found in
# the wild: several live `bfs -S dfs -regextype findutils-default / ...`
# processes, one unbounded at / for days, every one of them read as walking
# the CWD because the scan stopped dead at `-S`.
#
# Taken from `bfs --help` (4.1.1) rather than guessed, since guessing is how
# the gap got here: -E -X -d -s -x consume nothing, -f PATH supplies a root
# (root_flags), -S and -D take the next token, -jN and -ON are glued. GNU find
# rejects the bfs-only spellings outright, so listing them here costs nothing:
# the worst case is that a command find would refuse anyway is read more
# carefully.
FIND_PREFIX_FLAGS = ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x")

# Leading options that consume the FOLLOWING token, so neither the flag nor
# its value is a path. -D is find's and bfs's; -S is bfs's; -regextype is
# find's and is the second flag in the live invocations above. `-f` is NOT
# here: it is bfs's own root_flags entry (a root_flags-listed token, which
# is what "skip2" is used for, but its value is a PATH, not data to discard
# -- see the root_flags branch in _find_leading()), and it is never find's
# at all. Both used to be true of this one shared list, which is how find
# came to accept "-f" silently.
FIND_VALUE_LEADING_FLAGS = ("-D", "-S", "-regextype")

# Glued-value leading options: `-O3`, `-j8`. bfs documents both attached, so a
# bare `-O`/`-j` is not a real spelling -- and reading one as consuming
# nothing leaves the next token a path operand, which is judged rather than
# skipped, the safe direction.
FIND_GLUED_LEADING_PREFIXES = ("-O", "-j")


def _find_leading(profile, token):
    """"skip" for a leading option, "skip2" if it also eats the next token.

    Profile-aware: `-f` is bfs's root_flags entry and never find's, so
    whether it is recognized at all depends on which profile is asking, not
    on one list both share.
    """
    if token in FIND_PREFIX_FLAGS:
        return "skip"
    if token in profile.root_flags:
        # Already charged as a root by the main scan in roots(); here it is
        # skipped so its value is not ALSO read as a positional. Checked
        # before FIND_VALUE_LEADING_FLAGS because a tool could in principle
        # give a root flag the same spelling as a value flag -- none does
        # today, but the root reading has to win if one ever does.
        return "skip2"
    if token in FIND_VALUE_LEADING_FLAGS:
        return "skip2"
    for prefix in FIND_GLUED_LEADING_PREFIXES:
        if token.startswith(prefix):
            return "skip"
    # A cluster of PURE boolean prefix flags, getopt-style: `-HL` is `-H`
    # plus `-L`, both real (`bfs -HL clu -maxdepth 0` walks clu; GNU find
    # calls `-HL` an unknown predicate and never reaches this profile at
    # all since it never clusters). Every letter has to be a member or this
    # is not a cluster -- `-regextype` starts the same way and must not be
    # split into nine single-letter "flags" it does not have. Measured
    # before this existed: `roots()` on `["bfs", "-HL", "/big",
    # "-maxdepth", "1"]` returned the cwd, not /big -- a real path silently
    # swapped for a cheap one. Checked against `profile.cluster_letters`
    # rather than FIND_PREFIX_FLAGS directly, so this cannot drift from
    # parse_short_cluster()'s answer to the identical question for the
    # depth/device scan.
    if (profile.cluster_letters is not None and len(token) > 2
            and token.startswith("-") and token[1] != "-"
            and all(c in profile.cluster_letters for c in token[1:])):
        return "skip"
    return None


def effective_cwd(profile, argv, cwd):
    """The directory this invocation resolves its relative paths against.

    fd's `--base-directory` replaces the caller's cwd for that purpose, and
    for the default root when no operand is given. Last one wins.

    Every occurrence is resolved against the ORIGINAL cwd, not against the
    base a previous occurrence selected. fd resolves the base against the
    process's own directory, so `--base-directory /tmp --base-directory sub`
    from /big is /big/sub, not /tmp/sub. Resolving them in sequence made this
    consumer disagree with the shim -- which had it right -- and a
    disagreement is the one condition this repo calls worse than either layer
    alone.

    Walked IN ARGV ORDER with the same grammar as the other scanners: exec
    bodies skipped, `--` ends the options, and a value flag's value stepped
    over -- INCLUDING one consumed by a short option inside a cluster.
    `fd -HE --base-directory /tmp/cheap pat` has `-E` eat the flag, and
    missing that let an expensive cwd walk resolve against a false cheap
    base.
    """
    if not profile.cwd_flags:
        return cwd
    origin = cwd
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in profile.exec_flags:
            # Template tokens are data: the separate `-x` form consumes
            # everything up to `;` (measured on fd).
            i = skip_exec_body(profile, argv, i)
            continue
        if token == "--":
            # Past the marker a flag-shaped token is an operand, not an
            # option, so it cannot change the coordinate system.
            break
        matched = False
        for flag in profile.cwd_flags:
            if token == flag:
                if i + 1 < len(argv):
                    cwd = clean_path(argv[i + 1], origin)
                i += 2
                matched = True
                break
            if token.startswith(flag + "="):
                cwd = clean_path(token[len(flag) + 1:], origin)
                i += 1
                matched = True
                break
        if matched:
            continue
        # A cluster's value-taking letter consumes the next token, so that
        # token is not an option. Before the exact-match test below, which
        # never sees `-HE`.
        letters, vflag, _glued, wants_next = parse_short_cluster(profile, token)
        if letters and vflag is not None:
            i += 2 if wants_next else 1
            continue
        if _flag_takes_value(profile, token):
            # Its VALUE is not an option, however spelled. After the cwd
            # flags above, since those are value flags too.
            i += 2
            continue
        i += 1
    return cwd


def skip_exec_body(profile, argv, i):
    """Index just past the command template argv[i] introduces.

    argv[i] must already be known to be in profile.exec_flags. Terminators:
    `;` always, and `+` only for exec_plus_flags and only immediately after
    `{}` -- measured, `find DIR -maxdepth 1 -exec echo x +` is "missing
    argument to `-exec'" and `-ok echo {} +` likewise, while fd accepts no
    `+` at all and takes `;` (`fd -x echo {} ';' target` made target the
    pattern).

    An unterminated template runs to the end of argv, which is what both
    tools do.
    """
    plus_terminates = argv[i] in profile.exec_plus_flags
    previous = None
    i += 1
    while i < len(argv):
        if argv[i] == ";":
            break
        if plus_terminates and argv[i] == "+" and previous == "{}":
            break
        previous = argv[i]
        i += 1
    return i + 1  # step over the terminator itself


def resolve_for(profile, token, flags):
    """resolve_long_flag, gated on whether this tool abbreviates at all.

    Every abbreviation-aware site goes through this rather than calling
    resolve_long_flag directly, so a profile whose tool rejects prefixes
    cannot acquire the grammar by having a new call site added.
    """
    if not profile.abbreviates:
        return None
    return resolve_long_flag(token, flags, profile.exact_flags)


def resolve_long_flag(token, flags, exact=()):
    """The flag in `flags` that `token` is an unambiguous abbreviation of.

    GNU getopt_long accepts any unique prefix of a long option, so
    `grep --dir=recurse PAT /home` and `grep --rec PAT /home` both recurse --
    verified against grep(1), which accepts everything from `--di` up and
    calls `--d` ambiguous. Exact-match tables miss all of it.

    Applied ONLY to the flag families where a miss is a BYPASS:

      rec_flags        -- `--rec` reads as unknown, so no traversal is seen
      rec_value_flags  -- `--dir=recurse` likewise
      pat_flags        -- `--rege pat` leaves saw_pattern_flag false, and the
                          first real path operand gets charged as the pattern

    Also applied to value_flags by is_traversal(), which this docstring used
    to say was deliberately avoided -- and the reason it gave came true. The
    warning was that deciding value-flag abbreviations "properly would need
    grep's COMPLETE option list", and two live defects followed: `--time` is
    du's OWN optional-argument option but only a prefix of `--time-style` (a
    false refusal), and `--binary` is grep's own no-argument option but only
    a prefix of `--binary-files` (a BYPASS -- `grep --binary -r needle DIR`
    had the `-r` skipped as a value and recursed unseen).

    `exact` closes that: a token that is a real, non-consuming option of the
    tool is never an abbreviation of anything. It holds the enumerated
    collisions rather than the complete option list, which is the part that
    matters -- an option that is not a prefix of a value_flag cannot be
    mistaken for one.

    Ambiguity is otherwise judged against `flags`, not against the tool's real
    option set. That errs safe: a token this calls unique but the tool calls
    ambiguous makes us refuse a command the tool would itself reject.
    """
    if not token.startswith("--") or len(token) < 4:
        return None
    # An exact real option is itself, not an abbreviation of a longer one.
    # getopt_long resolves an exact match before considering prefixes.
    if token in exact:
        return None
    matches = [f for f in flags if f.startswith("--") and f.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    return matches[0] if len(set(matches)) == 1 else None


def _cluster_letters(token):
    """Letters of a clustered short option (`-rIn` -> `rIn`), else ''."""
    if len(token) > 1 and token[0] == "-" and token[1] != "-":
        return token[1:]
    return ""


def _enables_traversal_by_value(profile, argv, i):
    """Is argv[i] a flag whose value turns the walk on? `grep -d recurse`.

    Covers the three spellings GNU getopt accepts: separate (`-d recurse`),
    attached long (`--directories=recurse`) and glued short (`-drecurse`).
    """
    token = argv[i]
    for flag, values in profile.rec_value_flags:
        if token == flag:
            return i + 1 < len(argv) and argv[i + 1] in values
        if any(token == "%s=%s" % (flag, value) for value in values):
            return True
        if len(flag) == 2 and flag[1] != "-" and any(
                token == flag + value for value in values):
            return True
        # Abbreviated long spellings: --dir=recurse, --di recurse.
        head, sep, tail = token.partition("=")
        if resolve_for(profile, head, (flag,)):
            if sep:
                return tail in values
            return i + 1 < len(argv) and argv[i + 1] in values
    return False


def parse_short_cluster(profile, token):
    """Split a `-abc`-style token the way getopt does.

    Returns (letters, value_flag, glued_value, wants_next):

      letters      -- the characters that are really OPTION letters
      value_flag   -- the flag that consumed an argument, or None
      glued_value  -- that argument, when it was attached to this token
      wants_next   -- True when the argument is the NEXT argv element instead

    Scanning left to right, the first letter that takes an argument consumes
    the remainder of the token as its value, or the following element if the
    token ends there. So `-nefoo` is `-n` plus `-e foo`: the `f` and the `o`s
    are not options at all, and `-dread` is `-d read` rather than a cluster
    containing `r`.

    This replaced three separate special cases -- attached pattern, glued
    short value, exact-match-only pattern flags -- each added after the
    previous one missed a spelling. A cluster parser is what the problem was
    asking for; the special cases kept being one grammar rule behind.

    If NO letter is a value flag, the whole token is a cluster of boolean
    switches -- and if `profile.cluster_letters` is set, every one of those
    letters has to be a member or the token is not a cluster at all: it is
    returned exactly as `("", None, None, False)`, i.e. not this tool's
    grammar. Without that gate, a long predicate that happens to start with
    letters this tool never uses as flags -- `-regextype` -- gets walked
    letter by letter like a real cluster, and any one of its letters that is
    also a device or depth flag reads as though the caller had typed it.
    Measured: adding `-x` to find/bfs's device_flags without this gate made
    every `bfs -regextype ... /` read as device-bounded.
    """
    if len(token) < 2 or not token.startswith("-") or token[1] == "-":
        return ("", None, None, False)
    letters = ""
    rest = token[1:]
    while rest:
        letter, rest = rest[0], rest[1:]
        letters += letter
        if "-" + letter in profile.value_flags:
            if rest:
                return (letters, "-" + letter, rest, False)
            return (letters, "-" + letter, None, True)
    if profile.cluster_letters is not None and not all(
            c in profile.cluster_letters for c in letters):
        return ("", None, None, False)
    return (letters, None, None, False)


def _dir_action_setting(profile, argv, i):
    """("recurse"|"skip", tokens_consumed) if argv[i] sets the directory
    action, else None.

    Covers every spelling GNU getopt accepts for the value form -- separate
    (`-d skip`), glued (`-dskip`), attached long (`--directories=skip`) and
    abbreviated (`--dir=skip`) -- plus the plain recursive flags, which set
    the action to recurse rather than being a separate switch.
    """
    token = argv[i]
    for flag, on_values in profile.rec_value_flags:
        if token == flag:
            if i + 1 >= len(argv):
                # No value at all. getopt rejects that outright -- real grep
                # says "option requires an argument -- 'd'" -- so nothing is
                # applied and the action is UNCHANGED. Reading the absent
                # value as "skip" invented one, and made this consumer allow
                # `grep -r -d` while the shim refused it. A disagreement is
                # the condition this repo calls worse than either layer
                # alone, even on a command that cannot run.
                return None
            return (("recurse" if argv[i + 1] in on_values else "skip"), 2)
        head, sep, tail = token.partition("=")
        if sep and (head == flag or resolve_for(profile, head, (flag,))):
            return (("recurse" if tail in on_values else "skip"), 1)
        if not sep and resolve_for(profile, head, (flag,)):
            value = argv[i + 1] if i + 1 < len(argv) else None
            return (("recurse" if value in on_values else "skip"), 2)
        if len(flag) == 2 and flag[1] != "-" and token.startswith(flag) \
                and len(token) > 2:
            return (("recurse" if token[2:] in on_values else "skip"), 1)
    if token in profile.rec_flags or resolve_for(profile, token,
                                                profile.rec_flags):
        return ("recurse", 1)
    return None


def _last_dir_action(profile, argv):
    """The directory action in force after the whole option sequence."""
    action = profile.dir_action_default
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--":
            # After `--` a flag-shaped token is a pattern, not an action.
            break
        if _is_attached_pattern(profile, token):
            i += 1
            continue
        setting = _dir_action_setting(profile, argv, i)
        if setting is not None:
            action, consumed = setting
            i += consumed
            continue
        letters, vflag, glued, wants_next = parse_short_cluster(profile, token)
        if letters:
            # Left to right WITHIN the token: `-rd skip` is -r then -d skip,
            # so the -d wins. parse_short_cluster stops at the first
            # value-taking letter, so vflag is the last of `letters`.
            dir_flags = [f for f, _ in profile.rec_value_flags]
            scan = letters[:-1] if vflag in dir_flags else letters
            if "r" in scan or "R" in scan:
                action = "recurse"
            if vflag in dir_flags:
                on_values = dict(profile.rec_value_flags)[vflag]
                value = glued
                if value is None and wants_next and i + 1 < len(argv):
                    value = argv[i + 1]
                # Same rule as the exact spelling above: an absent value
                # leaves the action alone rather than inventing "skip".
                # `grep -rd` with nothing after it is the cluster form of it.
                if value is not None:
                    action = "recurse" if value in on_values else "skip"
            i += 2 if wants_next else 1
            continue
        if resolve_for(profile, token.partition("=")[0], profile.value_flags):
            i += 1 if "=" in token else 2
            continue
        if _flag_takes_value(profile, token):
            i += 2
            continue
        i += 1
    return action


def is_traversal(profile, argv):
    """Does this invocation walk a directory tree at all?

    The fast path. `ps aux | grep sshd` is the overwhelming majority of grep
    calls on a login node and must not pay for a mount lookup.
    """
    if profile.dir_action_default is not None:
        # Order-sensitive: see _last_dir_action(). Checked BEFORE `always`,
        # because rgrep is grep -r under another name and `rgrep -d skip`
        # really does not descend -- verified on the tool.
        if _last_dir_action(profile, argv) in RECURSE_ACTIONS:
            return True
        # ...except that for ugrep a depth flag outranks the action entirely,
        # and is not subject to its last-wins. Measured on 7.8.4: `-d skip`
        # alone walks nothing, while `-d skip --depth=9` and `--depth=9 -d
        # skip` both walk the whole tree. A malformed depth still reads as no
        # traversal, which is right -- the tool exits before opening anything.
        return bool(profile.depth_enables_traversal
                    and _scan_bounds(profile, argv)[0] is not None)
    if profile.always:
        return True
    i = 1
    while i < len(argv):
        token = argv[i]
        if _enables_traversal_by_value(profile, argv, i):
            return True
        if token == "--":
            # After `--` a flag-shaped token is a pattern: `grep -- -r file`
            # searches for the string -r and recurses into nothing.
            break
        if _is_attached_pattern(profile, token):
            # The pattern is INSIDE this token, so its letters are not option
            # letters. Without this the clustered scan below reads the `r` in
            # `grep -eerror` as the recursive flag and refuses a command that
            # walks nothing -- a false refusal, which is how an advisory layer
            # teaches people to alias around it. Checked before the
            # value-flag and clustered arms, not after.
            i += 1
            continue
        letters, vflag, glued, wants_next = parse_short_cluster(profile, token)
        if letters:
            # A rec-value flag reached inside a cluster: `-nd recurse`.
            for flag, values in profile.rec_value_flags:
                if vflag != flag:
                    continue
                if glued in values:
                    return True
                if (wants_next and i + 1 < len(argv)
                        and argv[i + 1] in values):
                    return True
            # Only the real option letters are scanned for r/R -- not a glued
            # value, which is why `-dread` and `-nefoo` no longer read as
            # recursive.
            if "r" in letters or "R" in letters:
                return True
            i += 2 if wants_next else 1
            continue
        if resolve_for(profile, token.partition("=")[0], profile.value_flags):
            # An ABBREVIATED value flag also consumes its value, and missing
            # that is not the safe direction: `grep --rege -- -r /home` has
            # `--rege` eat the `--`, leaving -r live, and grep(1) descends --
            # while this loop read the `--` as the end-of-options marker and
            # reported no traversal at all.
            #
            # An earlier review declined abbreviations for value_flags on the
            # grounds that a miss errs toward refusing. That reasoning was
            # wrong in exactly this direction and this is the counterexample.
            # Resolving against our own list still errs safe the other way: a
            # token we call unique and grep calls ambiguous makes us skip a
            # value grep would reject, so the command errors instead of
            # walking.
            i += 1 if "=" in token else 2
            continue
        if _flag_takes_value(profile, token):
            # ...but a `--` CONSUMED BY a flag is that flag's value and not a
            # marker at all. `grep -e -- -r /big` searches for the string
            # `--` and the -r after it is a real recursive flag: verified
            # against grep(1), which descends. Stopping the scan on that `--`
            # waved the incident shape straight through.
            i += 2
            continue
        if token in profile.rec_flags:
            return True
        if resolve_for(profile, token, profile.rec_flags):
            return True
        i += 1
    return False


# _depth_value()'s "this token is a depth flag but there is nothing left in
# argv to be its value" case, distinct from None ("not a depth flag at
# all"). Real find/tree validate the argument as they parse it, before
# opening anything -- `find -maxdepth` at the end of argv and `tree -L 2 d1
# -L` both exit nonzero and walk nothing (measured) -- so "no value here" is
# not "unbounded", it is "this command will not run", and the two must not
# collapse into the same answer the way None already means for both.
_NO_DEPTH_VALUE = object()


def _is_valid_depth(value):
    """Would this string parse as the tool's own depth-flag argument?

    Used to validate EACH occurrence as _scan_bounds() reaches it, not only
    the last-wins value at the end of the scan: find and tree both validate
    an argument at the point they parse it, so `-maxdepth bad -maxdepth 2`
    fails on the FIRST occurrence and never reaches the second, no matter
    how last-wins would have resolved the VALUE. Deferring this check to a
    single int() on the final `last` missed exactly that ordering.

    NOT a bare `int(value)`: that accepts things GNU find rejects outright,
    measured against findutils 4.8.0 -- `-maxdepth " 1"`, `"1 "`, `"-1"` and
    `"+1"` all print "Expected a positive decimal integer argument to
    -maxdepth" and walk nothing, while Python's int() parses all four. A
    generated shim can only test bytes (`case $v in *[!0-9]*)`), which is
    already this strict; a lenient int() here first agreed with the shim by
    coincidence, for every value small enough to be globally bounded either
    way -- `-maxdepth " 1"` read as "a trivially bounded depth of 1" and
    `-maxdepth "-1"` as "a trivially bounded depth of -1", both allowed
    regardless of mount -- and diverged the moment a lenient value was too
    LARGE to be globally bounded: `-maxdepth " 10"` on an expensive mount
    was refused here and allowed by the shim, which was the shim being
    right. Matching find's own grammar removes the coincidence rather than
    the divergence it was hiding.
    """
    return bool(value) and all(c in "0123456789" for c in value)  # site-literal-ok: the ten decimal digits, not an identifier


# One depth expression as it appears inside a short cluster: `9`, `3-5`,
# `3,5`, and the truncated `5-` / `5,` that the tool rejects. A trailing
# separator is CAPTURED rather than skipped, so it can be rejected rather than
# read as the bare number in front of it.
_DEPTH_EXPR = re.compile(r"[0-9]+(?:[-,][0-9]*)?")


def _depth_span(profile, value):
    """(MIN, MAX) for one depth argument, or None if the tool rejects it.

    MIN is None when the spelling does not carry one. Both are strings; the
    caller compares them as ints.

    Without `depth_range` the whole value is the bound and there is no MIN.
    With it the value is `[MIN,]MAX`, and only the MAX bounds the walk.
    Measured on ugrep 7.8.4 against a five-level tree:

        --depth=3,5   walks 3..5      --depth=,5   walks 1..5
        --depth=2,    "invalid argument -2,"    and walks nothing
        --depth=5,3   "invalid argument -5,3"   and walks nothing

    Three separate facts, and the third was missed once. An absent MIN is
    fine and an absent MAX is not; and a MIN ABOVE the MAX is rejected
    outright rather than clamped. Reading `5,3` as "bounded at 3" invents a
    bound for a command that never runs, and reading `2,` as "bounded at 2"
    does the same -- both in the false-refusal direction, which is still
    wrong: a refusal that is wrong about the user's command is what teaches
    people to alias around this layer.
    """
    if value is None:
        return None
    if not profile.depth_range:
        return (None, value) if _is_valid_depth(value) else None
    head, sep, tail = value.partition(",")
    if not sep:
        return (None, value) if _is_valid_depth(value) else None
    # MIN may be empty; MAX may not.
    if head and not _is_valid_depth(head):
        return None
    if not _is_valid_depth(tail):
        return None
    if head and int(head) > int(tail):
        return None
    return (head or None, tail)


def _depth_value(profile, argv, i):
    """The depth this token asks for, or None if it is not a depth flag.

    Returns _NO_DEPTH_VALUE, not None, when the token IS a depth flag but
    argv ends before its value would be -- see _NO_DEPTH_VALUE.
    """
    token = argv[i]
    for flag in profile.depth_flags:
        if token == flag:
            return argv[i + 1] if i + 1 < len(argv) else _NO_DEPTH_VALUE
        if token.startswith(flag + "="):
            return token[len(flag) + 1:]
        # `-d2`, `-L3`: a short depth flag with its value attached.
        if len(flag) == 2 and flag[1] != "-" and token.startswith(flag) and token[2:].isdigit():
            return token[2:]
    return None


def _device_setting(profile, token):
    """True if `token` turns the device bound on, False if off, else None.

    Exact and abbreviated long spellings; the glued short ones are letters
    inside a cluster and are handled by the caller's cluster scan.
    """
    if token in profile.device_off_flags:
        return False
    if token in profile.device_flags:
        return True
    if resolve_for(profile, token, profile.device_off_flags):
        return False
    if resolve_for(profile, token, profile.device_flags):
        return True
    return None


def globally_bounded(depth, policy):
    """Is this depth within the ceiling that applies on EVERY mount?

    The single place the global comparison lives, so nothing can hold a
    second copy of the ceiling. check() uses it for the fast accept and
    bounded() composes it with depth_bound(); when the comparison lived
    inside _scan_bounds() instead, the per-mount split left bounded()
    answering the global question while check() had moved on to a per-mount
    one, and a stale predicate named `bounded` is worse than no predicate.

    This is NOT the verdict. A depth above this ceiling may still be allowed
    on a mount with a looser allowance; only judge_root() knows.
    """
    return depth is not None and depth <= policy.maxdepth_allowed


def bounded(profile, argv, policy):
    """Is the walk bounded to the global ceiling or shallower?

    The GLOBAL question, over argv. Neither decision path calls it and that
    is deliberate, not a gap: check() needs the depth VALUE for a per-mount
    comparison, so asking this as well would rescan argv for something it
    already has, and the reaper declines depth bounds by policy. It exists
    because it is the name this repo's guard rule goes by -- the shim, the
    reaper and ADR-0007 all cite bounded() -- and because the argv grammar
    below is asserted through it. See _scan_bounds().

    LAST depth flag wins, because that is what the tools do. Measured:

      find DIR -maxdepth 1 -maxdepth 9 -name deep.txt   found a depth-6 file
      find DIR -maxdepth 9 -maxdepth 1 -name deep.txt   (nothing)
      tree -L 1 -L 4 DIR                                4 directories

    (`du -d 1 -d 9 DIR` printed 6 lines and `du -d 9 -d 1 DIR` 2, measured
    the same way. Kept as a record of du(1) and dropped as evidence here:
    du has no depth flag in this table, because its -d prunes output rather
    than the walk, so there is no bound for a later one to win.)

    Returning on the FIRST one made `find /big -maxdepth 1 -maxdepth 9` read
    as bounded while find walks to depth 9 -- a bypass. rg and fd reject a
    repeated depth flag outright ("provided more than once, but cannot be
    used multiple times"), so for them either answer is moot and last-wins
    is simply the honest model.

    This is the same shape as the directory action, which was also
    first-wins once and also wrong. The tell both times: an option whose
    value can be RESTATED is a setting, not a switch.
    """
    return globally_bounded(depth_bound(profile, argv), policy)


def depth_bound(profile, argv):
    """The depth this walk is bounded to, or None if it is unbounded.

    Split out from bounded() when the ceiling became per-mount: the value is
    mount-independent and cheap, the verdict is not. This is the half check()
    wants -- which ceiling applies is judge_root()'s to decide, and it cannot
    be decided here, where no mount table is in scope.

    Usually that is the last depth flag's value. It is `profile.default_depth`
    instead when the tool bounds a directory operand on its own and nothing in
    argv has said otherwise -- ugrep, where `ugrep PAT DIR` lists DIR's own
    files and descends no further.

    Three conditions, and each earns its place:

      * no depth flag occurred, so there is nothing to override;
      * the directory action is still "recurse_default", i.e. NOTHING set it.
        `-r`, `-R`, `--recursive`, `--dereference-recursive`, `-d recurse` and
        `--directories=dereference-recurse` all set it, in any order and any
        spelling, and each removes the bound. Reusing _last_dir_action() is
        the point: a second list of "flags that clear the default" would be a
        second scanner for a grammar this one already parses, which is how
        this table grew defects before;
      * a FILE operand was actually charged. With none, ugrep's fallback is an
        implicit -R over the working directory and is UNBOUNDED, not depth-1
        -- so the default must not follow the walk into the cwd.

    That last test is spelled with roots(fallback=False) rather than a new
    parameter because depth_bound()'s signature is pinned: the suite
    monkeypatches a two-argument spy over it to prove check() reads the bounds
    only through the named accessors. Same call shape as
    unclaimed_absolute_root(), for the same reason.
    """
    depth = _scan_bounds(profile, argv)[0]
    if depth is not None or profile.default_depth is None:
        return depth
    if _last_dir_action(profile, argv) != "recurse_default":
        return None
    if not roots(profile, argv, "", stdin_is_tty=False, fallback=False):
        return None
    return profile.default_depth


def device_bounded(profile, argv):
    """Does a device flag keep this walk on the filesystem its root sits on?

    `find -xdev`/`-mount`, `du -x`/`--one-file-system`, `rg`/`fd
    --one-file-system`, `tree -x`. Each was run against a local directory
    holding network mounts, and each listed the mount points without
    entering them.

    This is NOT a bound in the sense bounded() means, and the two must not be
    merged: a depth bound makes any walk cheap, while a device bound only
    keeps a walk from CROSSING onto an expensive filesystem. `find /big
    -xdev` starts on one and still enumerates all of it. Which of the two
    offences it answers is decided per root, in judge_root().
    """
    return _scan_bounds(profile, argv)[1]


def depth_malformed(profile, argv):
    """Did a depth flag get a value the tool will refuse to run on?

    Missing (the flag was the last token, or a repeat left nothing for it)
    or non-numeric (empty, another flag's spelling, an ordinary word) --
    find and tree both validate as they parse, before opening anything, so
    either shape means the command errors out and walks NOTHING. That is a
    stronger fact than "unbounded": unbounded still gets judged against the
    mount, this does not get judged at all. check() reads this before
    judging any root.

    THE REAPER DELIBERATELY DOES NOT READ IT, and the reason is the same one
    that keeps bounded() out of traversal_roots(): the two consumers are not
    asked the same question. check() is handed an argv BEFORE the tool runs,
    so "this will error out during argument parsing" is a prediction about a
    command that has not started, and refusing a command that walks nothing
    is the false refusal that teaches people to alias around an advisory
    layer. The reaper is handed a process that EXISTS -- it is in the table,
    it has an age, and the arms that make a finding of it want it past
    budget or orphaned. For that process the prediction has already been
    falsified: the tool did not exit during argument parsing, so whatever
    this function models about that tool's validation does not hold for the
    binary actually running. Suppressing the finding on the strength of a
    prediction the evidence contradicts is exactly the clean bill of health
    Layer 2 must never give, and the resulting divergence sits in the safe
    direction -- Layer 1 allows, Layer 2 still names it.

    Written down here rather than left to be rediscovered, and asserted by
    `test_layer2_deliberately_reports_a_malformed_depth_that_layer1_allows`.
    """
    return _scan_bounds(profile, argv)[2]


def _scan_bounds(profile, argv):
    """(depth VALUE or None, device-bounded, malformed) from ONE pass.

    The first element is the last depth flag's value, NOT a verdict on it --
    None when the walk is unbounded or the value will not parse. It was a
    bool until the ceiling became per-mount, at which point the comparison
    could no longer happen here: which ceiling applies depends on the mount,
    and this function has never seen a mount table. `depth_bound()` is the
    public spelling of the value and `globally_bounded()` of the comparison.

    The third element, `malformed`, is a DIFFERENT fact from "unbounded":
    it means a depth flag occurred with no value the tool can parse at all
    (missing, or non-numeric), which is a fact the tool validates as it
    parses argv, before opening anything -- so the correct reading is not
    "no bound was given" but "this command errors out and walks nothing".
    Once True it stays True for the rest of the scan: a LATER valid
    occurrence does not undo an earlier malformed one, because find and
    tree both fail at the point they hit the bad one, before any later
    token is even reached. `depth_malformed()` is its accessor.

    One scan and not two, because every scanner here has to step over the same
    grammar -- exec bodies, `--`, a value flag's value, a cluster's glued
    value -- and the way this table grew a defect was a scanner that knew one
    of those rules and not the rest. Two loops is two places to fix it in;
    the shim's single loop reads both facts for exactly the same reason.

    Every caller goes through a NAMED accessor rather than unpacking this
    tuple -- check() through depth_bound() and device_bounded(), the reaper
    through device_bounded() -- at the cost of a pass per question in the
    consumer that does not run on the node. What it buys is that the
    accessors, which is where the measurements are written down, cannot go
    stale behind a caller that reads around them. check() unpacked the tuple
    directly for one commit and that is exactly what happened: the docstring
    here went on describing a bool it no longer returned.

    bounded() is the one accessor with no caller on either decision path, and
    that is not an oversight to be fixed by calling it. It composes
    depth_bound() with globally_bounded(), so check() -- which needs the
    VALUE, to hand to a per-mount comparison -- would be paying a second scan
    for a verdict it can compute from what it already has. The reaper skips
    it by policy (a shallow walk of an expensive mount that is stalling IO
    now is still worth naming). What it is for is the name: the shim's
    comments, the reaper's and ADR-0007's all refer to this repo's depth rule
    as bounded(), and the argv-level grammar it documents -- last flag wins,
    a depth flag is a setting and not a switch -- is asserted through it in
    the suite. It cannot drift the way it once did, because the ceiling it
    compares against now lives in globally_bounded() alone.
    """
    last = None
    device = False
    malformed = False
    # The floor a later depth occurrence may not fall below, for a tool whose
    # repeats form a RANGE rather than last-winning. See _apply_depth().
    floor = None

    def _apply_depth(span):
        """Fold one depth occurrence into `last` and `malformed`.

        ugrep's repeats are NOT last-wins. Measured on 7.8.4 over a nine-level
        tree, stdout and stderr read separately:

            --depth=1 --depth=5          walks 1..5
            --depth=1 --depth=3 --depth=5 walks 1..5
            --depth=5 --depth=1          `invalid argument -1`, walks nothing
            --depth=3,5 --depth=4        walks 3..4   -- VALID, not an error
            --depth=3,5 --depth=1        `invalid argument -1`
            --depth=,9 --depth=5         `invalid argument -5`
            -3-5-7                       walks 3..7

        So a later occurrence supplies the MAX and may not fall below a floor
        set by the first. The floor is the MIN when the first expression
        carries one (`3,5` -> 3, which is why `--depth=3,5 --depth=4` is
        accepted) and the value itself when it does not (`5` -> 5, `,9` -> 9,
        which is why `--depth=,9 --depth=5` is rejected). Both halves are
        measured; neither is inferred from the other.

        This rule is FITTED to the rows above, not derived from a documented
        grammar. Three models of ugrep's option parser that explain
        `--depth=3,5 --depth=4` being valid all predict `--depth=,9
        --depth=5` is valid too, and it is not. A shape not listed above is
        therefore an extrapolation rather than a consequence -- measure it
        before relying on it. The rejected alternative "a later value must be
        >= the previous MAX" is refuted by `--depth=3,5 --depth=4`.

        This exists because an earlier version of this branch claimed taking
        the last value gave the right verdict either way -- "a coincidence of
        the verdict function". It does not. That holds only while the smaller
        value is within the global ceiling: `--depth=9 --depth=5` errors and
        walks nothing, while last-wins read it as a bound of 5, still above
        the ceiling, and REFUSED. A false refusal on an argv the tool was
        going to reject on its own -- which is how this layer teaches people
        to alias around it.
        """
        nonlocal last, malformed, floor
        if span is None:
            malformed = True
            last = None
            return
        low, high = span
        if not profile.depth_range:
            # Every other tool here really is last-wins -- measured,
            # `find -maxdepth 9 -maxdepth 1` walks one level. Only a tool
            # whose repeats form a RANGE gets the floor below, or this would
            # call find's ordinary last-wins an error.
            last = high
            return
        if floor is None:
            floor = int(low) if low is not None else int(high)
        elif int(high) < floor:
            malformed = True
            last = None
            return
        last = high

    i = 1
    while i < len(argv):
        token = argv[i]
        if token in profile.exec_flags:
            # A depth flag inside a command template is data, not a bound:
            # `find DIR -exec echo visited -maxdepth 1 ';'` walks the whole
            # tree, verified against find(1).
            i = skip_exec_body(profile, argv, i)
            continue
        if token == "--" and profile.root_mode != "leading":
            # A depth flag after `--` is a pattern, not a bound. Any real
            # bound BEFORE it still counts, which is why this breaks out to
            # the decision below rather than returning False.
            #
            # find's grammar is the exception: `--` there precedes the path
            # OPERANDS and the predicates come after them and stay active, so
            # `find -- DIR -maxdepth 1` really is bounded to one level.
            break
        value = _depth_value(profile, argv, i)
        if value is _NO_DEPTH_VALUE:
            # The flag is real but argv ends before its value would. Not
            # "unbounded" -- the tool validates this as it parses and never
            # opens anything. See _NO_DEPTH_VALUE.
            malformed = True
            last = None
            i += 1
            continue
        if value is not None:
            # Malformed is applied HERE, immediately -- not deferred to a
            # check on whatever `last` happens to be when the scan ends, or a
            # later valid occurrence would launder this one away.
            _apply_depth(_depth_span(profile, value))
            # Only the separate spelling consumes a following token; `-d2` and
            # `--max-depth=2` carry their value inside this one.
            i += 2 if token in profile.depth_flags else 1
            continue
        setting = _device_setting(profile, token)
        if setting is not None:
            # Last-wins: rg's --no-one-file-system undoes an earlier
            # --one-file-system, measured on the tool.
            device = setting
            i += 1
            continue
        cl_letters, cl_vflag, cl_glued, cl_next = parse_short_cluster(
            profile, token)
        if cl_letters and profile.device_flags:
            # A short device flag reached inside a cluster is still one:
            # `du -sx /` and `tree -xL 2 DIR` are the spellings people
            # actually type. No `continue` -- the same token can carry a depth
            # bound too (`du -xd1 /`), and the cluster arm below reads that.
            for letter in cl_letters:
                if "-" + letter in profile.device_flags:
                    device = True
        if cl_letters and profile.depth_digits:
            # ugrep spells a depth as a bare digit run, and it clusters:
            # `-l9`, `-9l`, `-rl2`, and `-l10` which is TEN rather than `-1`
            # then `-0`. No `continue` -- the same token can still carry a
            # value flag, which the arm below has to step over.
            #
            # Scanned over cl_letters and NEVER over cl_glued, which is what
            # keeps `-lC3` a depth-1 walk: parse_short_cluster stops at the
            # first value-taking letter and hands its argument back as glued,
            # so the 3 is -C's context and is not here to be misread.
            #
            # The charset gate covers the OTHER collision, the one value_flags
            # cannot: -K, -m, -Z and -Q take OPTIONAL numeric arguments, so
            # listing them would make them eat a following token they do not
            # take (the --color rule), and their digits therefore arrive among
            # the letters. A token containing any character outside the set is
            # not scanned, so an unaudited flag costs a bound rather than
            # inventing one.
            if all(c in profile.depth_digits for c in cl_letters):
                # Each `N`, `N-M` or `N,M` in the residual is ONE depth
                # occurrence, handed to the same accumulator as the long
                # spellings -- so `-l9-3` is rejected for the same reason
                # `--depth=9,3` is, rather than quietly becoming a bound of 9.
                # Taking the max of the digit runs was that bug.
                for expr in _DEPTH_EXPR.findall(cl_letters):
                    # A lone 0 is `--null`, not a depth: `ugrep -l0 PAT dir`
                    # prints NUL-separated names at the default depth 1.
                    # Reading it as "bounded at 0" would make `-r0` -- an
                    # unbounded recursive walk -- look bounded. Only the BARE
                    # spelling; `0-3` really does carry a MAX.
                    if expr == "0":
                        continue
                    # `-` is the short separator and `,` the long one; the
                    # span parser knows only the comma, so normalise.
                    _apply_depth(_depth_span(profile, expr.replace("-", ",")))
        if cl_letters and cl_vflag is not None:
            # A DEPTH flag reached inside a cluster is still a bound:
            # `fd -Hd1 pat DIR` is `-H` plus `-d1`, a real depth-1 walk
            # (verified on the tool). Skipping the token wholesale refused it.
            # du still reaches the arm below, because -d stayed in its
            # value_flags and its value must be stepped over, but it no
            # longer sets a bound.
            if cl_vflag in profile.depth_flags:
                cluster_value = cl_glued
                if cluster_value is None and cl_next and i + 1 < len(argv):
                    cluster_value = argv[i + 1]
                # cl_next True with nothing left in argv means this depth
                # flag, glued into a cluster, has no value either -- same
                # "will not run" fact as the non-cluster case. Validated
                # immediately, like the non-cluster branch above, so this
                # occurrence cannot be laundered by a later valid one.
                if cluster_value is None:
                    malformed = True
                    last = None
                else:
                    _apply_depth(_depth_span(profile, cluster_value))
            i += 2 if cl_next else 1
            continue
        # An ABBREVIATED value flag consumes its value here too, so that value
        # is not a depth bound. `du --time-s --max-depth 1 DIR` has
        # `--time-style` take `--max-depth` as its style string -- accepted
        # WITHOUT validation, unlike --threshold or --block-size -- and du
        # then walks DIR unbounded. Measured on the tool: it printed a path
        # four levels deep.
        #
        # An earlier review REFUTED this exact change, and was right at the
        # time: it then broke `du --time --max-depth 1 DIR`, which real du
        # bounds, because `--time` is du's own optional-argument option and
        # the resolver saw only a unique prefix of `--time-style`. What
        # retired that objection is exact_flags, added later for a different
        # finding -- `--time` is now an exact option and is never resolved as
        # an abbreviation, while `--time-s` still is. The refutation outlived
        # the code it rested on and nobody revisited it for a long while.
        if resolve_for(profile, token.partition("=")[0], profile.value_flags):
            i += 1 if "=" in token else 2
            continue
        if _flag_takes_value(profile, token):
            # Skip the value, so a `--` sitting in it is not read as a marker.
            i += 2
            continue
        i += 1
    # The VALUE, not the verdict. Which ceiling applies depends on the mount,
    # and the mount is not known until judge_root() -- so the comparison
    # cannot live here any more. `last` is only ever assigned a value that
    # already passed _is_valid_depth() above, so int() here cannot raise --
    # unlike a bare `int(last)` on a value nobody had validated yet, which
    # is what let a malformed EARLIER occurrence hide behind a valid LATER
    # one when this only checked the final value.
    # A malformed depth clears the VALUE as well as setting the flag, at every
    # site that can set it. Two argvs the tool rejects identically must not
    # disagree here: `-d skip --depth=9 --depth=5` and `-d skip -l9-3` both
    # error and walk nothing, and while the first kept a stale 9 it read as a
    # traversal through is_traversal()'s depth arm while the second did not.
    # That reaches Layer 2, which calls is_traversal() -- narrowly, and in the
    # correct direction, but not "not at all".
    if last is None:
        return (None, device, malformed)
    return (int(last), device, malformed)


def _flag_takes_value(profile, token):
    return token in profile.value_flags


def _abbreviated_pattern_flag(profile, token):
    """`--rege pat` / `--rege=pat`: an abbreviation still supplies the pattern.

    Missing it leaves saw_pattern_flag false, so the first real PATH operand
    is charged against skip_pos as though it were the pattern -- the same
    root-loss shape as the attached and mid-cluster forms.
    """
    head = token.partition("=")[0]
    return resolve_for(profile, head, profile.pat_flags)


def _is_attached_pattern(profile, token):
    """Does `token` carry its pattern inline, as `--regexp=pat` or `-epat`?

    Recognizing pattern flags only as exact tokens meant an attached form left
    `saw_pattern_flag` false, so the FIRST PATH operand was charged against
    skip_pos as if it were the pattern -- `grep --regexp=pat -r -- /home` lost
    /home, fell back to the cwd, and was allowed from a cheap one. Both
    consumers made the same mistake, so the agreement tests stayed green.
    """
    for flag in profile.pat_flags:
        if token.startswith(flag + "="):
            return True
        # `-epat`: a two-character short flag with the pattern glued on.
        if len(flag) == 2 and flag[1] != "-" and token.startswith(flag) \
                and len(token) > 2:
            return True
    return False


def _drop_empty_operands(found):
    """Path operands that are the empty string, removed.

    They are real argv elements -- `find "$dir"` with dir unset -- and no
    wrapped tool walks anything for one. Measured on the tools: find, du, fd,
    grep and rg all error on it and carry on with the other operands, and
    `tree real ""` SEGFAULTS (v2.0.2), which is not an error message but is
    emphatically not a walk either. Keeping one and resolving it against the
    cwd (`clean_path("", cwd)` is the cwd) charges a directory the tool never
    opens.
    """
    return [f for f in found if f != ""]


def roots(profile, argv, cwd, stdin_is_tty=True, fallback=True):
    """Path operands this invocation will walk, uncleaned.

    An empty list means the tool walks nothing (fzf reading a pipe).

    `fallback=False` returns only what the scan actually CHARGED, without the
    default root. The distinction is not cosmetic: with the fallback applied,
    "the scan found no operand" and "the scan found the cwd" are the same
    answer, and telling them apart is the whole of
    unclaimed_absolute_root().
    """
    found = []
    positionals = []
    saw_pattern_flag = False

    i = 1
    while i < len(argv):
        token = argv[i]

        if token in profile.exec_flags:
            # Template tokens are DATA, and charging one as a root is the
            # unsafe direction here, not the safe one: `fd -x echo {}
            # /tmp/cheap` from an expensive cwd had /tmp/cheap charged as the
            # only root and was ALLOWED, while fd walked the cwd -- verified,
            # the output was `./deep /tmp/cheap` per cwd entry. An earlier
            # review called this "erring toward refusing"; that was backwards,
            # because a charged root stops the fall-back to the cwd.
            i = skip_exec_body(profile, argv, i)
            continue

        if token in profile.root_flags:
            if i + 1 < len(argv):
                found.append(argv[i + 1])
            i += 2
            continue
        # `--search-path=/big`, the attached spelling -- fd's real root flag,
        # and NOT `--base-directory`, which is a cwd_flag: the comment at the
        # top of Profile records why modelling that one as a root is wrong in
        # both directions. NOT for a leading-mode profile: `-f=/big` is a root
        # to this branch and an unknown predicate to the shim, whose leading
        # case arm matches `-f` exactly and drops everything after it. bfs
        # and find agree with the shim -- both reject the attached spelling
        # outright, so the command walks nothing at all -- which makes
        # charging a root here the one reading no consumer can act on, and
        # it suppressed the cwd fallback to boot: `bfs -f=/tmp/cheap` from an
        # expensive cwd was allowed by this table and refused by the shim.
        # The exact `-f PATH` form above stays, because that IS bfs's grammar
        # and the shim charges it too.
        matched_root_flag = False
        if profile.root_mode != "leading":
            for flag in profile.root_flags:
                if token.startswith(flag + "="):
                    found.append(token[len(flag) + 1:])
                    matched_root_flag = True
                    break
        if matched_root_flag:
            i += 1
            continue

        # An ABBREVIATED root flag still supplies a root, and missing that was
        # a false refusal rather than a bypass: `updatedb --database-r
        # /var/tmp/things` is accepted by updatedb (verified -- no
        # "unrecognized option"), but the value was discarded, the walk fell
        # back to default_root `/`, and a cheap explicit root was refused.
        # Before the generic abbreviated-value branch below, which would
        # otherwise swallow it as an ordinary value.
        #
        # updatedb was the only wrapped tool that both abbreviated and took a
        # root flag, and it is no longer wrapped, so no CASES row reaches this
        # arm today -- fzf's --walker-root is exact-only, because clap rejects
        # prefixes. Kept rather than deleted: the grammar is the tool's, not
        # updatedb's, and the next getopt_long tool with a root flag would
        # otherwise reintroduce a defect that took a review to find. Pinned
        # by test_an_abbreviated_root_flag_still_supplies_a_root.
        #
        # Ungated, unlike the attached form above, because resolve_for()
        # already gates it: no leading-mode profile abbreviates, so this arm
        # cannot charge a root the shim's leading block would miss. That is a
        # property of the table and not of this line, so it is asserted --
        # test_no_leading_mode_profile_abbreviates.
        head, sep, tail = token.partition("=")
        resolved_root = resolve_for(profile, head, profile.root_flags)
        if resolved_root:
            if sep:
                found.append(tail)
                i += 1
            else:
                if i + 1 < len(argv):
                    found.append(argv[i + 1])
                i += 2
            continue

        if token in profile.pat_flags:
            saw_pattern_flag = True
            i += 2
            continue

        if _is_attached_pattern(profile, token):
            # Long form: `--regexp=pat`. Nothing follows it to skip.
            saw_pattern_flag = True
            i += 1
            continue

        if _abbreviated_pattern_flag(profile, token):
            saw_pattern_flag = True
            i += 1 if "=" in token else 2
            continue

        cl_letters, cl_vflag, _cl_glued, cl_next = parse_short_cluster(
            profile, token)
        if cl_letters and cl_vflag is not None:
            # `-nefoo` and `-ne foo` both supply the pattern from -e, so the
            # first PATH operand must not be charged against skip_pos. Missing
            # this dropped /home and fell back to the cwd.
            if cl_vflag in profile.pat_flags:
                saw_pattern_flag = True
            i += 2 if cl_next else 1
            continue

        if _flag_takes_value(profile, token):
            i += 2
            continue

        # An ABBREVIATED value flag consumes its value here too, and this
        # scanner not knowing that was a bypass rather than a cosmetic gap.
        # `grep --di recurse hay` is `--directories recurse` -- so grep
        # RECURSES THE CWD with `hay` as the pattern, verified on the tool
        # (it matched deep/deeper/f.txt from the cwd). Reading `recurse` as
        # the pattern and `hay` as a path charged the wrong root entirely:
        # from an expensive cwd with a cheap-looking operand the walk was
        # ALLOWED. Placed after the pattern arms, because pat_flags are a
        # subset of value_flags and have to set saw_pattern_flag first.
        if resolve_for(profile, token.partition("=")[0], profile.value_flags):
            i += 1 if "=" in token else 2
            continue

        if token == "--":
            if (profile.root_mode == "leading"
                    and profile.dashdash == "end_of_options"):
                # bfs: `--` ends the OPTIONS and the operands carry on through
                # it, so this pass must not stop here. Stopping is what left
                # `bfs -- -f /big` uncharged -- this pass is the only one that
                # charges root_flags, and the re-derivation below cannot do it
                # -- so the table allowed a walk bfs really performs while the
                # shim refused it. Leading mode discards these positionals
                # anyway; only the `break` mattered.
                #
                # Gated on root_mode because the shim reads its dashdash
                # setting ONLY inside its leading block: outside it, `--` sets
                # its no-more-flags state and every later token is a
                # positional, which is what `extend`-and-`break` does below.
                # Without the gate a positional-mode profile spelled
                # `end_of_options` would go on flag-parsing here while the
                # shim stopped -- `grep -r -- -r /big` charging the cwd in one
                # consumer and /big in the other. No profile spells it that
                # way today, so this is the "next tool of that shape" guard,
                # not a live fix; the docstring's "ignored outside leading
                # mode" is only true with it. Pinned by
                # test_dashdash_is_read_only_in_leading_mode.
                i += 1
                continue
            positionals.extend(argv[i + 1:])
            break

        if token.startswith("-") and token != "-":
            if profile.root_mode == "leading" and _find_leading(profile, token) is None:
                # find's grammar: the first real expression token ends the
                # operand list. Anything after it is a predicate, not a path.
                #
                # _find_leading(), not `token not in FIND_PREFIX_FLAGS`: that
                # membership test knew only the exact-match flags and so ended
                # this pass on a GLUED one, `-O3` or `-j8`. This pass is where
                # root_flags are charged, and the re-derivation below skips
                # `-f` as skip2 on the assumption that it already happened --
                # so `bfs -O3 -f /big -name x` lost /big entirely and fell
                # back to the cwd, while the shim's own case arm (which has
                # always matched -O* by prefix) charged it and refused. A
                # divergence in the worst direction: Layer 2 saw a cheap cwd
                # where Layer 1 saw the whole expensive mount.
                break
            i += 1
            continue

        positionals.append(token)
        i += 1

    if profile.root_mode == "leading":
        # Re-derive: for find, operands are strictly the leading ones.
        positionals = []
        skip_next = False
        for token in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            leading = _find_leading(profile, token)
            if leading == "skip2":
                skip_next = True
                continue
            if leading == "skip":
                continue
            if token == "--" and (not positionals
                                  or profile.dashdash == "end_of_options"):
                # End of options, not end of operands: `find -- /big` walks
                # /big. Skipped before the `-` test below, which would
                # otherwise end the operand list on it.
                #
                # For bfs that holds wherever `--` appears, not only before the
                # first operand: `bfs d1 -- d2` exits 0 and walks BOTH, and
                # `bfs -- d1 -- d2` walks both too. Measured on 2.1 and 4.0.4.
                # find is the opposite once an operand has been seen -- `find
                # d1 -- d2` is "unknown predicate `--'", rc=1, walks nothing --
                # so it keeps the `not positionals` guard and ends the list
                # here, which charges d1 for a command that walks nothing. That
                # is a false refusal in the safe direction and belongs to the
                # "this command will not run" family, not this one.
                continue
            if token == "-":
                # A bare `-` is a PATH OPERAND and does not end the operand
                # list. Measured: with a real directory named `-` present,
                # `find - -name x` and `bfs - -name x` both walk it, rc=0;
                # with no such file they error on the dash and walk every
                # REMAINING operand anyway, so `find d1 - d2` walks both.
                #
                # Ending the list here charged d1 and lost d2 -- a NAMED
                # expensive path hidden from both consumers at every cwd, and
                # from the reaper entirely, since the argv parses and the
                # charged root is cheap.
                #
                # The first pass above already has this exception
                # (`token.startswith("-") and token != "-"`), and so does the
                # shim's positional ladder; this re-derivation was the one
                # place that read `-` as an expression token. Judged against
                # the cwd like any other relative operand -- whether `./-`
                # exists is not asked, exactly as for `find runs`.
                positionals.append(token)
                continue
            if token.startswith("-") or token in ("(", ")", "!", ","):
                break
            positionals.append(token)
        found.extend(positionals)
    elif profile.root_mode == "stdin":
        named = bool(found)
        found = _drop_empty_operands(found)
        if not found and stdin_is_tty and fallback and not named:
            found.append(cwd)
        return found
    else:
        skip = 0 if saw_pattern_flag else profile.skip_pos
        found.extend(positionals[skip:])

    # An empty path operand walks NOTHING, and it does not stop the others.
    # Measured on the tools: `find real "" -name '*.txt'` returns the hits
    # under `real` and errors only on the empty operand; `du -sh real ""`
    # still prints real's total. So it charges no root -- and it suppresses
    # the default-root fallback, because the caller DID name an operand and
    # inventing the cwd for it refuses a directory the tool never opens.
    #
    # That suppression was reverted once, as a bypass, and is restored here
    # because the late-pattern-flag defect was its only cause. The shim used
    # to decide skip_pos streaming while this decided it after the whole
    # scan, so for `grep -r /big "" -e pat` the shim skipped /big as the
    # pattern, declined the empty, declined the cwd too, and judged nothing
    # at all. Both consumers now resolve the pattern flag before slicing, so
    # `named` here and the shim's operand count count the same operands.
    #
    # `named` is taken AFTER the skip and BEFORE the filter, which is the only
    # placement that works: `grep -r "" /big` spends the empty on the
    # PATTERN, so it must not count as an operand naming nothing, while
    # `rg "" -e pat` has the pattern supplied by -e and the empty really is
    # the sole operand.
    named = bool(found)
    found = _drop_empty_operands(found)
    if not found and fallback and not named and (
            stdin_is_tty or not profile.fallback_needs_tty):
        # With nothing named, some tools walk the cwd and some read stdin, and
        # for ugrep WHICH one is decided by whether stdin is a terminal:
        # `ugrep PAT` at a prompt recurses the working directory, the same
        # argv on the end of a pipe filters the pipe and walks nothing. Layer
        # 1 tests `[ -t 0 ]` for free; Layer 2 reads /proc/<pid>/fd/0, because
        # assuming a terminal there would turn every long-lived
        # `ugrep --line-buffered -E ...` log filter into a traversal finding.
        found.append(cwd if profile.default_root == "." else profile.default_root)
    return found


class Refusal(object):
    """Why one invocation was refused, in the terms the message needs."""

    def __init__(self, tool, root, mount, fstype, reason):
        self.tool = tool
        self.root = root
        self.mount = mount
        self.fstype = fstype
        self.reason = reason

    def __repr__(self):
        return "Refusal(%s, root=%s, mount=%s, %s)" % (
            self.tool, self.root, self.mount, self.reason)


def resolved_roots(profile, argv, cwd, stdin_is_tty=True):
    """[(raw, absolute)] for every path this invocation will walk.

    The ONE place the coordinate-system change is applied, and both consumers
    of the rule table go through it. Applying it in each caller instead was a
    Layer 2 miss: check() applied effective_cwd() and the reaper's
    traversal_roots() did not, so `fd --base-directory /big pat` from a cheap
    cwd was refused by Layer 1 and reported by Layer 2 as walking nothing --
    the backstop blind to exactly the walk a user who bypasses the advisory
    shim would perform.

    Not applied inside roots() itself, because check() also needs the base to
    resolve what roots() returns, and applying effective_cwd() twice is wrong
    rather than merely wasteful: a RELATIVE --base-directory resolved against
    an already-changed base gives the wrong directory. One call, one place,
    both callers.
    """
    base = effective_cwd(profile, argv, cwd)
    return [(raw, clean_path(raw, base))
            for raw in roots(profile, argv, base, stdin_is_tty)]


def judge_root(path, mounts, policy, device_bound=False, depth=None):
    """offending_mount(), minus the offences a bound makes impossible.

    The two reasons are not equally answerable. `descends_into` says the walk
    STARTS somewhere cheap and will cross onto an expensive mount, which is
    exactly what `-xdev` prevents -- so with a device bound there is no
    offence to report. `at_or_near_root` says the walk starts ON the expensive
    mount, and no device flag helps: `find /big -xdev` still enumerates the
    whole mount.

    Shared by both consumers on purpose. The one time this decision existed in
    two places -- check() applying the --base-directory change and the
    reaper's traversal_roots() not -- the backstop went blind to exactly the
    walk a user who bypasses the shim performs. Layer 2 naming a mount the
    process provably never enters is the same drift pointing the other way,
    and a finding that cannot survive being shown to its subject is not one
    this repo makes.
    """
    hit = offending_mount(path, mounts, policy)
    if not hit:
        return None
    if device_bound and hit[2] == "descends_into":
        return None
    # The depth ceiling is per-mount, so it can only be applied once the
    # mounts are known -- which is here, and was the reason for moving it out
    # of _scan_bounds(). check() has already accepted anything within the
    # global ceiling, so reaching this with a depth means the caller asked for
    # MORE than the default and these mounts are being asked whether they
    # allow it.
    if depth is not None and depth <= root_depth_allowance(path, mounts, hit, policy):
        return None
    return hit


def root_depth_allowance(path, mounts, hit, policy):
    """The deepest bound that still counts as bounded for a walk from `path`.

    `at_or_near_root` walks one mount, so that mount's allowance is the whole
    answer.

    `descends_into` walks EVERY expensive mount below `path`, so the allowance
    has to hold for all of them -- the strictest wins. Using the mount
    offending_mount() picked was wrong twice over. It made the verdict depend
    on a ranking written to choose what the refusal SAYS, and that ranking
    ties for two mounts of one type and was broken differently in each
    consumer, so `find / -maxdepth 4` was allowed by this table and refused
    by the shim -- and flipped in the shim when two lines of /proc/mounts
    were swapped. It was also wrong on its own terms: with a loosened home
    mount, a walk from `/` inherited the home's ceiling and enumerated the
    largest mount underneath it, which is the mount the ceiling exists to
    keep tight.

    The minimum is deliberately not adjusted for how far each mount sits
    below `path`. `find / -maxdepth 4` reaches only 3 levels into /home, so
    subtracting the offset would be a truer cost model -- and it would also
    mean a walk of the whole root is judged by an allowance measured for a
    walk of one home. Depth from a different origin is a different question;
    answering the strict one keeps a per-mount allowance a statement about
    that mount.
    """
    if hit[2] != "descends_into":
        return depth_allowance(hit[0], policy)
    return min(depth_allowance(row[0], policy)
               for row in descended_mounts(path, mounts))


def tool_from_argv(argv, comm=None):
    """The wrapped tool this argv runs, or None.

    argv[0]'s basename first, `comm` only as a fallback -- and that order is
    the whole point. `comm` is 15 bytes of a name the process may overwrite,
    and on a login node the thing overwriting it is usually not the tool.
    Several live `bfs` traversals at a reference deployment reported a
    version string as comm while argv[0] read `bfs`, and Layer 2 keyed every
    decision it makes on comm alone, matched against the wrapped names. All
    of them were invisible to the backstop -- not merely unclassified, but
    falling past every arm in classify() and leaving NO audit record at all,
    including one unbounded walk of / that had been running for days.

    The first reading of that was bfs renaming itself. It is not: the string
    was a coding agent's own version. That agent's shell snapshot defines
    FUNCTIONS `grep()`, `rg()` and `bfs()` that run `( exec -a <name>
    <agent binary> ... )`, and that wrapper is what sets comm -- for grep,
    rg, bfs and ugrep alike. So the rule is considerably better supported
    than the first reading knew: argv-first resolution is load-bearing for
    every tool such an agent runs, not for one self-renaming binary. Never
    key a tool identity on comm.

    argv[0] is also a name the process chooses, so this is not a trust
    boundary and is not meant to be one: Layer 2 reports, it does not enforce,
    and a wrapped tool that hides from the report costs its owner the
    explanation, not the guard its correctness. What argv[0] buys is that it
    is the name the tool was INVOKED as, which is what the rule table is
    written in terms of -- check() has always resolved it this way.

    comm stays as the fallback for the case argv cannot answer: an empty
    /proc/PID/cmdline, which a process can also arrange.
    """
    name = None
    if argv:
        name = argv[0].rsplit("/", 1)[-1]
    if name in PROFILE_BY_NAME:
        return name
    if comm in PROFILE_BY_NAME:
        return comm
    return None


def unclaimed_absolute_root(profile, argv):
    """An absolute path in argv that the operand scan never charged, or None.

    The fail-safe for a grammar this table does not model. When the scan finds
    NO operand it falls back to the default root -- the cwd for find -- and
    that fallback is right for `find -name foo` and wrong for
    `bfs -S dfs / -name foo`, where the scan stopped at an unmodelled leading
    option and `/` went unseen. Both look identical from the outside: roots
    fell back. The tell is that the second one has an absolute path sitting in
    argv that nothing accounted for.

    Deliberately only consulted when the scan charged NOTHING. Checking for
    any uncharged absolute path would fire on `find /big -newer /etc/fstab`,
    where /etc/fstab is a predicate's value and the scan is perfectly
    correct.

    This is a REPORTING signal, for Layer 2, and not a refusal: `find -newer
    /etc/fstab` with no path operand would trip it, and an advisory layer that
    refuses a correct command is how people learn to alias around it. Layer 1
    gets precision from the flag lists instead; Layer 2 gets suspicion from
    this, because a backstop that stays silent about a grammar it could not
    parse is the one answer it must never give.
    """
    if profile.root_mode != "leading":
        # Only find's grammar has the cliff this looks for: "the first
        # expression token ends the operand list". Every other mode consumes
        # the whole argv, so an absolute path it did not charge is a pattern
        # or a flag's value, not a missed root -- `grep -r /etc/passwd`
        # searches the cwd FOR that string, and reporting it as an unparsed
        # root would be noise in the trail this layer exists to keep clean.
        return None
    if roots(profile, argv, "", stdin_is_tty=False, fallback=False):
        return None
    for token in argv[1:]:
        if token.startswith("/"):
            return token
    return None


def check(argv, cwd, mounts, policy, stdin_is_tty=True):
    """The whole decision, as the shim makes it. None means allow.

    `mounts` is read_mounts()'s output -- the expensive rows only -- and
    `policy` the site's Policy. Kept in Python as well as generated into `sh`
    so the argv matrix in tests/argv_cases.py can assert against the table
    itself, and the shim tests can assert the generated dispatcher agrees
    with it.
    """
    if not argv:
        return None
    tool = argv[0].rsplit("/", 1)[-1]
    profile = PROFILE_BY_NAME.get(tool)
    if profile is None:
        return None
    if not is_traversal(profile, argv):
        return None
    # A depth flag with a missing or non-numeric value makes the tool exit
    # before it opens anything -- find: "Expected a positive decimal
    # integer argument to -maxdepth"; tree: "Missing argument to -L
    # option" -- so ALLOW is correct regardless of what any root would
    # otherwise judge to. Checked before the fast accept because it is a
    # stronger fact than any bound: this is not "no bound was given", it is
    # "nothing will be walked at all".
    if depth_malformed(profile, argv):
        return None
    # Fast accept: a bound within the global ceiling is allowed on EVERY mount,
    # so it needs no mount lookup. This keeps the early exit that every
    # currently-allowed bounded call takes -- only a bound LOOSER than the
    # global default falls through to the per-mount judgement below, where
    # only a mount with a `maxdepth` override in policy.mounts can allow it.
    # Every other mount refuses there, exactly as before.
    #
    # depth_bound() then globally_bounded(), NOT bounded() -- which composes
    # exactly those two and would therefore rescan argv for a value this
    # already has. Spelling the fast accept `if bounded(...)` reads better by
    # one line and buys a redundant pass whose only purpose is keeping a
    # function called; that is not the same as the function being needed, and
    # _scan_bounds()'s docstring says so rather than claiming otherwise.
    depth = depth_bound(profile, argv)
    if globally_bounded(depth, policy):
        return None
    device_bound = device_bounded(profile, argv)
    for raw, path in resolved_roots(profile, argv, cwd, stdin_is_tty):
        hit = judge_root(path, mounts, policy, device_bound, depth)
        if hit:
            return Refusal(tool, raw, hit[0], hit[1], hit[2])
    return None
