import sys

from .cli import main as _main


def main() -> None:
    sys.exit(_main())
