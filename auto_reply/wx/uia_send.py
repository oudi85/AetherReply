"""UIA-only sender with runtime-verified Weixin.dll accessibility gate.

No OCR path. A send attempt is never repeated if Enter may have reached WeChat.
"""

import sqlite3
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

import psutil
import uiautomation as auto

from tools import probe_gate, probe_send_ui as ui

from ..core.contacts import SendError, display_name  # noqa: F401  重新导出


def _window():
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() != "weixin.exe":
            continue
        hwnd = probe_gate.main_window(proc.pid)
        if hwnd is not None:
            return proc.pid, hwnd
    raise SendError("未找到交互桌面上的微信主窗口")


def _verified_module_base(pid: int) -> tuple[Path, int]:
    try:
        path, base = probe_gate.module_location(psutil.Process(pid))
    except (RuntimeError, psutil.Error) as exc:
        raise SendError(f"微信 DLL 路径未通过核对：{exc}") from exc
    if (probe_gate.process_byte(pid, base) != ord("M")
            or probe_gate.process_byte(pid, base + 1) != ord("Z")):
        raise SendError("微信 DLL 内存基址未通过核对")
    return path, base


def _ensure_accessibility(pid: int, hwnd: int) -> None:
    if probe_gate.materialized(hwnd):
        return
    path, base = _verified_module_base(pid)
    try:
        address = base + probe_gate.discover_gate(path.read_bytes())
    except (OSError, RuntimeError) as exc:
        raise SendError(f"辅助功能 gate 特征未通过核对：{exc}") from exc
    old = probe_gate.process_byte(pid, address)
    if old not in (0, 1):
        raise SendError("辅助功能 gate 当前状态异常")
    if old == 0:
        probe_gate.write_process_byte(pid, address, 1)
    for _ in range(10):
        if probe_gate.materialized(hwnd):
            return
        time.sleep(0.2)
    if old == 0:
        probe_gate.write_process_byte(pid, address, 0)
    raise SendError("辅助功能控件树未出现")


def _activate(hwnd: int):
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user.AttachThreadInput.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.BringWindowToTop.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    win = auto.ControlFromHandle(hwnd)
    if user.GetForegroundWindow() == hwnd:
        return win
    user.ShowWindow(hwnd, 9)  # SW_RESTORE
    user.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    if user.GetForegroundWindow() != hwnd:
        current_thread = kernel.GetCurrentThreadId()
        foreground = user.GetForegroundWindow()
        threads = [user.GetWindowThreadProcessId(h, None)
                   for h in (foreground, hwnd) if h]
        attached = []
        try:
            for thread in set(threads):
                if thread and thread != current_thread and user.AttachThreadInput(
                        current_thread, thread, True):
                    attached.append(thread)
            user.BringWindowToTop(hwnd)
            user.SetForegroundWindow(hwnd)
            time.sleep(0.2)
        finally:
            for thread in reversed(attached):
                user.AttachThreadInput(current_thread, thread, False)
    if user.GetForegroundWindow() != hwnd:
        raise SendError("微信主窗口未能置前")
    return win


def _open_exact_chat(win, hwnd: int, label: str):
    search = ui.find(win, lambda c: c.ClassName == "mmui::XValidatorTextEdit"
                     and c.ControlTypeName == "EditControl" and c.Name == "搜索")
    if search is None:
        raise SendError("未找到 UIA 搜索框")
    pattern = search.GetValuePattern()
    if pattern is None or pattern.IsReadOnly:
        raise SendError("UIA 搜索框不可写")
    try:
        search.SetFocus()
        pattern.SetValue(label)
        if pattern.Value != label:
            raise SendError("搜索关键词读回不一致")
        end = time.monotonic() + 5
        candidates = []
        while time.monotonic() < end:
            lst = ui.find(win, lambda c: ui.aid_hit(c, "search_list"),
                          max_nodes=1500)
            if lst is not None:
                candidates = [c for c in lst.GetChildren()
                              if (c.AutomationId or "").startswith("search_item_")
                              and (c.Name or "").split("\n", 1)[0].strip() == label]
            if candidates:
                break
            time.sleep(0.2)
        unique = {c.AutomationId: c for c in candidates}
        if len(unique) != 1:
            raise SendError("微信搜索结果不是唯一的完全匹配项")
        item = next(iter(unique.values()))
        result_rect = item.BoundingRectangle
        ui.click_rect(result_rect, hwnd)
        input_box = ui.wait_chat_input(win, label, timeout=4.0)
        if input_box is None or input_box.Name != label:
            raise SendError("打开后的会话名称不匹配")
        return input_box
    finally:
        try:
            pattern.SetValue("")
        except Exception:
            pass


