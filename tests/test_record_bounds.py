"""A Layer 1 record reaches the journal whole (issue #114).

logger(1) cuts a message at `--size`, 1 KiB unless told otherwise, and a cut
JSON record does not parse: a refusal from a deep enough working directory
arrived with no readable `action`. The shim now passes an explicit size and
bounds every string field in the awk that builds the record, marking a cut
field with `"<field>_truncated":true`.

The journal tests run the REAL logger, pointed with `-u` at a datagram
socket this test binds, so what is asserted is the datagram logger would
have handed journald -- and nothing reaches the node's own journal.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile

import pytest

from walk_blocker import search_rules as R
from conftest import run_shim

# A path segment long enough that a few of them make a few KiB, short enough
# to stay under NAME_MAX.
SEGMENT = "d" * 200


@pytest.fixture(autouse=True)
def _fixture_home(fixture_home):
    """The fixture `/home`, as the rest of the shim suite judges it."""


@pytest.fixture
def journal(shim_variant):
    """A shim whose trusted logger is the real logger(1), sending to a socket
    the test holds. Yields (shim_env, receive) where receive() returns the
    datagrams logger sent, decoded."""
    real = shutil.which("logger")
    if real is None:
        pytest.skip("logger(1) is not on PATH; the transport cannot be measured")
    # AF_UNIX paths are capped near 108 bytes and pytest's tmp_path can be
    # longer, so the socket lives in a short directory of its own. Pinned to
    # /tmp: a bare mkdtemp() honours TMPDIR, which can be just as long.
    sockdir = tempfile.mkdtemp(prefix="wb-log-", dir="/tmp")
    path = os.path.join(sockdir, "s")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(0.5)
    wrapper = os.path.join(sockdir, "logger")
    with open(wrapper, "w") as f:
        f.write("#!/bin/sh\nexec '%s' -u '%s' \"$@\"\n" % (real, path))
    os.chmod(wrapper, 0o755)

    def receive():
        got = []
        while True:
            try:
                got.append(sock.recv(1 << 20))
            except socket.timeout:
                return got

    try:
        yield shim_variant(SG_LOGGER="'%s'" % wrapper), receive
    finally:
        sock.close()
        shutil.rmtree(sockdir)


def _message(datagrams):
    """The one record's MESSAGE, parsed. The syslog header ends at the tag."""
    assert len(datagrams) == 1, datagrams
    raw = datagrams[0]
    head, sep, body = raw.partition(b"walk-blocker: ")
    assert sep, raw[:120]
    return json.loads(body.decode("utf-8"))


def _deep_dir(base, levels):
    path = str(base)
    for _ in range(levels):
        path = os.path.join(path, SEGMENT)
        os.mkdir(path)
    return path


def test_a_refusal_from_a_deep_directory_reaches_the_journal_as_json(journal, tmp_path):
    shim_env, receive = journal
    cwd = _deep_dir(tmp_path, 15)
    # Lexically the mount itself, so it is refused, and a few KiB long.
    root = "/scratch" + "/d/.." * 700
    assert len(cwd) > 3000 and len(root) > 3000
    result = run_shim(shim_env, ["find", root, "-name", "x"], cwd=cwd)
    assert result.returncode == R.EXIT_REFUSED, result.stderr
    record = _message(receive())
    assert record["action"] == "refused"
    assert record["mount"] == "/scratch"
    assert record["pwd_truncated"] is True
    assert record["root_truncated"] is True
    assert cwd.startswith(record["pwd"]) and len(record["pwd"]) == 1024
    assert root.startswith(record["root"]) and len(record["root"]) == 1024


def test_an_ordinary_record_carries_no_truncation_marker(journal):
    shim_env, receive = journal
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"])
    assert result.returncode == R.EXIT_REFUSED, result.stderr
    record = _message(receive())
    assert record["action"] == "refused"
    assert record["root"] == "/scratch"
    assert [k for k in record if k.endswith("_truncated")] == []


def _record_awk(guard_text):
    """The awk program that builds the record, as rendered."""
    start = guard_text.index("\"$sg_awk\" '") + len("\"$sg_awk\" '")
    return guard_text[start:guard_text.index("}'\n", start) + 1]


FIELDS = ("TS", "ACTION", "SEAM", "TOOL", "ROOT", "MOUNT", "FS", "REASON",
          "RESOLVED", "SHADOW", "PWD")


def _run_record_awk(guard_text, **fields):
    awk = shutil.which("awk")
    env = {"LC_ALL": "C", "SG_J_UID": str(2 ** 32 - 2)}
    for name in FIELDS:
        env["SG_J_" + name] = fields.get(name, "")
    out = subprocess.run([awk, _record_awk(guard_text)], env=env,
                         stdout=subprocess.PIPE, check=True).stdout
    return out


def _log_size(guard_text):
    return int(re.search(r"\nSG_LOG_SIZE=(\d+)\n", guard_text).group(1))


def test_every_field_at_once_stays_under_the_logger_size(rendered_shim):
    """The worst case the shim cannot produce in one run -- every string
    field oversized -- with room left for the syslog header."""
    text = rendered_shim["guard_text"]
    out = _run_record_awk(text, **{name: "\\" * 5000 for name in FIELDS})
    record = json.loads(out.decode("utf-8"))
    assert len(out) <= _log_size(text) - 1024, len(out)
    keys = {"ts", "action", "seam", "tool", "root", "mount", "fs", "reason",
            "resolved", "shadow_resolved", "pwd"}
    assert {k[:-len("_truncated")] for k in record if k.endswith("_truncated")} == keys


