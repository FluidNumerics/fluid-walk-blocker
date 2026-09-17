"""The shim's runtime seams are audited when -- and only when -- they change
the outcome (ADR-0001, ADR-0013).

`WALK_BLOCKER_MOUNTS`, `WALK_BLOCKER_FSTYPES`, `WALK_BLOCKER_DEPTH_BY_MOUNT`
and `WALK_BLOCKER_SHIM_DIR` exist so the suite can drive the shim against a
fixture; each is also a way around it. Pointed at an empty table, a narrowed
type list, a loosened ceiling or a different binary directory, the shim sees
nothing to refuse and allows -- silently, unlike `WALK_BLOCKER_UNSCOPED`. So
every seam has a compiled `_TRUSTED` twin, and when the live verdict is an
allow the shim re-judges the same argv against the trusted values in a
subshell and writes an `unaudited_seam` record if the trusted judgement
differs. A seam that was set but did not alter the verdict is not a record
worth writing; these tests pin both halves.

The fixture site compiles the fake mount table in as the trusted table and
points the logger at a path that does not exist, so no test here touches the
journal; the records are read back through the `WALK_BLOCKER_AUDIT` file sink.
"""

import json
import os
import subprocess

import pytest

from walk_blocker import search_rules as R
from conftest import SHIM_SEAMS, SHIM_SH, run_shim


@pytest.fixture(autouse=True)
def _fixture_home(fixture_home):
    """Every test here judges paths under the fixture `/home`."""