def _press_enter_once(input_box, label: str, text: str) -> None:
    if not text or len(text) > 1000 or "\n" in text or "\r" in text:
        raise SendError("回复文字为空、过长或包含换行")
    vp = input_box.GetValuePattern()
    if vp is None or vp.IsReadOnly or vp.Value:
        raise SendError("输入框不可读写或已有草稿")
    vp.SetValue(text)
    time.sleep(0.3)
    if vp.Value != text:
        vp.SetValue("")
        raise SendError("输入内容读回不一致")
    input_box.SetFocus()
    try:
        focus = auto.GetFocusedControl()
        focused = ui.aid_hit(focus, "chat_input_field") and focus.Name == label
    except Exception:
        focused = False
    if not focused or input_box.Name != label or vp.Value != text:
        vp.SetValue("")
        raise SendError("发送前焦点、目标或文字核对失败")
    try:
        input_box.SendKeys("{Enter}", waitTime=0.05)
    except Exception:
        # The key may have reached WeChat before the UIA call raised.
        # The caller must confirm in the DB or leave the attempt uncertain.
        pass


class SendSession:
    """Keep one verified chat open for consecutive parts of the same reply."""

    def __init__(self, con, talker: str):
        self.talker = talker
        self.label = display_name(con, talker)
        self.pid = None
        self.hwnd = None

    def chat(self):
        pid, hwnd = _window()
        if self.hwnd is not None and (pid, hwnd) != (self.pid, self.hwnd):
            raise SendError("微信窗口已变化，连发已停止")
        _ensure_accessibility(pid, hwnd)
        win = _activate(hwnd)
        if self.hwnd is None:
            box = _open_exact_chat(win, hwnd, self.label)
            self.pid, self.hwnd = pid, hwnd
        else:
            # If the user changes conversations, stop instead of reopening it.
            box = ui.find(win, lambda c: ui.aid_hit(c, "chat_input_field"),
                          max_nodes=1200)
            if box is None or box.Name != self.label:
                raise SendError("当前会话已变化，连发已停止")
        return pid, hwnd, win, box

    def send_text(self, cfg: dict, con, text: str) -> str:
        baseline = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        _, _, _, input_box = self.chat()
        _press_enter_once(input_box, self.label, text)
        return _confirm_text(cfg, self.talker, text, baseline, input_box, self.label)


def send_once(cfg: dict, con, talker: str, text: str) -> str:
    """Return confirmed / uncertain; preflight errors raise SendError.

    A false negative after Enter is 'uncertain', never a retryable failure.
    """
    return SendSession(con, talker).send_text(cfg, con, text)


def _confirm_text(cfg, talker, text, baseline, input_box, label):
    mirror = Path(cfg["sync"]["data_dir"])
    if not mirror.is_absolute():
        from auto_reply.config import PROJECT_ROOT
        mirror = PROJECT_ROOT / mirror
    mirror /= "auto_reply.db"
    from auto_reply.wx import sync
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(.35)
        try:
            sync.sync_once(cfg, verbose=False)
            with sqlite3.connect(f"file:{mirror.as_posix()}?mode=ro", uri=True) as check:
                hit = check.execute(
                    "SELECT 1 FROM messages WHERE id>? AND talker=? "
                    "AND is_sender=1 AND content=? LIMIT 1",
                    (baseline, talker, text)).fetchone()
            if hit:
                return "confirmed"
        except Exception:
            pass
    try:
        vp = input_box.GetValuePattern()
        if input_box.Name == label and vp is not None and vp.Value == text:
            vp.SetValue("")
    except Exception:
        pass
    return "uncertain"
