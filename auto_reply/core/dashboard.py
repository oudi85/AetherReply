"""控制台读取的只读快照；不依赖 Tk，便于在后台线程执行和单独测试。

统计口径（按 incoming_events 每条来讯计数，互不重叠）：
  waiting   待处理：pending 且从未失败过，正在等待生成/发送
  retrying  重试中：pending 且 attempts>0，生成失败或发送前核对失败，等待下次自动重试
  drafted   已生成：仅生成模式下已保存候选、不会发送
  sent      已发送：已在本地消息库确认
  review    待核对：uncertain（按下 Enter 后无法确认），或 sending 状态超过
            STALE_SENDING_SECONDS 仍未结束（进程中断），都不会自动重发
不计入任何卡片：skipped/done 是正常跳过；刚进入 sending 的那一条是瞬时状态。
"""

import json
import time

STALE_SENDING_SECONDS = 120
# 大于模型请求超时（120s），生成回复期间不会误报
SYNC_STALE_SECONDS = 150
COUNT_KEYS = ("waiting", "retrying", "drafted", "sent", "review")
BUCKET_LABELS = {
    "waiting": "待处理", "retrying": "重试中", "sending": "发送中", "drafted": "已生成",
    "sent": "已发送", "review": "待核对", "skipped": "已跳过", "done": "已完成",
}
# 会话列表角标优先级：越靠前越需要关注
BADGE_ORDER = ("review", "retrying", "sending", "waiting", "drafted")
TYPE_LABELS = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]",
               48: "[位置]", 49: "[链接/文件]", 50: "[通话]"}


def bucket(status: str, attempts: int, sending_since: int | None, now: int) -> str:
    if status == "pending":
        return "retrying" if attempts else "waiting"
    if status == "sending":
        return "review" if (sending_since or 0) < now - STALE_SENDING_SECONDS else "sending"
    if status == "uncertain":
        return "review"
    return status


def message_text(msg_type: int, content: str) -> str:
    if msg_type == 1:
        return content or ""
    return TYPE_LABELS.get(msg_type, content if msg_type == 10000 else "[消息]")


def _sending_since(con, talkers: list[str]) -> dict:
    # 同一会话的一批来讯只在最新一条上记录决策，因此按会话取“开始发送”的时间
    marks = ",".join("?" for _ in talkers)
    return {r[0]: r[1] for r in con.execute(
        "SELECT e.talker,MAX(d.updated_at) FROM incoming_events e "
        "JOIN reply_decisions d ON d.message_id=e.message_id "
        f"WHERE e.status='sending' AND e.talker IN ({marks}) GROUP BY e.talker",
        talkers)}


def names_for(con, talkers: list[str]) -> dict:
    if not talkers:
        return {}
    marks = ",".join("?" for _ in talkers)
    found = {r["wxid"]: (r["remark"] or r["nickname"] or r["wxid"]) for r in con.execute(
        f"SELECT wxid,remark,nickname FROM contacts WHERE wxid IN ({marks})", talkers)}
    return {t: found.get(t, t) for t in talkers}


def load_snapshot(con, allowed: list[str], *, selected: str | None = None,
                  now: int | None = None, limit: int = 150) -> dict:
    """con 需使用 sqlite3.Row。只读，不写库。"""
    from . import control

    now = int(time.time()) if now is None else now
    snap = {"now": now, "mode": control.current_mode(con),
            "health": control.worker_health(con),
            "sessions": [], "counts": dict.fromkeys(COUNT_KEYS, 0), "chat": None}
    if allowed:
        marks = ",".join("?" for _ in allowed)
        since = _sending_since(con, allowed)
        badges: dict[str, set] = {}
        for r in con.execute(
                "SELECT talker,status,attempts>0 retried,COUNT(*) n FROM incoming_events "
                f"WHERE talker IN ({marks}) AND status IN "
                "('pending','sending','sent','uncertain','drafted') "
                "GROUP BY talker,status,retried", allowed):
            key = bucket(r["status"], r["retried"], since.get(r["talker"]), now)
            if key in snap["counts"]:
                snap["counts"][key] += r["n"]
            badges.setdefault(r["talker"], set()).add(key)
        names = names_for(con, allowed)
        for talker in allowed:
            last = con.execute(
                "SELECT id,is_sender,msg_type,content,create_time FROM messages "
                "WHERE talker=? ORDER BY create_time DESC,id DESC LIMIT 1",
                (talker,)).fetchone()
            keys = badges.get(talker, set())
            badge = next((k for k in BADGE_ORDER[:-1] if k in keys), None)
            if badge is None and last is not None and not last["is_sender"]:
                # 已生成但你还没回复时提示一下；回复之后角标消失
                drafted = con.execute(
                    "SELECT 1 FROM incoming_events WHERE talker=? AND message_id=? "
                    "AND status='drafted'", (talker, last["id"])).fetchone()
                badge = "drafted" if drafted else None
            snap["sessions"].append({
                "talker": talker, "name": names[talker], "badge": badge,
                "last_ts": last["create_time"] if last else 0,
                "last_text": message_text(last["msg_type"], last["content"]) if last else "",
                "last_mine": bool(last["is_sender"]) if last else False,
            })
        snap["sessions"].sort(key=lambda s: -s["last_ts"])
    if selected:
        snap["chat"] = load_chat(con, selected, now=now, limit=limit)
    return snap


