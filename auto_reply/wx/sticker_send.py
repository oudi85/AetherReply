"""UIA-only favorite-sticker sender with per-DLL self-chat calibration."""

import ctypes
import hashlib
import json
import re
import sqlite3
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto

from .. import config as cfgmod
from ..core.contacts import SendError, display_name
from ..core import stickers
from . import sync, uia_send
from tools import probe_send_ui as ui


def _dll_hash(pid: int) -> str:
    path, _ = uia_send._verified_module_base(pid)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _favorite_stamp(cfg: dict) -> list[int]:
    from . import decrypt, paths
    account = paths.find_account(paths.find_data_dir(cfg))
    db = account / "db_storage" / "emoticon" / "emoticon.db"
    return [db.stat().st_mtime_ns, db.stat().st_size, *decrypt.wal_revision(db)]


def _popover(pid: int, hwnd: int, win):
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

    def find_popups():
        handles = []
        def collect(handle, _):
            owner = wintypes.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid and handle != hwnd and user.IsWindowVisible(handle):
                handles.append(handle)
            return True
        user.EnumWindows(callback_type(collect), 0)
        result = []
        for handle in handles:
            try:
                root = auto.ControlFromHandle(handle)
                if root.ClassName == "mmui::EmoticonPopover":
                    result.append((handle, root))
            except Exception:
                continue
        return result

    popups = find_popups()
    if not popups:
        button = ui.find(win, lambda c: c.ControlTypeName == "ButtonControl"
                         and c.Name == "发送表情(Alt+E)", max_nodes=1200)
        if button is None:
            raise SendError("未找到微信表情按钮")
        ui.click_rect(button.BoundingRectangle, hwnd)
        time.sleep(.25)
        popups = find_popups()
    if len(popups) != 1:
        raise SendError("微信表情面板不唯一或不可见")
    popup_hwnd, root = popups[0]
    tab = ui.find(root, lambda c: c.ControlTypeName == "TabItemControl"
                  and c.Name == "自定义表情", max_nodes=1200)
    if tab is None:
        raise SendError("未找到自定义表情页")
    ui.click_rect(tab.BoundingRectangle, popup_hwnd)
    time.sleep(.25)
    pending, items = [root], []
    while pending and len(items) < 200:
        item = pending.pop()
        try:
            if item.ClassName == "mmui::FavEmoticonItemView":
                items.append(item)
            pending.extend(item.GetChildren())
        except Exception:
            continue
    items.sort(key=lambda item: (item.BoundingRectangle.top,
                                 item.BoundingRectangle.left))
    if len(items) < 2:
        raise SendError("收藏表情网格未出现")
    return popup_hwnd, items


def _sent_md5(cfg: dict, row) -> str:
    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", row["src_table"]):
        return ""
    decrypted = cfgmod.data_dir(cfg) / "decrypted" / row["src_db"].replace(".db", ".sqlite")
    with sqlite3.connect(decrypted) as source:
        raw = source.execute(f'SELECT message_content FROM "{row["src_table"]}" '
                             "WHERE local_id=?", (row["src_rowid"],)).fetchone()
    if raw is None:
        return ""
    match = re.search(r'md5=["\']([0-9a-fA-F]{32})', sync._decode_text(raw[0]))
    return match.group(1).lower() if match else ""


def _click_and_confirm(cfg, con, talker: str, label: str, index: int,
                       expected: str, expected_count: int | None = None,
                       session: uia_send.SendSession | None = None) -> tuple[str, int]:
    baseline = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    if session is None:
        pid, hwnd = uia_send._window()
        uia_send._ensure_accessibility(pid, hwnd)
        win = uia_send._activate(hwnd)
        box = uia_send._open_exact_chat(win, hwnd, label)
    else:
        pid, hwnd, win, box = session.chat()
    if box.Name != label:
        raise SendError("表情发送目标核对失败")
    vp = box.GetValuePattern()
    if vp is None or vp.Value:
        raise SendError("表情发送前输入框已有草稿或不可读取")
    popup_hwnd, items = _popover(pid, hwnd, win)
    if (expected_count is not None and len(items) != expected_count) or index >= len(items):
        raise SendError("收藏表情网格大小与校准记录不一致")
    ui.click_rect(items[index].BoundingRectangle, popup_hwnd)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(.35)
        try:
            sync.sync_once(cfg, verbose=False)
            row = con.execute("SELECT src_db,src_table,src_rowid,msg_type FROM messages "
                              "WHERE id>? AND talker=? AND is_sender=1 "
                              "ORDER BY id DESC LIMIT 1", (baseline, talker)).fetchone()
            if row:
                if row["msg_type"] != 47:
                    return "uncertain", len(items)
                return ("confirmed" if _sent_md5(cfg, row) == expected else "uncertain",
                        len(items))
        except Exception:
            pass
    return "uncertain", len(items)


