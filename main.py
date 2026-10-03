import sys
from pathlib import Path

# Ensure root package is in path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from dion.cli import app

if __name__ == "__main__":
    app()
