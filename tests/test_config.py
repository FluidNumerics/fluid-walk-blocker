"""schema/site.schema.json and walk_blocker.config: the example validates,
the schema is a valid 2020-12 schema with a description on every key, and a
matrix of bad values is refused both by the library and by the CLI."""
import copy
import json
import os
import re
import subprocess
import sys

import jsonschema
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

from walk_blocker import cli, config  # noqa: E402

EXAMPLE = os.path.join(ROOT, "examples", "site.example.toml")


def load_example_dict():
    with open(EXAMPLE, "rb") as fh:
        return tomllib.load(fh)


# --- a small TOML writer: enough for the shapes site.toml uses -------------

def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(_inline(v) for v in value) + "]"
    raise TypeError(type(value))


def _inline(value):
    if isinstance(value, dict):
        return "{ " + ", ".join("%s = %s" % (k, _inline(v)) for k, v in value.items()) + " }"
    return _scalar(value)


def dump_toml(data, prefix=""):
    lines, tables, arrays = [], [], []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value) \
                and prefix != "reaper":
            arrays.append((key, value))
        else:
            lines.append("%s = %s" % (key, _scalar(value) if not isinstance(value, list)
                                       else "[" + ", ".join(_inline(v) for v in value) + "]"))
    out = "\n".join(lines) + ("\n" if lines else "")
    for key, value in tables:
        name = "%s.%s" % (prefix, key) if prefix else key
        out += "\n[%s]\n" % name + dump_toml(value, name)
    for key, value in arrays:
        name = "%s.%s" % (prefix, key) if prefix else key
        for item in value:
            out += "\n[[%s]]\n" % name + dump_toml(item, name)
    return out


REMOVE = object()


def set_dotted(data, dotted, value):
    """`filesystems.mounts[0].maxdepth` -> set (or REMOVE) that leaf."""
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", dotted)
    node = data
    for part in parts[:-1]:
        node = node[int(part[1:-1])] if part.startswith("[") else node.setdefault(part, {})
    leaf = parts[-1]
    key = int(leaf[1:-1]) if leaf.startswith("[") else leaf
    if value is REMOVE:
        del node[key]
    else:
        node[key] = value


def test_a_disabled_hook_still_needs_its_file(tmp_path):
    # The uninstall path removes a previously written hook; a disabled hook
    # with no file would leave the installer unable to name it.
    import walk_blocker.config as C
    src = open(os.path.join(ROOT, "examples", "site.example.toml"), encoding="utf-8").read()
    src = src.replace('file = "/etc/fish/conf.d/walk-blocker.fish"\n', "").replace(
        "[hooks.fish]\nenabled = true", "[hooks.fish]\nenabled = false")
    p = tmp_path / "site.toml"; p.write_text(src, encoding="utf-8")
    with pytest.raises(C.ConfigError) as exc:
        C.load_site(str(p))
    assert exc.value.path == "hooks.fish.file"


def test_the_toml_writer_round_trips_the_example():
    data = load_example_dict()
    assert tomllib.loads(dump_toml(data)) == data


# --- the schema itself ------------------------------------------------------

def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(config.load_schema())


def _properties(node, path=""):
    for name, sub in node.get("properties", {}).items():
        here = "%s.%s" % (path, name) if path else name
        yield here, sub
        yield from _properties(sub, here)
        if "items" in sub:
            yield from _properties(sub["items"], here + "[]")
    for name, sub in node.get("$defs", {}).items():
        yield from _properties(sub, "$defs." + name)


def test_every_property_has_a_description():
    missing = [path for path, sub in _properties(config.load_schema())
               if not sub.get("description", "").strip()]
    assert not missing, missing


