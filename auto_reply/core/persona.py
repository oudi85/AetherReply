"""人格画像引擎：统计 + 抽样对话 → LLM 人格卡片（支持增量合并）。"""

import json
import time

from .. import llm
from ..prompts import persona_prompts as P
from . import stats

# 累计新消息超过这个数才值得重跑画像
REBUILD_THRESHOLD = 200
# 增量合并时只喂最近 N 天样本
MERGE_SAMPLE_DAYS = 30


def build(cfg: dict, con, talker: str, force: bool = False) -> dict:
    """首次生成或增量更新人格卡片，返回卡片 dict。"""
    total = con.execute("SELECT COUNT(*) c FROM messages WHERE talker=?",
                        (talker,)).fetchone()["c"]
    if total == 0:
        raise ValueError(f"没有 {talker} 的聊天记录，先跑 sync")

    old = con.execute("SELECT * FROM personas WHERE wxid=?", (talker,)).fetchone()
    if old and not force and total - old["built_msg_count"] < REBUILD_THRESHOLD:
        return json.loads(old["card_json"])

    s = stats.stats_for(con, talker)
    if old and not force:
        convs = stats.sample_conversations(
            con, talker, max_msgs=300,
        )
        convs = _filter_days(convs, MERGE_SAMPLE_DAYS)
        msgs = [
            {"role": "system", "content": P.MERGE_SYSTEM},
            {"role": "user", "content": P.MERGE_USER_TMPL.format(
                old_card=old["card_json"], stats=stats.render_stats(s),
                n_conv=len(convs), convs=_fmt_convs(convs, cfg))},
        ]
    else:
        convs = stats.sample_conversations(con, talker, max_msgs=400)
        msgs = [
            {"role": "system", "content": P.PERSONA_SYSTEM},
            {"role": "user", "content": P.PERSONA_USER_TMPL.format(
                stats=stats.render_stats(s), n_conv=len(convs),
                convs=_fmt_convs(convs, cfg))},
        ]

    card = llm.chat_json(cfg, msgs, temperature=0.4, max_tokens=3000)
    con.execute(
        "INSERT INTO personas(wxid, card_json, built_at, built_msg_count) "
        "VALUES(?,?,?,?) ON CONFLICT(wxid) DO UPDATE SET card_json=?, "
        "built_at=?, built_msg_count=?",
        (talker, json.dumps(card, ensure_ascii=False), int(time.time()), total,
         json.dumps(card, ensure_ascii=False), int(time.time()), total))
    con.commit()
    return card


def get(con, talker: str) -> dict | None:
    row = con.execute("SELECT card_json FROM personas WHERE wxid=?",
                      (talker,)).fetchone()
    return json.loads(row["card_json"]) if row else None


def render_card(card: dict) -> str:
    """终端友好的卡片渲染。"""
    out = [f"== {card.get('name', '对方')} · {card.get('archetype', '')} =="]
    bf = card.get("big_five", {})
    if bf:
        out.append("大五: " + " | ".join(
            f"{k}: {v if isinstance(v, int) else v.get('score', '?')}"
            for k, v in bf.items()))
    for key, title in [("communication_style", "沟通风格"), ("values", "在意的事"),
                       ("turn_offs", "雷区"), ("how_to_approach", "相处之道"),
                       ("topics_that_land", "聊得来的话题"),
                       ("topics_that_die", "聊死了的话题"),
                       ("red_flags", "风险提示")]:
        v = card.get(key)
        if v:
            out.append(f"{title}: " + "；".join(x if isinstance(x, str)
                                               else json.dumps(x, ensure_ascii=False)
                                               for x in v))
    if card.get("humor"):
        out.append(f"幽默: {card['humor']}")
    if card.get("confidence") is not None:
        out.append(f"置信度: {card['confidence']}/100")
    return "\n".join(out)


def _fmt_convs(convs: list[dict], cfg: dict) -> str:
    parts = []
    for c in convs:
        body = "\n".join(c["msgs"])
        if cfg["llm"].get("redact_sensitive"):
            body = llm.redact(body)
        parts.append(f"— {c['start']} —\n{body}")
    return "\n\n".join(parts)


def _filter_days(convs: list[dict], days: int) -> list[dict]:
    import datetime as dt
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    kept = []
    for c in convs:
        try:
            if dt.datetime.strptime(c["start"], "%Y-%m-%d %H:%M") >= cutoff:
                kept.append(c)
        except ValueError:
            kept.append(c)
    return kept or convs[-3:]
