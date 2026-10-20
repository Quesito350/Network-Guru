"""Start the dashboard from anywhere: `python run.py dev` (same commands as python -m networkguru)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from networkguru.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