def test_every_object_forbids_unknown_keys():
    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False, path
            for k, v in node.items():
                walk(v, path + "/" + k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s/%d" % (path, i))
    walk(config.load_schema(), "")


def test_schema_command_prints_the_schema_path(capsys):
    assert cli.main(["schema"]) == 0
    assert capsys.readouterr().out.strip() == config.schema_path()
    assert os.path.exists(config.schema_path())


# --- the example -------------------------------------------------------------

def test_example_validates_and_derives():
    site = config.load_site(EXAMPLE)
    assert site.derived() == {
        "audit_path": "/var/log/walk-blocker/searchguard-audit.jsonl",
        "bin_dir": "/usr/local/lib/walk-blocker/bin",
        "timer_floor_s": 160,
    }
    assert site.lookup("filesystems.mounts[0].maxdepth") == 4
    assert site.lookup("hooks.fish.gate") == "best-effort"
    assert "package" not in site.data["hooks"]["fish"]


def test_example_is_written_out_in_full():
    """Every default the schema declares appears literally in the example, so
    a reader sees the whole surface without opening the schema."""
    raw = load_example_dict()
    filled = config.load_site(EXAMPLE).data
    assert raw == filled


def test_defaults_fill_a_minimal_site():
    site = config.from_dict({
        "schema_version": 1,
        "site": {"display_name": "Minimal"},
        # The two keys with no default (ADR-0025): who reads the trail, and
        # which service groups may hold a spool ancestor, are not guesses.
        "install": {"spool_group": "wbaudit", "trusted_groups": []},
        "slurm": {"partition": "p"},
        "timer": {"on_calendar": "*:00:30"},
    })
    assert site.lookup("install.prefix") == "/usr/local/lib/walk-blocker"
    assert site.lookup("hooks.bash.file") == "/etc/bash.bashrc"
    assert site.lookup("hooks.zsh.package") == "zsh"
    assert site.lookup("filesystems.mounts") == []
    assert [o["label"] for o in site.lookup("reaper.origins")] == [
        "ssh_seated", "ssh_seatless", "container", "systemd_service", "init"]
    assert "bfs" not in site.data["trusted_binaries"]
    assert site.derived()["timer_floor_s"] == 160
    # ADR-0030: the re-assertion cadence has a default; a site that omits
    # it still re-asserts.
    assert site.lookup("timer.reassert_interval_s") == 21600


def test_a_partial_hook_table_is_filled_but_needs_its_file():
    data = load_example_dict()
    data["hooks"]["bash"] = {"enabled": True}
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "hooks.bash.file"
    # Disabled is not exempt: the uninstall path must still know the file.
    data["hooks"]["bash"] = {"enabled": False}
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "hooks.bash.file"
    data["hooks"]["bash"] = {"enabled": False, "file": "/etc/bash.bashrc"}
    assert config.from_dict(data).lookup("hooks.bash.gate") == "required"


def _site_with_hook_files(tmp_path, **files):
    data = load_example_dict()
    for shell, path in files.items():
        data["hooks"][shell]["file"] = path
    site_file = tmp_path / "site.toml"
    site_file.write_text(dump_toml(data), encoding="utf-8")
    return site_file


def test_the_fish_drop_in_may_not_share_the_bash_rc_file(tmp_path, capsys):
    # Issue #224's reproduction: the drop-in is written whole and removed with
    # `rm -f`, so sharing a path would replace, then delete, the bash rc file.
    site_file = _site_with_hook_files(tmp_path, fish="/etc/bash.bashrc")
    with pytest.raises(config.ConfigError) as exc:
        config.load_site(str(site_file))
    assert exc.value.path == "hooks.fish.file"
    assert "hooks.bash.file" in exc.value.message
    assert "/etc/bash.bashrc" in exc.value.message

    assert cli.main(["validate", "--site", str(site_file)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hooks.fish.file" in captured.err
    assert "hooks.bash.file" in captured.err


def test_two_block_hooks_may_not_share_a_file(tmp_path):
    site_file = _site_with_hook_files(tmp_path, zsh="/etc/bash.bashrc")
    with pytest.raises(config.ConfigError) as exc:
        config.load_site(str(site_file))
    assert exc.value.path == "hooks.zsh.file"
    assert "hooks.bash.file" in exc.value.message


def test_a_disabled_hook_may_not_share_a_file_either():
    # `enabled` is one edit away: the next build would install or uninstall
    # over what this one wrote (issue #224's reproduction flipped fish).
    data = load_example_dict()
    data["hooks"]["fish"] = {"enabled": False, "file": "/etc/zsh/zshenv"}
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "hooks.fish.file"
    assert "hooks.zsh.file" in exc.value.message


def test_a_non_canonical_spelling_of_a_shared_file_is_refused_too():
    # The duplicate check compares spellings, which is sound only because
    # every hook file is held to canonical form first. This pins that earlier
    # layer, not the duplicate check: it passes with the duplicate check
    # removed, and fails if an alias spelled with `//` or `.` ever stops being
    # refused before it could slip past by not being byte-equal.
    for alias in ("/etc//bash.bashrc", "/etc/./bash.bashrc"):
        data = load_example_dict()
        data["hooks"]["zsh"]["file"] = alias
        with pytest.raises((config.ConfigError, jsonschema.ValidationError)):
            config.from_dict(data)


def test_distinct_hook_files_pass(tmp_path):
    site_file = _site_with_hook_files(
        tmp_path, bash="/etc/bashrc", zsh="/etc/zshenv",
        fish="/etc/fish/conf.d/walk-blocker.fish")
    site = config.load_site(str(site_file))
    assert sorted(site.data["hooks"][s]["file"] for s in ("bash", "zsh", "fish")) \
        == ["/etc/bashrc", "/etc/fish/conf.d/walk-blocker.fish", "/etc/zshenv"]


def test_policy_matches_the_rule_table_signature():
    search_rules = pytest.importorskip("walk_blocker.search_rules")
    policy = config.load_site(EXAMPLE).policy()
    assert isinstance(policy, search_rules.Policy)
    assert policy.mounts["/home"] == ("expensive", 4)
    assert policy.mounts["/opt/site-tools"] == ("cheap", None)


def test_validate_cli_accepts_the_example(capsys):
    assert cli.main(["validate", "--site", EXAMPLE]) == 0
    out = capsys.readouterr().out
    assert "timer_floor_s = 160" in out
    assert "audit_path = /var/log/walk-blocker/searchguard-audit.jsonl" in out


def test_python_m_walk_blocker_validate(tmp_path):
    r = subprocess.run([sys.executable, "-m", "walk_blocker", "validate", "--site", EXAMPLE],
                       capture_output=True, text=True, cwd=str(tmp_path),
                       env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
    assert r.returncode == 0, r.stderr


def test_validate_reports_a_missing_or_malformed_file(tmp_path, capsys):
    assert cli.main(["validate", "--site", str(tmp_path / "nope.toml")]) == 2
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not toml = at all\n", encoding="utf-8")
    assert cli.main(["validate", "--site", str(bad)]) == 2
    assert "bad.toml" in capsys.readouterr().err


# --- the rejection matrix -----------------------------------------------------

BAD_SITES = [
    ("install.prefix", "usr/local/lib/wb", "pattern"),
    ("install.prefix", "/usr/local/lib/walk blocker", "pattern"),
    ("install.prefix", "/usr/local/lib/walk%blocker", "pattern"),
    ("install.prefix", "/usr/local/lib/$HOME", "pattern"),
    ("install.prefix", "/usr/local/lib/wb;rm", "pattern"),
    ("install.prefix", "/usr/local/lib/`id`", "pattern"),
    ("install.prefix", "/usr/local/lib/'wb'", "pattern"),
    ("install.prefix", "/usr/local/lib/wb\n", "control character"),
    ("reaper.origins[0].pattern", "^init\\.scope$\t", "control character"),
    ("install.prefix", "/usr/local/lib/../lib/wb", "canonical"),
    ("install.prefix", "/usr", "two components"),
    ("filesystems.mount_table", "proc/mounts", "pattern"),
    ("filesystems.mounts[0].maxdepth", 9, "depth_allowance_max"),
    ("filesystems.mounts[1].maxdepth", 2, "expensive"),
    ("filesystems.mounts[1].path", "/home", "duplicate"),
    ("filesystems.mounts[1].inodes_used", 1_300_000, "carries its date"),
    ("filesystems.mounts[1].capacity_bytes", 10_000, "carries its date"),
    ("filesystems.mounts[0].inodes_used", -1, "minimum"),
    ("filesystems.mounts[0].surveyed", "yesterday", "pattern"),
    ("filesystems.mounts[0].surveyed", "2026-1-5", "pattern"),
    ("filesystems.maxdepth_allowed", 9, "depth_allowance_max"),
    ("filesystems.remote_fstypes", ["nfs", "nfs"], "unique"),
    ("filesystems.remote_fstypes", ["Weka FS"], "pattern"),
    ("bogus", {"x": 1}, "Additional properties"),
    ("slurm.bogus", 1, "Additional properties"),
    ("slurm.partition", "data mover", "pattern"),
    ("slurm.default_time", "1h", "pattern"),
    ("slurm.default_mem", "8 GB", "pattern"),
    ("install.tool_search_path", ["/usr/bin", "bin"], "pattern"),
    ("timer.on_calendar", "every ten minutes", "calendar"),
    ("timer.timeout_start_sec", 100, "floor"),
    # ADR-0030: the re-assertion cadence is bounded both ways.
    ("timer.reassert_interval_s", 3599, "minimum"),
    ("timer.reassert_interval_s", 604801, "maximum"),
    ("timer.reassert_interval_s", "21600", "integer"),
    ("timer.reassert_interval_s", 21600.5, "integer"),
    ("reaper.origins[0].pattern", "(", "compile"),
    ("hooks.bash.file", REMOVE, "required (even when"),
    ("hooks.fish.package", "fish", "Additional properties"),
    ("site.display_name", "Example 100% HPC", "pattern"),
    ("site.docs_url", "https://docs.example.org/hpc walk-blocker", "pattern"),
    ("schema_version", 2, "1"),
    # ADR-0025: two keys with no default, both group NAMES.
    ("install.spool_group", REMOVE, "required property"),
    ("install.trusted_groups", REMOVE, "required property"),
    ("install.spool_group", "", "pattern"),
    ("install.spool_group", "123", "pattern"),
    ("install.spool_group", "-g", "pattern"),
    ("install.spool_group", "a b", "pattern"),
    ("install.spool_group", "g" * 33, "pattern"),
    ("install.spool_group", "root", "ADR-0012"),
    ("install.trusted_groups", [""], "pattern"),
    ("install.trusted_groups", ["123"], "pattern"),
    ("install.trusted_groups", ["-g"], "pattern"),
    ("install.trusted_groups", ["a b"], "pattern"),
    ("install.trusted_groups", ["svc-log", "svc-log"], "unique"),
    ("install.trusted_groups", "svc-log", "array"),
]


def _bad_site_toml(dotted, value):
    data = load_example_dict()
    set_dotted(data, dotted, value)
    return dump_toml(data)


@pytest.mark.parametrize("dotted,value,fragment", BAD_SITES,
                         ids=["%s=%r" % (d, v) if v is not REMOVE else "%s removed" % d
                              for d, v, _ in BAD_SITES])
def test_bad_site_is_refused(tmp_path, capsys, dotted, value, fragment):
    site_file = tmp_path / "site.toml"
    site_file.write_text(_bad_site_toml(dotted, value), encoding="utf-8")
    with pytest.raises((config.ConfigError, jsonschema.ValidationError)) as exc:
        config.load_site(str(site_file))
    assert fragment.lower() in str(exc.value).lower(), str(exc.value)

    assert cli.main(["validate", "--site", str(site_file)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(str(site_file) + ": ")
    # The message names where: a dotted key for a semantic fault, a JSON
    # pointer for a shape fault.
    head = dotted.split(".")[0].split("[")[0]
    assert head in captured.err or dotted == "bogus", captured.err


def test_an_empty_trusted_groups_list_is_allowed_and_names_are_kept():
    data = load_example_dict()
    assert data["install"]["trusted_groups"] == []
    assert config.from_dict(data).lookup("install.trusted_groups") == []
    data["install"]["trusted_groups"] = ["svc-log", "_svc.2"]
    data["install"]["spool_group"] = "wb_read-1"
    site = config.from_dict(data)
    assert site.lookup("install.trusted_groups") == ["svc-log", "_svc.2"]
    assert site.lookup("install.spool_group") == "wb_read-1"


def test_the_reaper_action_is_a_closed_set_with_a_report_default():
    """`report` or `kill` and nothing else -- the retired `--kill-others` has
    no value here -- and a site that omits the key ships report-only
    (ADR-0034)."""
    data = load_example_dict()
    data["reaper"]["action"] = "kill_others"
    with pytest.raises(jsonschema.ValidationError) as exc:
        config.from_dict(data)
    assert list(exc.value.absolute_path) == ["reaper", "action"], exc.value
    data = load_example_dict()
    del data["reaper"]["action"]
    assert config.from_dict(data).lookup("reaper.action") == "report"


def test_validate_prints_the_kill_notice_only_for_kill(tmp_path, capsys):
    """One stdout line, not a failure and not stderr: the value is valid,
    and the line is for whoever reads the build log (ADR-0034)."""
    assert cli.main(["validate", "--site", EXAMPLE]) == 0
    assert "reaper.action" not in capsys.readouterr().out
    site = tmp_path / "site.toml"
    site.write_text(open(EXAMPLE).read().replace(
        'action = "report"', 'action = "kill"'), encoding="utf-8")
    assert cli.main(["validate", "--site", str(site)]) == 0
    captured = capsys.readouterr()
    assert 'reaper.action = "kill"' in captured.out and "--kill" in captured.out
    assert captured.err == ""


def test_neither_group_key_has_a_default():
    """A default reader group would be a guess about who reads the trail,
    and a default trusted list would be a guess about which ancestor is
    safe; both are the site's to state (ADR-0025)."""
    props = config.load_schema()["properties"]["install"]["properties"]
    assert "default" not in props["spool_group"]
    assert "default" not in props["trusted_groups"]
    assert set(config.load_schema()["properties"]["install"]["required"]) == {
        "spool_group", "trusted_groups"}


def test_empty_remote_fstypes_is_allowed():
    data = load_example_dict()
    data["filesystems"]["remote_fstypes"] = []
    assert config.from_dict(data).lookup("filesystems.remote_fstypes") == []


def test_config_error_carries_its_path():
    data = load_example_dict()
    data["filesystems"]["mounts"][1]["maxdepth"] = 3
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "filesystems.mounts[1].maxdepth"
    assert str(exc.value).startswith("filesystems.mounts[1].maxdepth: ")


def test_single_component_paths_are_allowed_only_where_listed():
    data = load_example_dict()
    data["install"]["staging_parent"] = "/run"
    data["filesystems"]["mounts"].append({"path": "/scratch", "class": "expensive"})
    config.from_dict(copy.deepcopy(data))
    data["install"]["spool_dir"] = "/var"
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "install.spool_dir"


@pytest.mark.skipif(not os.path.exists("/usr/bin/systemd-analyze")
                    and not os.path.exists("/bin/systemd-analyze"),
                    reason="systemd-analyze not installed")
def test_systemd_analyze_rejects_a_malformed_calendar():
    data = load_example_dict()
    data["timer"]["on_calendar"] = "*:99:00"
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path == "timer.on_calendar"


def test_a_load_error_is_worded_by_its_class():
    """The one wording both `validate` and `build` use: dotted key for a
    semantic fault, JSON pointer for a shape fault, the bare message for an
    unreadable or malformed file -- with no empty field left in between."""
    site = "site.toml"
    assert config.describe_load_error(
        site, config.ConfigError("slurm.qos", "not a name")) == "site.toml: slurm.qos: not a name"
    shape = jsonschema.ValidationError("is not of type 'integer'", path=["reaper", "fanout_n"])
    assert config.describe_load_error(site, shape) == "site.toml: /reaper/fanout_n: is not of type 'integer'"
    assert config.describe_load_error(site, OSError("no such file")) == "site.toml: no such file"
    assert config.describe_load_error(site, ValueError("Invalid statement")) == "site.toml: Invalid statement"


def test_journal_readable_defaults_off_and_tmpfiles_dir_is_admin_owned():
    """ADR-0026: the whole-journal grant is opt-in, and the drop-in's
    directory is one of the two an administrator owns."""
    data = load_example_dict()
    del data["install"]["journal_readable"]
    del data["install"]["tmpfiles_dir"]
    site = config.from_dict(data)
    assert site.lookup("install.journal_readable") is False
    assert site.lookup("install.tmpfiles_dir") == "/etc/tmpfiles.d"
    data = load_example_dict()
    data["install"]["tmpfiles_dir"] = "/usr/lib/tmpfiles.d"
    with pytest.raises((config.ConfigError, jsonschema.ValidationError)):
        config.from_dict(data)


@pytest.mark.parametrize("group", sorted(config.JOURNAL_GROUPS))
def test_journal_readable_refuses_a_group_systemd_already_grants(group):
    """Revoking the drop-in's grant for one of these would strip systemd's
    own. Off, the same group is fine -- except root, refused on its own."""
    data = load_example_dict()
    data["install"]["spool_group"] = group
    data["install"]["journal_readable"] = True
    with pytest.raises(config.ConfigError) as exc:
        config.from_dict(data)
    assert exc.value.path in ("install.journal_readable", "install.spool_group")
    if group != "root":
        data["install"]["journal_readable"] = False
        config.from_dict(data)
