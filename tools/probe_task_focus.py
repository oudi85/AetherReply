"""One-shot focus check from the scheduled task's desktop; sends nothing."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auto_reply.wx.uia_send import _activate, _open_exact_chat, _window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contact", help="要检查的联系人名称")
    contact = parser.parse_args().contact
    out = ROOT / "data" / "focus-probe.log"
    try:
        _, hwnd = _window()
        win = _activate(hwnd)
        box = _open_exact_chat(win, hwnd, contact)
        result = "foreground_and_target_ok" if box.Name == contact else "target_mismatch"
    except Exception as exc:
        result = f"foreground_error {type(exc).__name__}: {exc}"
    out.write_text(result + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
