"""Visible dashboard launcher with errors written to a local log."""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
log_path = ROOT / "data" / "dashboard.log"

with log_path.open("a", encoding="utf-8", buffering=1) as log:
    sys.stderr = log
    try:
        from auto_reply.ui import main
        main()
    except BaseException:
        traceback.print_exc(file=log)
        raise
