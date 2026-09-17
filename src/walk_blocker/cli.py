"""`walk-blocker`: the workstation-side command.

`validate` and `schema` are Milestone 2; `survey` runs the node's own
`survey.py` in place so the same code an administrator runs on the node can
be tried against a saved mount table here. `build` compiles a site into the
node payload, or with `--check` compares a payload against what it would
build (ADR-0013).
"""
import argparse
import importlib.util
import json
import os
import sys

from . import __version__, build, config, paths


def _load_survey_module():
    """`node/survey.py` is stdlib-only and not a package (ADR-0015). Load it
    by file so nothing named `survey` lands on `sys.path`."""
    location = os.path.join(paths.node_dir(), "survey.py")
    spec = importlib.util.spec_from_file_location("walk_blocker_node_survey", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_site_or_exit(path):
    try:
        return config.load_site(path)
    except config.LOAD_ERRORS as exc:
        # sys.stderr looked up per call, so a redirected stream is honoured.
        sys.stderr.write(config.describe_load_error(path, exc) + "\n")
    return None


def cmd_validate(args):
    site = _load_site_or_exit(args.site)
    if site is None:
        return 2
    print("%s: valid (%s)" % (args.site, site.lookup("site.display_name")))
    for key, value in sorted(site.derived().items()):
        print("%s = %s" % (key, value))
    return 0


def cmd_schema(args):
    print(config.schema_path())
    return 0


def cmd_survey(args):
    survey = _load_survey_module()
    site = None
    remote_fstypes, remote_proxy = None, True
    if args.site:
        site = _load_site_or_exit(args.site)
        if site is None:
            return 2
        remote_fstypes = site.lookup("filesystems.remote_fstypes")
        remote_proxy = site.lookup("filesystems.remote_proxy")
    rows = survey.survey(args.mounts, timeout=args.timeout, include_all=args.all,
                         remote_fstypes=remote_fstypes, remote_proxy=remote_proxy)
    if site is not None:
        overrides = {m["path"]: m for m in site.lookup("filesystems.mounts")}
        for row in rows:
            entry = overrides.get(row["mountpoint"])
            row["override"] = None if entry is None else entry["class"]
            row["override_maxdepth"] = None if entry is None else entry.get("maxdepth")
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    print(survey.render_table(rows))
    print()
    print(survey.render_toml(rows), end="")
    return 0


def cmd_build(args):
    return build.build(args.site, args.out, check=args.check)


# The maze on the README, printed by --help and --version.
BANNER = """\
  ╶─┬─────────┬─────┬─────┬───┬─────────────┬───────┐
  🯇╮│  ╭┈┈┈┈┈╮│  ╭┈╮│     │   │        ╭┈┈┈╮│       │
  ╷┊│ ╷┊┌───╴┊│ ╷┊╷┊│ ╷ ╶─┘ ╷ ╵ ┌─┬───┐┊┌─┐┊└───┐ ╷ │
  │┊│ │┊│╭┈┈┈╯│ │┊│┊│ │     │   │ │╭┈╮│┊│ │╰┈┈┈╮│ │ │
  │┊└─┤┊│┊╶─┬─┘ │┊│┊│ └─────┴───┘ │┊╷┊╵┊│ └───╴┊│ │ │
  │╰┈╮│┊│╰┈╮│   │┊│┊│             │┊│╰┈╯│╭┈┈┈┈┈╯│ │ │
  ├─╴┊│┊└─┐┊└─┬─┘┊│┊├─────────────┤┊├───┤┊╶─┬───┴─┘ │
  │╭┈╯│╰┈╮│╰┈╮│╭┈╯│┊│FluidNumerics│┊│   │╰┈╮│       │
  │┊╶─┤ ╷┊└─┐┊╵┊┌─┘┊│             │┊│ ╶─┼─╴┊│ ╷ ┌─╴ │
  │╰┈╮│ │╰┈╮│╰┈╯│╭┈╯│ WALKBLOCKER │┊│   │╭┈╯│ │ │   │
  ├─╴┊├─┴─╴┊├───┤┊╶─┤             │┊│ ╷ │┊╶─┤ │ └─┐ │
  │╭┈╯│╭┈┈┈╯│   │╰┈╮│ stops slow  │┊│ │ │╰┈╮│ │   │ │
  │┊╶─┤┊╶───┤ ╶─┴─╴┊│ filesystem  │┊└─┤ └─┐┊├─┴─╴ │ │
  │╰┈╮│╰┈┈┈╮│╭┈┈┈┈┈╯│ traversals  │╰┈╮│   │┊│     │ │
  ├─┐┊└───╴┊│┊╶─┬───┴───┬─────────┘ ╷┊│ ╷ ╵┊│ ╶───┴─┤
  │ │╰┈┈┈┈┈╯│╰┈╮│╭┈┈┈┈┈╮│           │┊│ │╭┈╯│       │
  │ └───┬───┼─╴┊│┊┌───┐┊└───┬───────┤┊└─┤┊┌─┘ ╶───┐ │
  │     │   │╭┈╯│┊│   │╰┈┈┈╮│╭┈┈┈┈┈╮│╰┈╮│┊│       │ │
  │ ╷ ╶─┘ ╷ ╵┊╶─┘┊│ ╶─┴───╴┊╵┊┌───╴┊└─╴┊│┊└───────┘ ╵
  │ │     │  ╰┈┈┈╯│        ╰┈╯│    ╰┈┈┈╯│╰┈┈┈┈┈┈┈┈┈┈🯈
  └─┴─────┴───────┴───────────┴─────────┴───────────╴
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="walk-blocker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=BANNER + "\n\nCompile and check a site's walk-blocker configuration.")
    parser.add_argument("--version", action="version",
                        version="%s\nwalk-blocker %s" % (BANNER, __version__))
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("validate", help="schema and semantic checks on a site.toml")
    p.add_argument("--site", required=True, metavar="FILE")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("schema", help="print the path of the site schema")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("survey", help="classify and measure the mounts in a mount table")
    p.add_argument("--mounts", default="/proc/mounts", metavar="FILE")
    p.add_argument("--timeout", type=float, default=2.0, metavar="SECONDS",
                   help="per-mount statfs budget (default 2.0)")
    p.add_argument("--site", metavar="FILE",
                   help="mark each mount with the override that covers it")
    p.add_argument("--all", action="store_true", help="include pseudo filesystems")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("build", help="compile a site.toml into the node payload")
    p.add_argument("--site", required=True, metavar="FILE")
    p.add_argument("--out", required=True, metavar="DIR",
                   help="payload directory; rebuilt if it carries the build marker")
    p.add_argument("--check", action="store_true",
                   help="compare DIR against a fresh render and write nothing; "
                        "exit 1 if they differ")
    p.set_defaults(func=cmd_build)
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
