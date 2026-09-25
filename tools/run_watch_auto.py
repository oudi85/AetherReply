"""Run the allowlisted auto-reply loop without a visible console window."""

import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    log_path = ROOT / "data" / "watch-auto.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 自动回复进程启动", flush=True)
        try:
            from auto_reply.cli import cmd_watch_auto
            from auto_reply.config import load_config
            from auto_reply.wx.uia_send import _window
            from tools import probe_gate

            try:
                _, hwnd = _window()
                print(f"[i] 交互桌面微信窗口可访问：{probe_gate.materialized(hwnd)}")
            except Exception as exc:
                print(f"[i] 微信窗口待就绪：{type(exc).__name__}")

            cmd_watch_auto(load_config())
        except BaseException:
            traceback.print_exc()
            raise


if __name__ == "__main__":
    main()
