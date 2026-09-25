"""控制台访问本机环境的唯一入口：镜像库、config.toml、计划任务、运行日志。

界面只调用下列方法，演示模式（demo.py）实现同一套接口：

  demo                    是否演示模式
  capabilities            后台已支持的能力；为 False 时界面对应控件置灰，不会调用：
    draft_mode              “仅生成”模式（生成候选但不发送）
    thinking_toggle         深度思考总开关（llm.thinking）
  config()                读取配置
  connect()               只读为主的 sqlite 连接（row_factory=sqlite3.Row）
  allowlist() / set_allowlist(list)
  set_effort("low"|"high"|"max")
  set_thinking(bool)      需 capabilities["thinking_toggle"]
  current_mode(con)       "auto" | "draft" | "paused"
  set_mode(mode)          "draft" 需 capabilities["draft_mode"]
  worker_running() / start_worker() -> 错误文本或 None
  read_log(n) / open_log_folder() / close()

后台在 mark_sending 之前判断模式；仅生成时把候选与 drafted 状态写入本地镜像。
"""

import os
import sqlite3
from contextlib import closing

import psutil

from ..config import PROJECT_ROOT, load_config
from ..core import control, preferences
from ..winproc import run_hidden

DB = PROJECT_ROOT / "data" / "auto_reply.db"
LOG = PROJECT_ROOT / "data" / "watch-auto.log"
TASK_NAME = "WeChatAutoReply"
MODES = ("auto", "draft", "paused")


class LiveBackend:
    demo = False
    db_path = DB
    capabilities = {"draft_mode": True, "thinking_toggle": True}

    def config(self) -> dict:
        return load_config()

    def connect(self):
        con = sqlite3.connect(DB, timeout=2)
        con.row_factory = sqlite3.Row
        return con

    def allowlist(self) -> list[str]:
        return preferences.current_allowlist()

    def set_allowlist(self, talkers: list[str]) -> None:
        preferences.set_allowlist(talkers)

    def set_effort(self, value: str) -> None:
        preferences.set_reasoning_effort(value)

    def set_thinking(self, enabled: bool) -> None:
        preferences.set_thinking(enabled)

    def current_mode(self, con) -> str:
        return control.current_mode(con)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式：{mode}")
        with closing(self.connect()) as con:
            control.set_mode(con, mode)

    def worker_running(self) -> bool:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if (proc.info.get("name") or "").lower() == "pythonw.exe" and any(
                        "run_watch_auto.py" in arg for arg in (proc.info.get("cmdline") or [])):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def start_worker(self) -> str | None:
        result = run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             f"Start-ScheduledTask -TaskName '{TASK_NAME}'"], timeout=30)
        return (result.stderr or result.stdout).strip()[:300] or "未知错误" \
            if result.returncode else None

    def read_log(self, max_lines: int = 400) -> list[str]:
        try:
            with LOG.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 128 * 1024))
                data = fh.read()
        except FileNotFoundError:
            return ["还没有运行记录。"]
        return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]

    def open_log_folder(self) -> None:
        os.startfile(LOG.parent)

    def close(self) -> None:
        pass
