"""Learn the account owner's wording from human-authored local messages.

Other contacts' incoming text is used only for local example retrieval. It is
never included in cross-contact model examples. Sparse contacts inherit the
global profile rather than getting an invented relationship style.
"""

import json
import re
import statistics
import time
from collections import Counter, defaultdict, deque

from .. import llm

INDEX_VERSION = "3"
REBUILD_AFTER_MESSAGES = 200
MAX_INDEX_AGE_SECONDS = 7 * 86400
GLOBAL_SAMPLE_LIMIT = 6000
MIN_CONTACT_MESSAGES = 20
MIN_CONTACT_EXAMPLES = 8
MARKERS = ("哈哈", "哈哈哈", "hh", "嗯", "哦", "啊", "呀", "呢", "吧", "嘛",
           "好呀", "好的", "行", "可以", "收到", "ok", "OK", "～", "😂", "🤣")
FORMAL_PHRASES = ("非常感谢您的", "祝您生活愉快", "如有任何问题", "期待您的回复",
                  "请您知悉", "感谢您的理解与支持")
_UNSAFE_EXAMPLE = re.compile(r"https?://|www\.|wxid_|@|\d{5,}|验证码|身份证|密码|银行卡", re.I)
_GLOBAL_FACT = re.compile(r"\d|今天|明天|后天|下周|已经|我在|我有|我要|我会|我去|地址|转账|金额")
_SPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _SPACE.sub(" ", text or "").strip()


def _usable(text: str, *, example: bool = False) -> bool:
    if example and ("\n" in text or "\r" in text):
        return False
    text = _clean(text)
    if not text or len(text) > (80 if example else 300):
        return False
    if example and _UNSAFE_EXAMPLE.search(text):
        return False
    return True


