"""`walk-blocker build`: compile `site.toml` into the node payload (ADR-0013).

The whole payload is rendered in memory first -- a map of payload-relative
path to `(bytes, mode)` -- and only then written or compared. Rendering in
memory is what makes `--check` a pure comparison that never writes, and it
is what makes the output deterministic: nothing here reads a clock or a
hostname, every file's mode is set explicitly, and the same inputs render to
the same bytes.

Payload layout:

    .walk-blocker-build     build marker: this directory may be rebuilt
    deploy.py               node/deploy.py, stamped from [install], [hooks], [timer],
                            [trusted_binaries] and [site]; runs FROM this directory (0755)
    README.md               the repo README, verbatim
    docs/...                the docs tree, verbatim
    docs/what-to-run-instead.md  the users' page, rendered from node/docs/ and site.toml
    search_rules.py         the rule table, verbatim (the reaper imports it)
    survey.py               node/survey.py, verbatim
    walk-job                node/walk-job, stamped from [slurm] and the bfs pin
    reaper.py               node/reaper.py, stamped from [reaper] and [filesystems]
    shim/install.sh         node/shim/install.sh, stamped from [install], [hooks] and the mount policy
    site.toml               the input, byte for byte, as a record
    site.lock.json          schema/tool/payload versions and a hash per file
    shim/guard.sh           rendered from the rule table and site.toml (0755)
    shim/wrapped_names.sh   rendered likewise (0644)
    shim/measure.sh         node/shim/measure.sh, verbatim: the shim benchmark (0755)
    shim/measure-flags.sh   node/shim/measure-flags.sh, verbatim: the flag probe (0644)

A build refuses an output directory it did not create: one that exists, is
not empty, and has no `.walk-blocker-build` marker. One that carries the
marker is wiped and rebuilt. The payload is assembled beside the target and
renamed into place, so a failed build leaves the previous payload intact.
"""
import os
import shutil
import stat
import sys
import tempfile

from . import __version__, config, paths, stamp
from .render import alternatives, manifest
from .render.shim import RenderError, render_shim, render_wrapped_names

BUILD_MARKER = ".walk-blocker-build"

MODE_FILE = 0o644
MODE_EXEC = 0o755
MODE_DIR = 0o755

# Payload-relative paths that are executed directly rather than sourced or
# imported. Everything else is 0644.
EXECUTABLE = frozenset(["shim/guard.sh", "shim/install.sh", "shim/measure.sh", "walk-job",
                        "reaper.py", "deploy.py"])

EXIT_OK, EXIT_DIFFERS, EXIT_ERROR = 0, 1, 2


class BuildError(Exception):
    """Anything that stops a build: a config error, a refusal, a defect in a
    generated text. The message is the whole report."""


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _load_site(site_path):
    """`config.load_site`, with a failure worded as `validate` words it --
    from `config`, so this module has no dependency on the command-line
    layer."""
    try:
        return config.load_site(site_path)
    except config.LOAD_ERRORS as exc:
        raise BuildError(config.describe_load_error(site_path, exc))


