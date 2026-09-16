"""`python -m walk_blocker`. Only `--version` exists until Milestone 2."""
import argparse
import sys

from . import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(prog="walk-blocker")
    parser.add_argument("--version", action="version",
                        version="walk-blocker %s" % __version__)
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