def _record(audit):
    assert audit.exists(), "an unaudited seam measures nothing"
    lines = [line for line in audit.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


# --------------------------------------------------------------------------
# WALK_BLOCKER_MOUNTS
# --------------------------------------------------------------------------

def test_mounts_seam_is_audited_when_it_suppresses_a_real_refusal(shim_env, tmp_path):
    empty = tmp_path / "empty-mounts"
    empty.write_text("")
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_MOUNTS": str(empty), "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout, "still advisory: the real binary still ran"
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_MOUNTS"
    assert entry["mount"] == "/scratch"
    assert entry["fs"] == "wekafs"
    assert entry["reason"] == "at_or_near_root"


def test_mounts_seam_stays_quiet_when_the_override_matches_the_trusted_table(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/var/log", "-name", "x"],
                      env={"WALK_BLOCKER_MOUNTS": shim_env["mounts"], "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert not audit.exists(), "no divergence from the trusted table, nothing to audit"


def test_mounts_seam_stays_quiet_on_a_refusal(shim_env, tmp_path):
    """A refusal is its own record; the shadow runs only on an allow."""
    empty = tmp_path / "empty-mounts"
    empty.write_text("")
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_MOUNTS": shim_env["mounts"], "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == R.EXIT_REFUSED
    entry = _record(audit)
    assert entry["action"] == "refused", "the refusal's own record, and no seam record"


def test_an_empty_mounts_seam_is_the_compiled_table(shim_env):
    """`:-`, so an explicit empty value falls back rather than opening
    nothing -- and therefore never diverges."""
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"], env={"WALK_BLOCKER_MOUNTS": ""})
    assert result.returncode == R.EXIT_REFUSED


# --------------------------------------------------------------------------
# WALK_BLOCKER_FSTYPES
# --------------------------------------------------------------------------

def test_fstypes_seam_is_audited_when_it_suppresses_a_real_refusal(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_FSTYPES": "nfs4", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_FSTYPES"
    assert entry["mount"] == "/scratch"


def test_fstypes_seam_handles_a_multi_entry_override(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_FSTYPES": "nfs4:tmpfs", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    entry = _record(audit)
    assert entry["seam"] == "WALK_BLOCKER_FSTYPES"
    assert entry["mount"] == "/scratch"


def test_fstypes_seam_stays_quiet_when_the_override_matches_the_default(shim_env, tmp_path, policy):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/var/log", "-name", "x"],
                      env={"WALK_BLOCKER_FSTYPES": ":".join(policy.remote_fstypes),
                           "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert not audit.exists()


def test_fstypes_seam_cannot_hide_a_mount_that_is_remote_by_proxy(shim_env, tmp_path):
    """Narrowing the type list to nothing that matches still leaves tier
    three: `/archive` has a `host:` source, so it stays expensive and the
    seam changes nothing -- no allow, no seam record; only the refusal's own."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/archive", "-name", "x"],
                      env={"WALK_BLOCKER_FSTYPES": "wekafs", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == R.EXIT_REFUSED
    entry = _record(audit)
    assert entry["action"] == "refused"
    assert entry["mount"] == "/archive"


def test_fstypes_seam_and_table_agree(shim_env, node_fs, policy, monkeypatch):
    """Both consumers read the same variable: the table through
    Policy.from_env, the shim live. Verdicts must match under it."""
    monkeypatch.setenv(R.FSTYPES_SEAM, "nfs4")
    monkeypatch.delenv(R.DEPTH_BY_MOUNT_SEAM, raising=False)
    live = R.Policy.from_env(policy)
    mounts = R.read_mounts(node_fs["mounts"], live)
    for argv in (["find", "/scratch", "-name", "x"], ["find", "/archive", "-name", "x"],
                 ["find", "/home/me", "-name", "x"], ["grep", "-r", "pat", "/"]):
        table = R.check(argv, "/", mounts, live) is not None
        shim = run_shim(shim_env, argv, env={"WALK_BLOCKER_FSTYPES": "nfs4"}).returncode == 2
        assert table == shim, argv


# --------------------------------------------------------------------------
# WALK_BLOCKER_DEPTH_BY_MOUNT
# --------------------------------------------------------------------------

def test_depth_by_mount_seam_is_audited_when_it_suppresses_a_real_refusal(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    # 6, not something past depth_allowance_max: a value above it would test
    # the drop-toward-restrictive rule by accident instead of the audit.
    result = run_shim(shim_env, ["find", "/scratch", "-maxdepth", "5"],
                      env={"WALK_BLOCKER_DEPTH_BY_MOUNT": "/scratch=6",
                           "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_DEPTH_BY_MOUNT"
    assert entry["mount"] == "/scratch"


def test_depth_by_mount_seam_stays_quiet_when_the_override_matches_the_default(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/var/log", "-maxdepth", "5"],
                      env={"WALK_BLOCKER_DEPTH_BY_MOUNT": "/home=4", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert not audit.exists()


def test_a_depth_by_mount_override_past_the_allowance_max_falls_back(shim_env):
    """Dropped, not clamped: `/home=999` falls back to the global ceiling,
    exactly as `_parse_depth_by_mount` does."""
    result = run_shim(shim_env, ["find", "/home/me", "-maxdepth", "9"],
                      env={"WALK_BLOCKER_DEPTH_BY_MOUNT": "/home=999"})
    assert result.returncode == 2
    # ...and it took the compiled 4 away too, since the variable is present.
    result = run_shim(shim_env, ["find", "/home/me", "-maxdepth", "4"],
                      env={"WALK_BLOCKER_DEPTH_BY_MOUNT": "/home=999"})
    assert result.returncode == 2


def test_a_present_but_empty_depth_seam_clears_every_allowance(shim_env, policy, monkeypatch):
    """Presence, not non-emptiness, is the trigger -- matching Policy.from_env."""
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, "")
    assert R.depth_allowance("/home", R.Policy.from_env(policy)) == policy.maxdepth_allowed
    result = run_shim(shim_env, ["find", "/home/me", "-maxdepth", "4"],
                      env={"WALK_BLOCKER_DEPTH_BY_MOUNT": ""})
    assert result.returncode == 2
    assert run_shim(shim_env, ["find", "/home/me", "-maxdepth", "4"]).returncode == 0


def test_depth_seam_drops_malformed_pairs_like_the_table(shim_env, policy, monkeypatch):
    seam = "home=4:/home=x:/home=:=4:/home/=3"
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, seam)
    monkeypatch.delenv(R.FSTYPES_SEAM, raising=False)
    live = R.Policy.from_env(policy)
    assert R.depth_allowance("/home", live) == 3  # only the trailing-slash pair survives
    env = {"WALK_BLOCKER_DEPTH_BY_MOUNT": seam}
    assert run_shim(shim_env, ["find", "/home/me", "-maxdepth", "3"], env=env).returncode == 0
    assert run_shim(shim_env, ["find", "/home/me", "-maxdepth", "4"], env=env).returncode == 2


def test_depth_seam_adds_an_expensive_override_for_an_unlisted_mount(
        shim_env, node_fs, policy, tmp_path, monkeypatch):
    """Policy.from_env adds `(expensive, N)` for a mount the compiled table
    did not list, which also changes its CLASS. The shim does the same, so a
    tmpfs named in the seam is guarded in both consumers."""
    seam = "/run=1"
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, seam)
    monkeypatch.delenv(R.FSTYPES_SEAM, raising=False)
    live = R.Policy.from_env(policy)
    mounts = R.read_mounts(node_fs["mounts"], live)
    assert "/run" in [row[0] for row in mounts]
    env = {"WALK_BLOCKER_DEPTH_BY_MOUNT": seam}
    for argv in (["find", "/run", "-name", "x"], ["find", "/run", "-maxdepth", "1"]):
        table = R.check(argv, "/", mounts, live) is not None
        result = run_shim(shim_env, argv, env=env)
        assert (result.returncode == 2) == table, (argv, result.stderr.decode())


def test_fstypes_and_depth_by_mount_set_together_are_still_audited(shim_env, tmp_path):
    """One combined shadow, forcing both to trusted at once: each seam's own
    re-judge would otherwise still see the other override's effect."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-maxdepth", "5"],
                      env={"WALK_BLOCKER_FSTYPES": "nfs4",
                           "WALK_BLOCKER_DEPTH_BY_MOUNT": "/scratch=6",
                           "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_FSTYPES+WALK_BLOCKER_DEPTH_BY_MOUNT"
    assert entry["mount"] == "/scratch"


# --------------------------------------------------------------------------
# WALK_BLOCKER_SHIM_DIR
# --------------------------------------------------------------------------

def test_shim_dir_seam_is_audited_when_it_changes_the_resolved_binary(shim_env, tmp_path):
    """The seam only decides which PATH entry sg_resolve skips. The installer
    sets it to the directory the shim was found in, so a mismatch is never
    the legitimate case, and pointing it elsewhere changes which real binary
    runs. PATH here never contains the shim's own directory, so a wrong value
    cannot make sg_resolve find the shim itself."""
    bin_a = tmp_path / "bin-a"
    bin_b = tmp_path / "bin-b"
    for label, d in (("A", bin_a), ("B", bin_b)):
        d.mkdir()
        stub = d / "find"
        stub.write_text("#!/bin/sh\nprintf 'RAN %s\\n'\nexit 0\n" % label)
        stub.chmod(0o755)
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/var/log", "-name", "x"],
                      env={"PATH": "%s:%s" % (bin_a, bin_b),
                           "WALK_BLOCKER_SHIM_DIR": str(bin_a),
                           "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN B" in result.stdout
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_SHIM_DIR"
    assert entry["resolved"] == str(bin_b / "find")
    assert entry["shadow_resolved"] == str(bin_a / "find")


def test_shim_dir_seam_stays_quiet_when_it_matches_the_shims_own_dir(shim_env, tmp_path):
    """The production shape: a plain string compare against $0 -- no fork, no
    audit -- on every ordinary allowed command."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/var/log", "-name", "x"],
                      env={"WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    assert not audit.exists()


def test_the_compiled_bin_dir_is_skipped_without_any_seam(make_shim_env, tmp_path):
    """The install's own `$prefix/bin` is never resolved back into, seam or
    no seam: with the shim installed THERE, PATH holding it first, and the
    seam unset, the real tool behind it still runs."""
    prefix = tmp_path / "prefix"
    env = make_shim_env(**{"install.prefix": str(prefix)})
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    os.symlink(os.path.join(env["shim_dir"], "find"), str(bin_dir / "find"))
    real = tmp_path / "real"
    real.mkdir()
    (real / "find").write_text("#!/bin/sh\nprintf 'RAN real\\n'\nexit 0\n")
    (real / "find").chmod(0o755)
    import subprocess
    from conftest import SHIM_SH, SHIM_SEAMS
    environ = {k: v for k, v in os.environ.items() if k not in SHIM_SEAMS}
    environ.pop("WALK_BLOCKER_SHIM_DIR", None)
    environ.update({"PATH": "%s:%s" % (bin_dir, real), "HOME": "/home/someone"})
    result = subprocess.run([SHIM_SH, str(bin_dir / "find"), "/var/log", "-name", "x"],
                            capture_output=True, env=environ, cwd="/")
    assert result.returncode == 0, result.stderr.decode()
    assert b"RAN real" in result.stdout
    assert "SG_BIN_DIR='%s'" % bin_dir in env["rendered"]["guard_text"]


# --------------------------------------------------------------------------
# the escape hatch record
# --------------------------------------------------------------------------

def test_escape_hatch_record_describes_the_real_judgement(shim_env, tmp_path):
    """`escape_hatch` carries the mount judgement it overrode -- root, mount,
    fs, reason -- with an empty seam field, so one record shape serves the
    hatch and the seams alike."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/", "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    entry = _record(audit)
    assert entry["action"] == "escape_hatch"
    assert entry["seam"] == ""
    assert entry["root"] == "/"
    assert entry["reason"] == "descends_into"
    assert entry["mount"] in ("/home", "/scratch")
    assert entry["fs"] == "wekafs"
    assert entry["tool"] == "find"
    assert entry["pwd"] == "/"


def test_escape_hatch_and_a_seam_together_record_the_hatch(shim_env, tmp_path):
    """With the hatch set, the refusal never happens and the hatch is the
    record; a seam that would also have been audited is not double-counted."""
    empty = tmp_path / "empty-mounts"
    empty.write_text("")
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_MOUNTS": str(empty),
                           "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    entry = _record(audit)
    assert entry["action"] == "unaudited_seam"
    assert entry["seam"] == "WALK_BLOCKER_MOUNTS"


# --------------------------------------------------------------------------
# refusals are recorded, not only printed
# --------------------------------------------------------------------------

def test_a_refusal_is_recorded_with_the_judgement_that_refused_it(shim_env, tmp_path):
    """The record carries what the message says: tool, root, the mount and
    type that judged it, the reason class, and who asked."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == R.EXIT_REFUSED
    assert b"REFUSED" in result.stderr
    entry = _record(audit)
    assert entry["action"] == "refused"
    assert entry["layer"] == "shim"
    assert entry["seam"] == ""
    assert entry["tool"] == "find"
    assert entry["root"] == "/scratch"
    assert entry["mount"] == "/scratch"
    assert entry["fs"] == "wekafs"
    assert entry["reason"] == "at_or_near_root"
    assert isinstance(entry["uid"], int) and entry["uid"] >= 0


def test_a_descending_refusal_records_descends_into(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/", "-name", "x"],
                      env={"WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == R.EXIT_REFUSED
    entry = _record(audit)
    assert entry["action"] == "refused"
    assert entry["reason"] == "descends_into"
    assert entry["root"] == "/"
    assert entry["fs"] == "wekafs"


def test_a_refusal_record_reaches_the_journal_sink(shim_env, logger_stub):
    """Through the documented fallback, since the fixture site's trusted
    logger does not exist; the trusted-path test lives beside the escape
    hatch's."""
    result = run_shim(
        shim_env, ["find", "/scratch", "-name", "x"],
        env={"PATH": "%s:%s:%s" % (shim_env["shim_dir"], logger_stub["bin"], shim_env["bin_dir"])})
    assert result.returncode == R.EXIT_REFUSED
    calls = [line for line in logger_stub["log"].read_text().splitlines() if line.strip()]
    assert len(calls) == 1, calls
    assert "-t walk-blocker" in calls[0]
    assert '"action":"refused"' in calls[0]
    assert '"mount":"/scratch"' in calls[0]


def test_an_allowed_call_records_nothing(shim_env, tmp_path, logger_stub):
    """The fast path stays silent as well as fork-free: a record per allowed
    grep would be a journal nobody reads."""
    audit = tmp_path / "audit.jsonl"
    result = run_shim(
        shim_env, ["find", "/home/me", "-maxdepth", "4"],
        env={"WALK_BLOCKER_AUDIT": str(audit),
             "PATH": "%s:%s:%s" % (shim_env["shim_dir"], logger_stub["bin"], shim_env["bin_dir"])})
    assert result.returncode == 0
    assert not audit.exists()
    assert logger_stub["log"].read_text().strip() == ""


def test_the_refusal_record_has_the_shape_of_the_escape_hatch_record(shim_env, tmp_path):
    """One record shape for every action, so a reader tells them apart by one
    field. The same argv under the hatch produces exactly one record, the
    hatch's -- the refusal record is written only when the refusal stands."""
    refused = tmp_path / "refused.jsonl"
    hatched = tmp_path / "hatched.jsonl"
    run_shim(shim_env, ["find", "/scratch", "-name", "x"],
             env={"WALK_BLOCKER_AUDIT": str(refused)})
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={"WALK_BLOCKER_AUDIT": str(hatched), R.ESCAPE_HATCH: "1"})
    assert result.returncode == 0
    a, b = _record(refused), _record(hatched)
    assert b["action"] == "escape_hatch"
    assert set(a) == set(b)
    for key in ("tool", "root", "mount", "fs", "reason", "uid"):
        assert a[key] == b[key], key


def test_a_refusal_survives_having_no_sink_at_all(shim_env):
    """No audit file, no trusted logger, no logger on PATH: the refusal still
    refuses, still explains itself, and says nothing about the sink."""
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"])
    assert result.returncode == R.EXIT_REFUSED
    assert result.stderr.startswith(b"REFUSED")
    assert b"logger" not in result.stderr.lower().replace(b"walk-blocker", b"")


def test_the_record_is_written_before_the_message(shim_env, tmp_path):
    """The record comes first, so a terminal closed mid-message cannot lose
    it. Pinned by a sink that measures how much of the message has reached
    stderr at the moment it is called: nothing, if the order is right."""
    stderr_file = tmp_path / "stderr.txt"
    seen = tmp_path / "stderr-bytes-at-record.txt"
    bin_dir = tmp_path / "ordering-bin"
    bin_dir.mkdir()
    stub = bin_dir / "logger"
    stub.write_text("#!/bin/sh\nwc -c < '%s' >> '%s'\nexit 0\n" % (stderr_file, seen))
    stub.chmod(0o755)
    env = dict(os.environ)
    for seam in SHIM_SEAMS:
        env.pop(seam, None)
    env.pop("PWD", None)
    env.update({
        "PATH": "%s:%s:%s" % (shim_env["shim_dir"], bin_dir, shim_env["bin_dir"]),
        "WALK_BLOCKER_SHIM_DIR": shim_env["shim_dir"],
        "HOME": "/home/someone",
    })
    with open(str(stderr_file), "wb") as err:
        result = subprocess.run(
            [SHIM_SH, os.path.join(shim_env["shim_dir"], "find"), "/scratch", "-name", "x"],
            stdout=subprocess.PIPE, stderr=err, env=env, cwd="/")
    assert result.returncode == R.EXIT_REFUSED
    assert stderr_file.read_bytes().startswith(b"REFUSED")
    sizes = seen.read_text().split()
    assert sizes == ["0"], "the sink was called once, after %s bytes of message" % sizes