def calibrate(cfg: dict, con) -> dict:
    """Send exactly two favorites to File Transfer Assistant; never auto retry."""
    favorites = [row[0] for row in con.execute(
        "SELECT md5 FROM stickers ORDER BY favorite_order DESC")]
    if len(favorites) < 2:
        raise ValueError("请先扫描至少两个收藏表情")
    pid, _ = uia_send._window()
    dll_sha256 = _dll_hash(pid)
    visible = 0
    for index in (0, 1):
        status, count = _click_and_confirm(cfg, con, "filehelper", "文件传输助手",
                                           index, favorites[index], visible or None)
        if status != "confirmed":
            raise RuntimeError("表情校准未确认；不会重试或开启自动发送")
        visible = count
    stickers.scan(cfg, con)
    current = [item[0] for item in con.execute(
        "SELECT md5 FROM stickers WHERE favorite_order>=0 "
        "ORDER BY favorite_order DESC LIMIT ?", (visible,))]
    selected = favorites[:visible]
    if current != selected:
        raise RuntimeError("校准期间收藏顺序发生变化")
    con.execute("INSERT INTO sticker_calibration(id,dll_sha256,favorites_json,"
                "visible_count,calibrated_at) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET dll_sha256=excluded.dll_sha256,"
                "favorites_json=excluded.favorites_json,"
                "visible_count=excluded.visible_count,calibrated_at=excluded.calibrated_at",
                (dll_sha256, json.dumps({"items": selected,
                                         "source_stamp": _favorite_stamp(cfg)}),
                 visible, int(time.time())))
    con.commit()
    return {"visible": visible, "mapped": len(selected)}


def calibrated_favorites(cfg: dict, con) -> list[str]:
    row = con.execute("SELECT favorites_json,visible_count FROM sticker_calibration "
                      "WHERE id=1").fetchone()
    if row is None:
        return []
    record = json.loads(row[0])
    if not isinstance(record, dict):
        return []
    expected = record.get("items", [])
    if record.get("source_stamp") != _favorite_stamp(cfg):
        # WAL revisions can change without changing the favorites. Re-read the
        # source before trusting the saved position-to-MD5 mapping.
        stickers.scan(cfg, con)
    actual = [item[0] for item in con.execute(
        "SELECT md5 FROM stickers WHERE favorite_order>=0 "
        "ORDER BY favorite_order DESC LIMIT ?", (row[1],))]
    if actual != expected:
        return []
    if record.get("source_stamp") != _favorite_stamp(cfg):
        con.execute("UPDATE sticker_calibration SET favorites_json=? WHERE id=1",
                    (json.dumps({"items": expected,
                                 "source_stamp": _favorite_stamp(cfg)}),))
        con.commit()
    return expected


def send_once(cfg: dict, con, talker: str, md5: str,
              session: uia_send.SendSession | None = None) -> str:
    if not cfg.get("reply_plan", {}).get("allow_stickers") or not con.execute(
            "SELECT 1 FROM sticker_assignments WHERE md5=? LIMIT 1", (md5,)).fetchone():
        raise SendError("表情自动发送已关闭或人工标注已撤销")
    label = display_name(con, talker)
    row = con.execute("SELECT dll_sha256,visible_count FROM sticker_calibration "
                      "WHERE id=1").fetchone()
    favorites = calibrated_favorites(cfg, con)
    if row is None or md5 not in favorites:
        raise SendError("收藏表情未校准或收藏顺序已变化")
    pid, _ = uia_send._window()
    if _dll_hash(pid) != row[0]:
        raise SendError("微信版本与表情校准时不同")
    status = _click_and_confirm(cfg, con, talker, label,
                                favorites.index(md5), md5, row[1], session=session)[0]
    if status == "confirmed":
        try:
            previous = json.loads(con.execute(
                "SELECT favorites_json FROM sticker_calibration WHERE id=1").fetchone()[0])
            if _favorite_stamp(cfg) != previous["source_stamp"]:
                stickers.scan(cfg, con)
                current = [item[0] for item in con.execute(
                    "SELECT md5 FROM stickers WHERE favorite_order>=0 "
                    "ORDER BY favorite_order DESC LIMIT ?", (row[1],))]
                if current == favorites:
                    con.execute("UPDATE sticker_calibration SET favorites_json=? WHERE id=1",
                                (json.dumps({"items": favorites,
                                             "source_stamp": _favorite_stamp(cfg)}),))
                    con.commit()
        except Exception:
            pass  # The send was confirmed; a stale calibration blocks later sends.
    return status
