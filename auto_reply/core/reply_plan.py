"""Validated multi-part reply plans and crash-safe per-part ledger."""

import time

from . import control


def normalize_candidates(cfg: dict, candidates: list,
                         allowed_stickers: dict[str, str] | None = None,
                         allowed_categories: set[str] | None = None) -> list[dict]:
    opts = cfg.get("reply_plan", {})
    enabled = bool(opts.get("enabled", False))
    max_parts = max(1, min(3, int(opts.get("max_parts", 3)))) if enabled else 1
    allowed_stickers = allowed_stickers or {}
    allowed_categories = allowed_categories or set()
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("模型没有返回候选回复")
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("候选回复格式无效")
        parts = candidate.get("parts")
        if not parts:
            parts = [{"type": "text", "text": candidate.get("text", "")}]
        if not isinstance(parts, list) or not 1 <= len(parts) <= max_parts:
            raise ValueError("回复条数超过当前设置")
        clean = []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("回复步骤格式无效")
            if part.get("type") == "sticker":
                md5 = str(part.get("md5", "")).lower()
                if md5 not in allowed_stickers or not opts.get("allow_stickers"):
                    raise ValueError("模型选了未标记或未校准的收藏表情")
                clean.append({"type": "sticker", "md5": md5})
                continue
            if part.get("type") == "sticker_category":
                category = part.get("category")
                if not isinstance(category, str) or category not in allowed_categories:
                    raise ValueError("模型选了未开放的表情分类")
                clean.append({"type": "sticker_category", "category": category})
                continue
            if part.get("type") != "text":
                raise ValueError("未知的回复步骤类型")
            value = part.get("text")
            if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
                raise ValueError("回复步骤必须是单行文字")
            value = value.strip()
            if len(value) > 60:
                raise ValueError("单条回复超过 60 字")
            clean.append({"type": "text", "text": value})
        if sum(p["type"] in ("sticker", "sticker_category") for p in clean) > 1:
            raise ValueError("一次回复最多使用一张收藏表情")
        if any(p["type"] == "sticker_category" for p in clean) and not any(
                p["type"] == "text" for p in clean):
            raise ValueError("分类选择必须搭配至少一条文字回复")
        display = [p["text"] if p["type"] == "text" else
                   f"[表情：{allowed_stickers[p['md5']]}]" if p["type"] == "sticker"
                   else f"[待选表情：{p['category']}]" for p in clean]
        result.append({**candidate, "text": " / ".join(display),
                       "parts": clean})
    return result


def prepare(con, message_id: int, parts: list[dict]) -> None:
    if not parts or any(part.get("type") not in ("text", "sticker") for part in parts):
        raise ValueError("回复计划仍含未确定的表情分类")
    existing = con.execute("SELECT status FROM reply_parts WHERE message_id=?",
                           (message_id,)).fetchall()
    if any(row[0] in ("sending", "confirmed", "uncertain") for row in existing):
        raise ValueError("已有发送中的回复步骤，禁止覆盖或重放")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM reply_parts WHERE message_id=?", (message_id,))
        con.executemany("INSERT INTO reply_parts(message_id,ordinal,kind,content,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        [(message_id, index, part["type"],
                          part["text"] if part["type"] == "text" else part["md5"],
                          int(time.time()))
                         for index, part in enumerate(parts)])
        con.commit()
    except Exception:
        con.rollback()
        raise


def _set(con, message_id: int, index: int, status: str) -> None:
    con.execute("UPDATE reply_parts SET status=?,updated_at=? WHERE message_id=? AND ordinal=?",
                (status, int(time.time()), message_id, index))
    con.commit()


def _may_continue(con, cfg_loader, talker: str, message_id: int, event_time: int,
                  confirmed: int) -> bool:
    cfg = cfg_loader()
    opts = cfg.get("auto_send", {})
    if control.current_mode(con) != "auto" or not opts.get("enabled") or \
            talker not in opts.get("allow_talkers", []):
        return False
    if con.execute("SELECT 1 FROM messages WHERE talker=? AND is_sender=0 "
                   "AND id>? LIMIT 1", (talker, message_id)).fetchone():
        return False
    sent = con.execute("SELECT COUNT(*) FROM messages WHERE talker=? AND is_sender=1 "
                       "AND create_time>=?", (talker, event_time)).fetchone()[0]
    return sent <= confirmed


def send(con, cfg: dict, cfg_loader, sender, talker: str, message_id: int,
         event_time: int, parts: list[dict], sticker_sender=None) -> str:
    """First-step preflight may retry; after a confirmed step, never replay a plan."""
    prepare(con, message_id, parts)
    confirmed = 0
    for index, part in enumerate(parts):
        if not _may_continue(con, cfg_loader, talker, message_id, event_time, confirmed):
            _set(con, message_id, index, "stopped")
            return "uncertain" if confirmed else "preflight"
        _set(con, message_id, index, "sending")
        try:
            if part["type"] == "sticker":
                if sticker_sender is None:
                    raise ValueError("表情发送器不可用")
                status = sticker_sender(cfg, con, talker, part["md5"])
            else:
                status = sender(cfg, con, talker, part["text"])
        except Exception as exc:
            from ..wx.uia_send import SendError
            if isinstance(exc, SendError):
                _set(con, message_id, index, "preflight")
                if not confirmed:
                    raise
                return "uncertain"
            _set(con, message_id, index, "uncertain")
            raise
        _set(con, message_id, index, status)
        if status != "confirmed":
            return "uncertain"
        confirmed += 1
    return "confirmed"