def load_chat(con, talker: str, *, now: int | None = None, limit: int = 150) -> dict:
    """一个会话最近的消息，按时间正序；来讯上附带事件状态与模型候选。"""
    now = int(time.time()) if now is None else now
    msgs = con.execute(
        "SELECT id,is_sender,msg_type,content,create_time FROM messages WHERE talker=? "
        "ORDER BY create_time DESC,id DESC LIMIT ?", (talker, limit)).fetchall()[::-1]
    chat = {"talker": talker, "name": names_for(con, [talker])[talker],
            "items": [], "pending": None, "sig": None}
    if not msgs:
        chat["sig"] = (talker,)
        return chat
    first = min(m["id"] for m in msgs)
    since = _sending_since(con, [talker]).get(talker)
    evs = {r["message_id"]: r for r in con.execute(
        "SELECT message_id,status,attempts,next_attempt_at FROM incoming_events "
        "WHERE talker=? AND message_id>=?", (talker, first))}
    decs = {r["message_id"]: r for r in con.execute(
        "SELECT d.message_id,d.selected_index,d.candidates_json,d.analysis,d.status,"
        "d.error,d.updated_at FROM reply_decisions d JOIN messages m ON m.id=d.message_id "
        "WHERE m.talker=? AND d.message_id>=?", (talker, first))}
    parts_by_message = {}
    for part in con.execute(
            "SELECT p.message_id,p.kind,p.content,p.status FROM reply_parts p "
            "JOIN messages m ON m.id=p.message_id WHERE m.talker=? "
            "AND p.message_id>=? ORDER BY p.message_id,p.ordinal", (talker, first)):
        parts_by_message.setdefault(part["message_id"], []).append(part)
    expect = None
    sig = [talker]
    for m in msgs:
        item = {"id": m["id"], "mine": bool(m["is_sender"]), "type": m["msg_type"],
                "text": message_text(m["msg_type"], m["content"]), "ts": m["create_time"],
                "ai": False, "decision": None}
        ev = evs.get(m["id"])
        bkt = bucket(ev["status"], ev["attempts"], since, now) if ev else None
        d = decs.get(m["id"])
        if d is not None:
            try:
                cands = [c.get("text", "") if isinstance(c, dict) else str(c)
                         for c in json.loads(d["candidates_json"] or "[]")]
            except ValueError:
                cands = []
            item["decision"] = {
                "bucket": bkt or d["status"], "status": d["status"],
                "candidates": [c for c in cands if c], "selected": d["selected_index"],
                "analysis": d["analysis"] or "", "error": d["error"] or "",
                "attempts": ev["attempts"] if ev else 0,
                "next_attempt_at": ev["next_attempt_at"] if ev else 0,
            }
            confirmed_parts = [part for part in parts_by_message.get(m["id"], [])
                               if part["status"] == "confirmed"]
            if confirmed_parts:
                expect = [part["content"] if part["kind"] == "text" else "[表情]"
                          for part in confirmed_parts]
            elif d["status"] == "sent" and 0 <= d["selected_index"] < len(cands):
                expect = [cands[d["selected_index"]].strip()]
            sig.append((m["id"], bkt, d["status"], d["updated_at"]))
        elif ev is not None:
            sig.append((m["id"], bkt))
        if item["mine"] and expect and item["text"].strip() == expect[0]:
            item["ai"] = True
            expect.pop(0)
        chat["items"].append(item)
    # 还没有候选的最新来讯：显示“生成中”
    for item in reversed(chat["items"]):
        if item["mine"]:
            break
        ev = evs.get(item["id"])
        if ev is not None and ev["status"] in ("pending", "sending"):
            if item["decision"] is None:
                chat["pending"] = bucket(ev["status"], ev["attempts"], since, now)
            break
    sig.append((msgs[-1]["id"], len(msgs), chat["pending"]))
    chat["sig"] = tuple(sig)
    return chat


def search_contacts(con, term: str, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT wxid,remark,nickname FROM contacts WHERE "
        "wxid LIKE ? OR remark LIKE ? OR nickname LIKE ? "
        "ORDER BY (remark=? OR nickname=?) DESC, length(remark) DESC "
        "LIMIT ?", (f"%{term}%",) * 3 + (term, term, limit)).fetchall()
    return [{"wxid": r["wxid"], "label": r["remark"] or r["nickname"] or r["wxid"]}
            for r in rows]


def health_view(health: dict, *, running: bool, mode: str, enabled: bool,
                now: int) -> dict:
    """把后台状态整理成界面文案：state 为顶部状态，sync 为最近成功同步，issue 为未解决问题。"""
    if not running:
        state = ("bad", "后台未运行")
    elif mode == "paused":
        state = ("warn", "已暂停")
    elif mode == "auto" and not enabled:
        state = ("warn", "自动发送未启用")
    else:
        state = ("ok", "运行中")
    ok_at = health.get("sync_ok_at")
    if ok_at is None:
        sync = ("warn", "暂无同步记录")
    else:
        age = max(0, now - ok_at)
        level = "ok" if age <= SYNC_STALE_SECONDS else "warn" if running else "bad"
        sync = (level, f"{format_age(age)}同步" if age >= 5 else "刚刚同步")
    issue = health.get("issue")
    if issue and issue.get("at", 0) > (ok_at or 0):
        issue = (issue["message"], issue["at"])
    else:
        issue = None
    return {"state": state, "sync": sync, "issue": issue}


def format_age(seconds: int) -> str:
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def format_clock(ts: int, now: int) -> str:
    """会话列表与聊天分隔线用的时间，风格同微信。"""
    t, n = time.localtime(ts), time.localtime(now)
    if t.tm_year == n.tm_year and t.tm_yday == n.tm_yday:
        return time.strftime("%H:%M", t)
    if t.tm_year == n.tm_year and n.tm_yday - t.tm_yday == 1:
        return "昨天 " + time.strftime("%H:%M", t)
    if t.tm_year == n.tm_year:
        return f"{t.tm_mon}月{t.tm_mday}日 " + time.strftime("%H:%M", t)
    return time.strftime("%Y/%m/%d %H:%M", t)
