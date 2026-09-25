"""本人表达习惯 + 当前会话上下文 + 最新来讯 → 候选回复。"""

import json

from .. import llm
from ..prompts import suggest_prompts as P
from . import reply_plan, stickers, voice


def _type_label(t: int) -> str:
    return {1: "", 3: "[图片]", 34: "[语音]", 43: "[视频]",
            47: "[表情包]", 48: "[位置]", 49: "[卡片/文件]"}.get(t, f"[类型{t}]")


def context_window(con, talker: str, n: int = 40) -> list[str]:
    rows = con.execute(
        "SELECT is_sender, msg_type, content, create_time FROM messages "
        "WHERE talker=? ORDER BY create_time DESC, id DESC LIMIT ?",
        (talker, n)).fetchall()
    rows = list(reversed(rows))
    from datetime import datetime
    lines = []
    for r in rows:
        body = r["content"] if r["msg_type"] == 1 else _type_label(r["msg_type"])
        t = datetime.fromtimestamp(r["create_time"]).strftime("%m-%d %H:%M")
        who = "我" if r["is_sender"] == 1 else "TA"
        text = (r["content"] or "") if r["msg_type"] == 1 else body
        lines.append(f"{t} {who}: {llm.redact(text) if text else body}")
    return lines


def latest_incoming(con, talker: str) -> dict | None:
    row = con.execute(
        "SELECT msg_type, content, create_time FROM messages "
        "WHERE talker=? AND is_sender=0 ORDER BY create_time DESC, id DESC LIMIT 1",
        (talker,)).fetchone()
    return dict(row) if row else None


def suggest(cfg: dict, con, talker: str, note: str = "") -> dict:
    card = con.execute("SELECT card_json FROM personas WHERE wxid=?",
                       (talker,)).fetchone()
    card_txt = card["card_json"] if card else json.dumps(
        {"note": "没有足够可靠的对方画像；不要猜测关系或性格"}, ensure_ascii=False)

    ctx = "\n".join(context_window(con, talker))
    inc = latest_incoming(con, talker)
    if not inc:
        raise ValueError(f"{talker} 没有收到过消息")
    incoming = inc["content"] if inc["msg_type"] == 1 else _type_label(inc["msg_type"])
    if note:
        incoming += f"\n（用户补充说明: {note}）"

    voice.ensure_index(con)
    own_style = voice.guidance(con, talker, incoming)

    plan_opts = cfg.get("reply_plan", {})
    system = P.SUGGEST_SYSTEM
    categories = {}
    if plan_opts.get("enabled"):
        system += P.MULTI_SYSTEM.format(max_parts=max(1, min(3, int(plan_opts.get("max_parts", 3)))))
        if plan_opts.get("allow_stickers"):
            categories = stickers.available_categories(cfg, con)
            if categories:
                system += P.STICKER_SYSTEM.format(options="\n".join(
                    f"- {name}: {guidance or '按分类名理解'}"
                    for name, guidance in categories.items()))
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": P.SUGGEST_USER_TMPL.format(
            card=card_txt, voice_profile=own_style["profile"],
            voice_examples=own_style["examples"],
            context=ctx, incoming=llm.redact(incoming))},
    ]
    result = llm.chat_json(cfg, msgs, temperature=0.8, max_tokens=1500)
    result["candidates"] = reply_plan.normalize_candidates(
        cfg, result.get("candidates"), allowed_categories=set(categories))
    result["sticker_suggestions"] = stickers.recommend(con, incoming, limit=3)
    return result


def resolve_selected(cfg: dict, con, candidate: dict, incoming: str) -> dict:
    """Only the chosen candidate incurs a second, within-category DS decision."""
    if not any(p.get("type") == "sticker_category"
               for p in (candidate.get("parts") or [])):
        return candidate
    parts = []
    reply_text = " / ".join(p["text"] for p in candidate["parts"]
                            if p["type"] == "text")
    for part in candidate["parts"]:
        if part["type"] != "sticker_category":
            parts.append(part)
            continue
        category = part["category"]
        if category not in stickers.available_categories(cfg, con):
            raise ValueError("表情分类已不可用")
        md5 = stickers.choose_in_category(cfg, con, category, incoming, reply_text)
        if md5:
            parts.append({"type": "sticker", "md5": md5})
    allowed = stickers.available_for_reply(cfg, con)
    return reply_plan.normalize_candidates(cfg, [{**candidate, "parts": parts}], allowed)[0]


def render(result: dict) -> str:
    lines = [f"【意图分析】{result.get('analysis', '')}",
             f"【紧急度】{result.get('urgency', '')}", "", "【候选回复】"]
    for i, c in enumerate(result.get("candidates", []), 1):
        lines.append(f"{i}. ({c.get('style', '')}) {c.get('text', '')}")
        lines.append(f"   ↳ {c.get('why', '')}")
    lines.append("")
    lines.append(f"【注意】{result.get('avoid', '')}")
    if result.get("sticker_suggestions"):
        lines.append("【收藏表情建议（仅展示，未自动发送）】")
        lines.extend(f"- {item['md5']}: {','.join(item['tags'])}"
                     for item in result["sticker_suggestions"])
    return "\n".join(lines)
