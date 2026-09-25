"""行为统计层：纯 SQL/Python 计算，不花 LLM token，是人格画像的地基。"""

import sqlite3
from collections import Counter
from datetime import datetime, timedelta

TEXT_TYPES = (1,)
SESSION_GAP = timedelta(hours=4)   # 超过这个间隔视为新会话


def _rows(con, talker: str, limit_days: int | None = None):
    sql = ("SELECT is_sender, msg_type, content, create_time FROM messages "
           "WHERE talker=? ORDER BY create_time")
    args = [talker]
    if limit_days:
        cutoff = int((datetime.now() - timedelta(days=limit_days)).timestamp())
        sql += " AND create_time >= ?"
        args.append(cutoff)
    return con.execute(sql, args).fetchall()


def stats_for(con: sqlite3.Connection, talker: str, my_wxid: str = "",
              limit_days: int | None = None) -> dict:
    rows = _rows(con, talker, limit_days)
    if not rows:
        return {"talker": talker, "total": 0}

    mine = [r for r in rows if r["is_sender"] == 1]
    theirs = [r for r in rows if r["is_sender"] == 0]
    texts = [r for r in rows if r["msg_type"] in TEXT_TYPES]

    # --- 活跃时段（对方的） ---
    hours = Counter(datetime.fromtimestamp(r["create_time"]).hour
                    for r in theirs)
    # --- 表达习惯（对方的文本） ---
    t_texts = [r["content"] for r in texts if r["is_sender"] == 0]
    t_lens = [len(t) for t in t_texts]
    sticker = sum(1 for r in theirs if r["msg_type"] == 47)
    voice = sum(1 for r in theirs if r["msg_type"] == 34)
    image = sum(1 for r in theirs if r["msg_type"] == 3)
    question = sum(1 for t in t_texts if "?" in t or "？" in t)
    exclaim = sum(1 for t in t_texts if "!" in t or "！" in t)
    ellipsis = sum(1 for t in t_texts if "…" in t or ".." in t)
    laugh = sum(1 for t in t_texts if "哈哈" in t or "hhh" in t.lower())

    # --- 会话切分 & 主动性 / 响应 ---
    sessions = _split_sessions(rows)
    they_start = me_start = 0
    their_replies, my_replies = [], []
    for s in sessions:
        if not s:
            continue
        if s[0]["is_sender"] == 0:
            they_start += 1
        else:
            me_start += 1
        # 响应延迟：会话内对方回复我上一条的平均间隔
        prev = None
        for r in s:
            if prev is not None:
                dt = r["create_time"] - prev["create_time"]
                if dt < 3600:  # 1小时内才算"回复"
                    if prev["is_sender"] == 1 and r["is_sender"] == 0:
                        their_replies.append(dt)
                    elif prev["is_sender"] == 0 and r["is_sender"] == 1:
                        my_replies.append(dt)
            prev = r

    total_days = max(1, (rows[-1]["create_time"] - rows[0]["create_time"]) / 86400)
    active_hours = ", ".join(f"{h}点({c})" for h, c in hours.most_common(5))

    return {
        "talker": talker,
        "total": len(rows),
        "mine": len(mine),
        "theirs": len(theirs),
        "days_covered": round(total_days, 1),
        "msgs_per_day": round(len(rows) / total_days, 2),
        "sessions": len(sessions),
        "they_initiate_pct": round(100 * they_start / max(1, they_start + me_start)),
        "their_avg_reply_min": round(_mean(their_replies) / 60, 1) if their_replies else None,
        "my_avg_reply_min": round(_mean(my_replies) / 60, 1) if my_replies else None,
        "their_avg_len": round(_mean(t_lens), 1) if t_lens else None,
        "their_sticker_pct": round(100 * sticker / max(1, len(theirs))),
        "their_voice_count": voice,
        "their_image_count": image,
        "question_pct": round(100 * question / max(1, len(t_texts))) if t_texts else 0,
        "exclaim_pct": round(100 * exclaim / max(1, len(t_texts))) if t_texts else 0,
        "ellipsis_pct": round(100 * ellipsis / max(1, len(t_texts))) if t_texts else 0,
        "laugh_pct": round(100 * laugh / max(1, len(t_texts))) if t_texts else 0,
        "top_active_hours": active_hours,
        "first_msg_at": rows[0]["create_time"],
        "last_msg_at": rows[-1]["create_time"],
    }


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0


def _split_sessions(rows):
    out, cur = [], []
    prev_t = None
    for r in rows:
        t = r["create_time"]
        if prev_t is not None and t - prev_t > SESSION_GAP.total_seconds():
            out.append(cur)
            cur = []
        cur.append(r)
        prev_t = t
    if cur:
        out.append(cur)
    return out


def render_stats(s: dict) -> str:
    """给 LLM 看的紧凑统计摘要。"""
    if s.get("total", 0) == 0:
        return "（无聊天记录）"
    lines = [
        f"总消息 {s['total']} 条（我 {s['mine']} / TA {s['theirs']}），"
        f"跨度 {s['days_covered']} 天，日均 {s['msgs_per_day']} 条，共 {s['sessions']} 个会话",
        f"TA 主动开启会话占比 {s['they_initiate_pct']}%",
        f"平均回复时长：TA {s['their_avg_reply_min']} 分钟 / 我 {s['my_avg_reply_min']} 分钟",
        f"TA 平均每条 {s['their_avg_len']} 字；表情包 {s['their_sticker_pct']}%，"
        f"语音 {s['their_voice_count']} 条，图片 {s['their_image_count']} 张",
        f"问号 {s['question_pct']}%，感叹号 {s['exclaim_pct']}%，省略号 {s['ellipsis_pct']}%，"
        f"哈哈/hhh {s['laugh_pct']}%",
        f"TA 常活跃时段：{s['top_active_hours']}",
    ]
    return "\n".join(lines)


def sample_conversations(con, talker: str, max_msgs: int = 400,
                         max_sessions: int = 12) -> list[dict]:
    """抽样会话：最近的 + 历史上最长的，供 LLM 画像。"""
    rows = _rows(con, talker)
    if not rows:
        return []
    sessions = _split_sessions(rows)
    # 会话元数据
    meta = []
    for i, s in enumerate(sessions):
        meta.append((i, len(s), s[0]["create_time"]))
    recent = sorted(meta, key=lambda x: x[2])[-6:]
    longest = sorted(meta, key=lambda x: x[1])[-6:]
    picked = list({i for i, _, _ in recent} | {i for i, _, _ in longest})

    out, count = [], 0
    for i in sorted(picked, key=lambda i: sessions[i][0]["create_time"]):
        conv = {"start": datetime.fromtimestamp(sessions[i][0]["create_time"])
                .strftime("%Y-%m-%d %H:%M"), "msgs": []}
        for r in sessions[i]:
            conv["msgs"].append(("我" if r["is_sender"] == 1 else "TA")
                                + (": " + (r["content"] or "") if r["msg_type"] == 1
                                   else f": [第{r['msg_type']}类消息]"))
            count += 1
            if count >= max_msgs:
                break
        out.append(conv)
        if count >= max_msgs:
            break
    _ = max_sessions
    return out
