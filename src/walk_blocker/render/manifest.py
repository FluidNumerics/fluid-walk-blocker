"""`site.lock.json`: the record of what a payload was built from (ADR-0013).

The payload carries its own configuration as a record, not as an input: the
node never reads it, but an operator can ask which configuration is deployed
and check the answer against a commit. The manifest holds the schema version,
the version of the tool that built it, the payload `VERSION`, the SHA-256 of
the `site.toml` bytes and a SHA-256 per emitted file.

No timestamp and no hostname, keys sorted, one trailing newline: two builds
of the same inputs must produce byte-identical manifests, because `--check`
is a byte comparison and CI runs it.
"""
import hashlib
import json
import os

FILENAME = "site.lock.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def render_manifest(site_bytes, version, schema_version, tool_version, files):
    """The manifest as a dict. `files` maps payload-relative path to the
    bytes at that path; the manifest itself is never among them."""
    if FILENAME in files:
        raise ValueError("%s cannot list itself" % FILENAME)
    return {
        "schema_version": schema_version,
        "walk_blocker_version": tool_version,
        "version": version,
        "site_sha256": sha256(site_bytes),
        "files": {rel: sha256(data) for rel, data in files.items()},
    }


def manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_manifest(out_dir, site_path, version, schema_version, tool_version, files):
    """Render and write `<out_dir>/site.lock.json`; return the dict."""
    with open(site_path, "rb") as fh:
        site_bytes = fh.read()
    manifest = render_manifest(site_bytes, version, schema_version, tool_version, files)
    target = os.path.join(out_dir, FILENAME)
    with open(target, "wb") as fh:
        fh.write(manifest_bytes(manifest))
    os.chmod(target, 0o644)
    return manifest


def read_manifest(path):
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))