def _tree_files(root):
    """Every regular file under `root`, as sorted relative POSIX paths.
    Hidden entries and `__pycache__` are not payload."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for name in filenames:
            if name.startswith(".") or name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def _generated(rel, text):
    """A rendered text is checked for an unfilled `@@PLACEHOLDER@@`, then
    encoded."""
    if "@@" not in text:
        return text.encode("utf-8")
    raise BuildError("%s: rendered text still contains '@@' (an unfilled placeholder)" % rel)


def _verbatim(rel, source):
    """A file copied byte for byte. It may carry a stamp marker only if
    `stamp.CONSUMERS` lists it -- otherwise the marker would never be
    stamped and never be checked, and the literal on the node would be
    whatever the source last said."""
    data = _read(source)
    if rel not in stamp.CONSUMERS:
        try:
            markers = stamp.find_markers(data.decode("utf-8"))
        except UnicodeDecodeError:
            markers = []
        if markers:
            raise BuildError("%s: carries a `%s` marker (line %d) but is not in "
                             "stamp.CONSUMERS; add the row or drop the marker"
                             % (rel, stamp.MARKER, markers[0].lineno))
    return data


def render_payload(site, site_bytes, version):
    """The payload as `{relpath: (bytes, mode)}`, manifest and marker
    included."""
    files = {}

    def put(rel, data):
        # The mode follows EXECUTABLE membership and nothing else, so a path
        # cannot be listed there and land 0644, or land 0755 unlisted.
        if rel in files:
            raise BuildError("%s: emitted twice" % rel)
        files[rel] = (data, MODE_EXEC if rel in EXECUTABLE else MODE_FILE)

    node = paths.node_dir()
    put("site.toml", site_bytes)
    put("README.md", _read(paths.readme_file()))
    # Clause 1 of the licence: a redistribution keeps the notice and the
    # terms. Copying this payload onto a node is a redistribution.
    put("LICENSE", _read(paths.license_file()))
    docs = paths.docs_dir()
    for rel in _tree_files(docs):
        put("docs/" + rel, _read(os.path.join(docs, rel)))
    # Verbatim copies. The benchmark and the flag probe are tools an operator
    # runs beside the shim; they carry no site value, so they ship as-is too.
    for rel, source in (
            ("search_rules.py", paths.rules_file()),
            ("survey.py", os.path.join(node, "survey.py")),
            ("shim/measure.sh", os.path.join(node, "shim", "measure.sh")),
            ("shim/measure-flags.sh", os.path.join(node, "shim", "measure-flags.sh"))):
        put(rel, _verbatim(rel, source))

    # Stamped consumers: the source is the same relative path under node/.
    values = stamp.SiteValues(site, version)
    for rel, required in sorted(stamp.CONSUMERS.items()):
        kind = "py" if rel.endswith(".py") else "sh"
        text = _read(os.path.join(node, rel)).decode("utf-8")
        try:
            text = stamp.stamp_text(text, values, kind)
            findings = stamp.check_text(text, values, kind, required)
        except stamp.StampError as exc:
            raise BuildError("%s: %s" % (rel, exc))
        if findings:
            raise BuildError("%s: %s" % (rel, "; ".join(findings)))
        put(rel, _generated(rel, text))

    policy = site.policy()
    try:
        put("shim/guard.sh", _generated("shim/guard.sh", render_shim(policy, site, version)))
        put("shim/wrapped_names.sh",
            _generated("shim/wrapped_names.sh", render_wrapped_names(policy, site, version)))
        # The users' page, rendered from the same site the shim was compiled
        # from, so the depth it quotes is the depth the shim applies.
        put(alternatives.PAYLOAD_PATH,
            _generated(alternatives.PAYLOAD_PATH,
                       alternatives.render_page(policy, site, version)))
    except RenderError as exc:
        raise BuildError(str(exc))

    hashed = {rel: data for rel, (data, _mode) in files.items()}
    lock = manifest.render_manifest(site_bytes, version, site.lookup("schema_version"),
                                    __version__, hashed)
    put(manifest.FILENAME, manifest.manifest_bytes(lock))
    put(BUILD_MARKER, ("%s\n" % version).encode("ascii"))
    return files


def _refuse_unless_ours(out_dir):
    """A non-empty directory without the marker was not made by a build."""
    if not os.path.exists(out_dir):
        return
    if not os.path.isdir(out_dir):
        raise BuildError("%s: exists and is not a directory" % out_dir)
    if os.listdir(out_dir) and not os.path.exists(os.path.join(out_dir, BUILD_MARKER)):
        raise BuildError("%s: refusing to overwrite a non-empty directory that has no "
                         "%s marker; a build only replaces what a build made"
                         % (out_dir, BUILD_MARKER))


def _write_tree(root, files):
    for rel in sorted(files):
        data, mode = files[rel]
        target = os.path.join(root, *rel.split("/"))
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "wb") as fh:
            fh.write(data)
        os.chmod(target, mode)
    for dirpath, dirnames, _filenames in os.walk(root):
        for d in dirnames:
            os.chmod(os.path.join(dirpath, d), MODE_DIR)
    os.chmod(root, MODE_DIR)


def _write_payload(out_dir, files):
    """Assemble beside `out_dir`, then swap it in. The temp dir is a sibling
    so the rename is a rename and not a copy across filesystems."""
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    staging = tempfile.mkdtemp(prefix=".walk-blocker-build.", dir=parent)
    try:
        _write_tree(staging, files)
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        os.rename(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _on_disk(out_dir):
    """`{relpath: (bytes, mode)}` for everything under `out_dir`, hidden
    files included: `--check` must see the marker and any stray file."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(out_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, out_dir).replace(os.sep, "/")
            found[rel] = (_read(full), stat.S_IMODE(os.lstat(full).st_mode))
    return found


def compare(expected, actual):
    """One line per difference between a rendered payload and a directory.
    Empty means identical, bytes and modes both."""
    lines = []
    for rel in sorted(set(expected) | set(actual)):
        if rel not in actual:
            lines.append("missing: %s" % rel)
        elif rel not in expected:
            lines.append("extra: %s" % rel)
        else:
            (want, want_mode), (have, have_mode) = expected[rel], actual[rel]
            if want != have:
                lines.append("differs: %s" % rel)
            elif want_mode != have_mode:
                lines.append("mode: %s is %04o, expected %04o" % (rel, have_mode, want_mode))
    return lines


def build(site_path, out_dir, check=False, out=None, err=None):
    """Build `site_path` into `out_dir`, or with `check` compare the two
    without writing. Returns the process exit code: 0 built or identical,
    1 `check` found a difference, 2 config error, refusal or build defect."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        site = _load_site(site_path)
        try:
            version = paths.read_version()
        except (OSError, ValueError) as exc:
            raise BuildError(str(exc))
        if not check:
            _refuse_unless_ours(out_dir)
        files = render_payload(site, _read(site_path), version)
        if check:
            if not os.path.isdir(out_dir):
                out.write("missing: %s (not a directory)\n" % out_dir)
                return EXIT_DIFFERS
            lines = compare(files, _on_disk(out_dir))
            for line in lines:
                out.write(line + "\n")
            return EXIT_DIFFERS if lines else EXIT_OK
        _write_payload(out_dir, files)
        out.write("%s: built %d files into %s\n" % (site_path, len(files), out_dir))
        return EXIT_OK
    except BuildError as exc:
        err.write("walk-blocker build: %s\n" % exc)
        return EXIT_ERROR