def _profile(texts: list[str], turns: list[list[tuple]] | None = None) -> dict:
    lengths = [len(t) for t in texts]
    n = len(texts)
    if not n:
        return {"sample_count": 0}
    marker_counts = Counter()
    for t in texts:
        for marker in MARKERS:
            if marker in t:
                marker_counts[marker] += 1
    markers = [m for m, count in marker_counts.most_common()
               if count >= max(3, n // 100)][:6]
    result = {
        "sample_count": n,
        "median_length": round(statistics.median(lengths)),
        "short_pct": round(100 * sum(length <= 12 for length in lengths) / n),
        "question_pct": round(100 * sum("?" in t or "？" in t for t in texts) / n),
        "exclaim_pct": round(100 * sum("!" in t or "！" in t for t in texts) / n),
        "sentence_period_pct": round(100 * sum(t.endswith(("。", ".")) for t in texts) / n),
        "laugh_pct": round(100 * sum("哈哈" in t or "hh" in t.lower() for t in texts) / n),
        "common_markers": markers,
    }
    if turns:
        counts = [len(turn) for turn in turns]
        gaps = [turn[i][2] - turn[i - 1][2] for turn in turns
                for i in range(1, len(turn))]
        result.update(turn_count=len(turns),
                      multi_part_pct=round(100 * sum(c > 1 for c in counts) / len(counts)),
                      median_parts=statistics.median(counts),
                      typical_gap_seconds=round(statistics.median(gaps)) if gaps else 0,
                      sticker_turn_pct=round(100 * sum(any(p[0] == 47 for p in turn)
                                                        for turn in turns) / len(turns)))
    return result


def _auto_sent_ids(con) -> set[int]:
    """Exclude confirmed model replies from examples of the user's own voice."""
    excluded = set()
    for talker, kind, content, incoming_at in con.execute(
            "SELECT m.talker,p.kind,p.content,m.create_time FROM reply_parts p "
            "JOIN messages m ON m.id=p.message_id WHERE p.status='confirmed' "
            "ORDER BY p.message_id,p.ordinal"):
        rows = con.execute(
            "SELECT id FROM messages WHERE talker=? AND is_sender=1 AND msg_type=? "
            "AND create_time BETWEEN ? AND ? "
            + ("AND content=? " if kind == "text" else "")
            + "ORDER BY create_time,id",
            ((talker, 1, incoming_at, incoming_at + 300, content)
             if kind == "text" else (talker, 47, incoming_at, incoming_at + 300))).fetchall()
        match = next((row[0] for row in rows if row[0] not in excluded), None)
        if match is not None:
            excluded.add(match)
    rows = con.execute(
        "SELECT d.selected_index,d.candidates_json,m.talker,m.create_time "
        "FROM reply_decisions d JOIN messages m ON m.id=d.message_id "
        "WHERE d.status='sent'")
    for index, raw, talker, incoming_at in rows:
        try:
            candidate = json.loads(raw)[index]
            text = candidate["text"].strip()
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        row = con.execute(
            "SELECT id FROM messages WHERE talker=? AND is_sender=1 "
            "AND content=? AND create_time BETWEEN ? AND ? "
            "ORDER BY create_time,id LIMIT 1",
            (talker, text, incoming_at, incoming_at + 300)).fetchone()
        if row:
            excluded.add(row[0])
    return excluded


def rebuild(con) -> dict:
    """Rebuild local profiles/examples from the normalized message mirror."""
    max_id = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    excluded = _auto_sent_ids(con)
    per_contact = defaultdict(lambda: deque(maxlen=500))
    turns_by_contact = defaultdict(lambda: deque(maxlen=500))
    all_turns = []
    examples = []
    current_talker, incoming, current_turn = None, [], []
    turn_prompt = None
    turn_reply_id = None
    def finish_turn():
        nonlocal turn_prompt, turn_reply_id
        if current_turn:
            turn = list(current_turn)
            turns_by_contact[current_talker].append(turn)
            all_turns.append((turn[0][2], turn))
            if turn_prompt is not None:
                reply = " / ".join(body if kind == 1 else "[表情包]"
                                   for kind, body, _ in turn)
                if _usable(reply, example=True):
                    incoming_id, context = turn_prompt
                    examples.append((turn_reply_id, current_talker, incoming_id,
                                     context, reply, turn[0][2]))
            current_turn.clear()
        turn_prompt = None
        turn_reply_id = None
    query = (
        "SELECT id,talker,is_sender,msg_type,content,create_time FROM messages "
        "WHERE talker<>'filehelper' AND talker NOT LIKE '%@chatroom' "
        "ORDER BY talker,create_time,id")
    for row in con.execute(query):
        msg_id, talker, mine, kind, content, created = row
        if talker != current_talker:
            finish_turn()
            current_talker, incoming = talker, []
        body = _clean(content) if kind == 1 else ""
        if not mine:
            finish_turn()
            if body and _usable(body):
                if incoming and created - incoming[-1][2] > 3600:
                    incoming = []
                incoming.append((msg_id, body, created))
                incoming = incoming[-3:]
            else:
                incoming = []
            continue
        if current_turn and created - current_turn[-1][2] > 90:
            finish_turn()
        if msg_id not in excluded and (body and _usable(body) or kind == 47):
            if not current_turn and incoming and 0 <= created - incoming[-1][2] <= 3600:
                turn_prompt = (incoming[-1][0],
                               " / ".join(item[1] for item in incoming)[-160:])
                turn_reply_id = msg_id
            current_turn.append((kind, body, created))
        else:
            finish_turn()
        if msg_id not in excluded and body and _usable(body):
            per_contact[talker].append(body)
        incoming = []
    finish_turn()

    global_texts = []
    for msg_id, content in con.execute(
            "SELECT id,content FROM messages WHERE is_sender=1 AND msg_type=1 "
            "AND talker<>'filehelper' AND talker NOT LIKE '%@chatroom' "
            "ORDER BY create_time DESC,id DESC"):
        if msg_id not in excluded and _usable(content):
            global_texts.append(_clean(content))
            if len(global_texts) >= GLOBAL_SAMPLE_LIMIT:
                break

    now = int(time.time())
    recent_turns = [turn for _, turn in sorted(all_turns, key=lambda item: item[0],
                                               reverse=True)[:3000]]
    profiles = [("global", json.dumps(_profile(global_texts, recent_turns), ensure_ascii=False), now)]
    profiles.extend(
        (f"talker:{talker}", json.dumps(_profile(list(texts), list(turns_by_contact[talker])),
                                         ensure_ascii=False), now)
        for talker, texts in per_contact.items() if len(texts) >= MIN_CONTACT_MESSAGES)
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM voice_examples")
        con.executemany(
            "INSERT INTO voice_examples(reply_id,talker,incoming_id,incoming,reply,created_at) "
            "VALUES(?,?,?,?,?,?)", examples)
        con.execute("DELETE FROM voice_profiles")
        con.executemany(
            "INSERT INTO voice_profiles(scope,profile_json,built_at) VALUES(?,?,?)", profiles)
        con.executemany(
            "INSERT INTO voice_index_state(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (("version", INDEX_VERSION), ("max_message_id", str(max_id)),
             ("built_at", str(now))))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {"own_texts_sampled": len(global_texts), "contact_profiles": len(profiles) - 1,
            "reply_examples": len(examples), "excluded_auto_messages": len(excluded)}


def ensure_index(con) -> None:
    state = dict(con.execute("SELECT name,value FROM voice_index_state").fetchall())
    current = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    previous = int(state.get("max_message_id", "0"))
    built_at = int(state.get("built_at", "0"))
    if (state.get("version") != INDEX_VERSION or current < previous or
            current - previous >= REBUILD_AFTER_MESSAGES or
            time.time() - built_at >= MAX_INDEX_AGE_SECONDS):
        rebuild(con)


def _read_profile(con, scope: str) -> dict | None:
    row = con.execute("SELECT profile_json FROM voice_profiles WHERE scope=?", (scope,)).fetchone()
    if row is None or not json.loads(row[0]).get("sample_count"):
        row = con.execute("SELECT profile_json FROM portable_voice_profiles WHERE scope=?",
                          (scope,)).fetchone()
    return json.loads(row[0]) if row else None


def _ngrams(text: str) -> set[str]:
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def _similarity(a: set[str], b: str) -> float:
    other = _ngrams(b)
    return len(a & other) / max(1, len(a | other))


def _format_profile(profile: dict, label: str) -> str:
    markers = "、".join(profile.get("common_markers", [])) or "无明显高频口头语"
    turn_summary = (f"；约 {profile['multi_part_pct']}% 的回复轮次会连发多条，"
                    f"常见间隔 {profile['typical_gap_seconds']} 秒，"
                    f"约 {profile['sticker_turn_pct']}% 带表情包"
                    if profile.get("turn_count") else "")
    return (f"{label}：样本 {profile['sample_count']} 条；每条中位数 "
            f"{profile['median_length']} 字；12字内占 {profile['short_pct']}%；"
            f"问句 {profile['question_pct']}%；感叹句 {profile['exclaim_pct']}%；"
            f"句末句号 {profile['sentence_period_pct']}%；笑声 {profile['laugh_pct']}%；"
            f"常见用语：{markers}{turn_summary}。")


def guidance(con, talker: str, incoming: str) -> dict:
    """Return a compact prompt block; omit other contacts' incoming messages."""
    global_profile = _read_profile(con, "global")
    if not global_profile or not global_profile.get("sample_count"):
        return {"profile": "缺少本人真实消息样本；不要编造用户的习惯。", "examples": "无"}
    contact = _read_profile(con, f"talker:{talker}")
    descriptions = [_format_profile(global_profile, "本人整体表达习惯")]
    if contact:
        descriptions.append(_format_profile(contact, "对当前联系人已有的表达习惯"))
    else:
        descriptions.append("当前联系人样本不足；按本人整体习惯和当前会话语境回复，不猜测关系或专属称呼。")

    grams = _ngrams(incoming)
    same = []
    if contact:
        rows = con.execute(
            "SELECT incoming,reply,created_at FROM voice_examples WHERE talker=? "
            "ORDER BY created_at DESC LIMIT 300", (talker,)).fetchall()
        if len(rows) < MIN_CONTACT_EXAMPLES:
            rows = con.execute(
                "SELECT incoming,reply,created_at FROM portable_voice_examples WHERE talker=? "
                "ORDER BY created_at DESC LIMIT 300", (talker,)).fetchall()
        if len(rows) >= MIN_CONTACT_EXAMPLES:
            same = sorted(rows, key=lambda r: (_similarity(grams, r[0]), r[2]),
                          reverse=True)[:2]

    rows = con.execute(
        "SELECT talker,incoming,reply,created_at FROM voice_examples "
        "ORDER BY created_at DESC LIMIT 8000").fetchall()
    reply_talkers = defaultdict(set)
    for row in rows:
        if row[0] != talker and len(row[2]) <= 40 and not _GLOBAL_FACT.search(row[2]):
            reply_talkers[row[2]].add(row[0])
    shared = [r for r in rows if r[0] != talker and
              len(reply_talkers[r[2]]) >= 2 and len(r[2]) <= 40]
    shared.sort(key=lambda r: (_similarity(grams, r[1]), r[3]), reverse=True)
    global_replies = []
    seen = set()
    for row in shared:
        if row[2] in seen:
            continue
        seen.add(row[2])
        global_replies.append(llm.redact(row[2]))
        if len(global_replies) == 4:
            break
    lines = ["其他会话中本人真实发过的常见短回复（只参考语气，不借用其中事实）："]
    lines.extend(f"- {reply}" for reply in global_replies)
    if not global_replies:
        lines.append("- 无足够可靠的跨会话例句")
    if same:
        lines.append("当前联系人中本人真实回复过的情境：")
        lines.extend(f"- TA：{llm.redact(row[0])} / 我：{llm.redact(row[1])}" for row in same)
    return {"profile": "\n".join(descriptions), "examples": "\n".join(lines)}


def candidate_score(profile: dict, text: str) -> float:
    """A small, explainable tie breaker; content relevance stays with the model."""
    text = _clean(text)
    if not text:
        return -1000
    target = profile.get("median_length", 20)
    score = -min(.8, abs(len(text) - target) / max(8, target))
    if len(text) > 60:
        score -= 2
    if profile.get("sentence_period_pct", 50) < 15 and text.endswith(("。", ".")):
        score -= .3
    if profile.get("exclaim_pct", 0) < 10 and text.count("！") + text.count("!") > 1:
        score -= .3
    if any(phrase in text for phrase in FORMAL_PHRASES):
        score -= 3
    if any(marker in text for marker in profile.get("common_markers", [])):
        score += .15
    return score


def best_candidate(con, talker: str, candidates: list[dict]) -> int | None:
    profile = _read_profile(con, f"talker:{talker}") or _read_profile(con, "global")
    if not profile or not profile.get("sample_count"):
        return None
    usable = []
    for i, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        parts = candidate.get("parts")
        if isinstance(parts, list) and parts:
            text = " ".join(part.get("text", "") for part in parts
                            if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = candidate.get("text", "")
        if isinstance(text, str) and text.strip():
            usable.append((i, text.strip()))
    if not usable:
        return None
    scores = {i: candidate_score(profile, text) for i, text in usable}
    first = usable[0][0]
    best = max(scores, key=lambda i: (scores[i], -i))
    return best if scores[best] - scores[first] >= .5 else first
