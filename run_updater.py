"""PyInstaller entry point for the small, standalone Windows update helper."""
from pathlib import Path
import sys

from proficentr.updater import apply

if __name__ == '__main__':
    raise SystemExit(apply(Path(sys.argv[1])))
