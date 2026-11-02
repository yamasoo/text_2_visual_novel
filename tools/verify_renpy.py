"""薄外殼：python tools/verify_renpy.py --sdk <SDK> --project <專案>（邏輯在 t2vn/verify_renpy.py）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t2vn.verify_renpy import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
