"""Command-line entry point: `python -m engine run packs/webapps`."""

import sys

from engine.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
