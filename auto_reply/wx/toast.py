"""Windows 通知中心监听：微信 PC 的消息 toast 会写入系统通知库
%LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db（明文 SQLite）。

这是不碰微信、不用 OCR 的实时来讯通道；前提是微信 设置→消息通知 里
允许了 Windows 通知（且微信窗口不在前台时才会弹）。
"""

import os
import re
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

WPNDATABASE = Path(os.environ.get("LOCALAPPDATA", "")) / \
    "Microsoft/Windows/Notifications/wpndatabase.db"

# 微信 PC 的 AUMID 可能形如 'xwechat_...' / 'Weixin_...' / '...Weixin!App' / 旧版 'WeChat'
_WECHAT_AUMID = re.compile(r"eixin|echat|encent", re.I)

_TOAST_TEXT = re.compile(r"<text[^>]*>(.*?)</text>", re.S)
_XML_ESC = [("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
            ("&quot;", '"'), ("&apos;", "'")]


def _unescape(s: str) -> str:
    for a, b in _XML_ESC:
        s = s.replace(a, b)
    return s


def _open_snapshot() -> sqlite3.Connection | None:
    """把 wpndatabase.db(+wal) 拷到临时目录再打开，避免与系统服务争锁。"""
    if not WPNDATABASE.exists():
        return None
    tmp = Path(tempfile.gettempdir()) / f"wpn_snap_{os.getpid()}"
    tmp.mkdir(exist_ok=True)
    dst = tmp / "wpndatabase.db"
    shutil.copy2(WPNDATABASE, dst)
    wal = WPNDATABASE.with_name("wpndatabase.db-wal")
    if wal.exists():
        shutil.copy2(wal, dst.with_suffix(".db-wal"))
    return sqlite3.connect(f"file:{dst}?mode=ro", uri=True)


def wechat_toasts(since_arrival: int = 0, limit: int = 50) -> list[dict]:
    """返回微信 toast 列表 [{arrival, title, body}]，按时间升序。

    since_arrival: Windows FILETIME 水位，只返回比它新的。
    """
    con = _open_snapshot()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT n.Payload, n.ArrivalTime FROM Notification n "
            "JOIN NotificationHandler h ON n.HandlerId = h.RecordId "
            "WHERE h.PrimaryId LIKE '%eixin%' OR h.PrimaryId LIKE '%echat%' "
            "OR h.PrimaryId LIKE '%encent%' "
            "ORDER BY n.ArrivalTime DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out = []
    for payload, arrival in rows:
        if arrival <= since_arrival:
            continue
        if not isinstance(payload, bytes):
            continue
        xml = payload.decode("utf-8", errors="ignore")
        if "<toast" not in xml and "<text" not in xml:
            continue  # tile/badge 等非消息通知
        texts = [_unescape(t).strip() for t in _TOAST_TEXT.findall(xml)]
        texts = [t for t in texts if t]
        if not texts:
            continue
        title = texts[0]
        body = texts[1] if len(texts) > 1 else ""
        out.append({"arrival": arrival, "title": title, "body": body,
                    "extra": texts[2:]})
    out.sort(key=lambda x: x["arrival"])
    return out


def latest_watermark() -> int:
    con = _open_snapshot()
    if con is None:
        return 0
    try:
        row = con.execute("SELECT MAX(ArrivalTime) FROM Notification").fetchone()
        return row[0] or 0
    except sqlite3.Error:
        return 0
    finally:
        con.close()