@pytest.mark.parametrize("tail", ["\\", '"', "\n", "é", "€", "\U0001F600"])
@pytest.mark.parametrize("shift", range(4))
def test_a_cut_never_splits_an_escape_or_a_character(rendered_shim, tail, shift):
    """The cut lands at every offset into a two-byte escape and a two-, three-
    and four-byte UTF-8 character, and the field still decodes."""
    value = "x" * (1020 + shift) + tail * 400
    out = _run_record_awk(rendered_shim["guard_text"], ROOT=value)
    record = json.loads(out.decode("utf-8"))
    assert record["root_truncated"] is True
    assert value.startswith(record["root"])
    assert len(json.dumps(record["root"], ensure_ascii=False)[1:-1].encode()) <= 1024


def test_the_runbook_states_the_size_and_bounds_the_code_uses():
    """docs/operating.md tells an operator what a record can hold and what the
    node's logger must accept; the numbers there are the ones the shim and the
    reconcile use, and the logger requirement is a prerequisite."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "docs", "operating.md")) as f:
        doc = " ".join(f.read().split())
    with open(os.path.join(repo, "node", "shim", "guard.sh.in")) as f:
        guard = f.read()
    with open(os.path.join(repo, "node", "shim", "install.sh")) as f:
        install = f.read()
    size = re.search(r"\nSG_LOG_SIZE=(\d+)\n", guard).group(1)
    assert re.search(r"\nSG_LOG_SIZE=%s\n" % size, install)
    assert "`logger --size %s`" % size in doc
    p, s = re.search(r"P = (\d+); S = (\d+)", guard).groups()
    mount = re.search(r"\nSG_REPORT_MOUNT_MAX=(\d+)\n", install).group(1)
    fstype = re.search(r"\nSG_REPORT_FSTYPE_MAX=(\d+)\n", install).group(1)
    assert "`pwd`) at %s bytes as escaped and every other string at %s" % (p, s) in doc
    assert "`mount` at %s and `fstype` at %s" % (mount, fstype) in doc
    # The marker means the bound cut the field, not that it is byte-for-byte
    # what the caller had: control characters were rewritten before the bound.
    assert "holds every field whole" not in doc
    prereq = doc[doc.index("## 1. Prerequisites"):doc.index("## 2.")]
    assert "accepts `--size`" in prereq


# --------------------------------------------------------------------------
# a caller's POSIXLY_CORRECT (issue #160)
# --------------------------------------------------------------------------

def _shim_awk(guard_text):
    """The trusted awk the rendered shim names, as an executable path."""
    value = re.search(r"\nSG_AWK=(.*)\n", guard_text).group(1).strip("'\"")
    if not os.access(value, os.X_OK):
        pytest.skip("the shim's trusted awk %s is not here" % value)
    return value


def _awk_halves_the_escape_under_posix(awk):
    """The defect's precondition, measured on the awk the shim uses: gawk
    writes one backslash for the replacement "\\\\" when POSIXLY_CORRECT is
    in its environment. An awk that does not is not exposed, and a test
    against it would pass whether or not the shim clears the variable."""
    program = 'BEGIN{s=ENVIRON["S"]; gsub(/\\\\/, "\\\\\\\\", s); print s}'
    out = subprocess.run([awk, program], stdout=subprocess.PIPE, check=True,
                         env={"S": "a\\q", "POSIXLY_CORRECT": "1"}).stdout
    return out == b"a\\q\n"


@pytest.mark.parametrize("posixly_correct", ["1", ""])
@pytest.mark.parametrize("shell", ("dash", "bash"))
def test_a_callers_posixly_correct_does_not_unescape_the_record(
        shim_env, rendered_shim, tmp_path, shell, posixly_correct):
    """The record's awk runs in the caller's environment, so the caller
    controls POSIXLY_CORRECT -- present-and-empty included, which gawk
    honours too. With it set, a backslash in a root reached the record
    bare and the line did not parse. The shim unsets it inside the
    substitution; the record must parse and give the root back exactly."""
    from conftest import shim_invocation

    awk = _shim_awk(rendered_shim["guard_text"])
    if not _awk_halves_the_escape_under_posix(awk):
        pytest.skip("%s keeps both backslashes under POSIXLY_CORRECT (not "
                    "gawk), so it cannot show the defect" % awk)
    real = shutil.which(shell)
    if real is None:
        pytest.skip("%s is not installed" % shell)
    # Under the name `sh`: bash enters POSIX mode from its basename.
    as_sh = tmp_path / ("as-sh-" + shell)
    as_sh.mkdir()
    os.symlink(real, str(as_sh / "sh"))

    root = "/scratch/a\\q"
    audit = tmp_path / "audit.jsonl"
    command, environ, cwd = shim_invocation(
        shim_env, ["find", root, "-name", "x"],
        env={"WALK_BLOCKER_UNSCOPED": "1", "WALK_BLOCKER_AUDIT": str(audit),
             "POSIXLY_CORRECT": posixly_correct})
    result = subprocess.run([str(as_sh / "sh")] + command, env=environ,
                            cwd=cwd, capture_output=True)
    assert result.returncode == 0, result.stderr
    lines = [line for line in audit.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, (
        "expected one escape-hatch record for %r, got %r" % (root, lines))
    record = json.loads(lines[0])
    assert record["root"] == root, record
