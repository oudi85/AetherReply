"""Small shared state between the Windows dashboard and background worker."""

import json
import time


MODES = ("auto", "draft", "paused")


def current_mode(con) -> str:
    """Read the persisted mode, falling back to the original pause flag."""
    rows = dict(con.execute(
        "SELECT name,value FROM runtime_settings WHERE name IN "
        "('reply_mode','auto_send_paused')").fetchall())
    mode = rows.get("reply_mode")
    if mode in MODES:
        return mode
    return "paused" if rows.get("auto_send_paused") == "1" else "auto"


def resume_mode(con) -> str:
    row = con.execute(
        "SELECT value FROM runtime_settings WHERE name='reply_resume_mode'").fetchone()
    if row is not None and row[0] in ("auto", "draft"):
        return row[0]
    mode = current_mode(con)
    return mode if mode != "paused" else "auto"


def is_paused(con) -> bool:
    return current_mode(con) == "paused"


def set_mode(con, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    con.execute("BEGIN IMMEDIATE")
    try:
        previous = current_mode(con)
        resume = resume_mode(con)
        if mode == "paused":
            if previous != "paused":
                resume = previous
        else:
            resume = mode
        con.executemany(
            "INSERT INTO runtime_settings(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (("reply_mode", mode), ("reply_resume_mode", resume),
             ("auto_send_paused", "1" if mode == "paused" else "0")))
        con.commit()
    except Exception:
        con.rollback()
        raise


def set_paused(con, paused: bool) -> None:
    if paused:
        set_mode(con, "paused")
    elif is_paused(con):
        set_mode(con, resume_mode(con))


def _set(con, name: str, value: str) -> None:
    con.execute(
        "INSERT INTO runtime_settings(name,value) VALUES(?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
    con.commit()


def record_sync_ok(con, now: int | None = None) -> None:
    """Only after every changed WeChat DB reached the mirror and events were queued."""
    _set(con, "worker_sync_ok_at", str(int(time.time()) if now is None else now))


def record_worker_issue(con, message: str, now: int | None = None) -> None:
    _set(con, "worker_issue", json.dumps(
        {"at": int(time.time()) if now is None else now, "message": message[:300]},
        ensure_ascii=False))


def worker_health(con) -> dict:
    rows = dict(con.execute(
        "SELECT name,value FROM runtime_settings "
        "WHERE name IN ('worker_sync_ok_at','worker_issue')").fetchall())
    issue = None
    if rows.get("worker_issue"):
        try:
            issue = json.loads(rows["worker_issue"])
        except ValueError:
            issue = None
    ok_at = rows.get("worker_sync_ok_at")
    return {"sync_ok_at": int(ok_at) if ok_at and ok_at.isdigit() else None,
            "issue": issue}


def record_generation_failure(con, message_id: int, error: str) -> None:
    """Keep the reason visible in the dashboard; the event itself stays pending."""
    con.execute(
        "INSERT INTO reply_decisions(message_id,provider,selected_index,"
        "candidates_json,analysis,status,error,updated_at) "
        "VALUES(?,'',-1,'[]','','generate_failed',?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET status='generate_failed',"
        "error=excluded.error,updated_at=excluded.updated_at",
        (message_id, error[:300], int(time.time())))
    con.commit()


def record_decision(con, message_id: int, provider: str, selected_index: int,
                    candidates: list, analysis: str = "") -> None:
    con.execute(
        "INSERT INTO reply_decisions(message_id,provider,selected_index,"
        "candidates_json,analysis,status,updated_at) VALUES(?,?,?,?,?,'ready',?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "provider=excluded.provider,selected_index=excluded.selected_index,"
        "candidates_json=excluded.candidates_json,analysis=excluded.analysis,"
        "status='ready',error='',updated_at=excluded.updated_at",
        (message_id, provider, selected_index,
         json.dumps(candidates, ensure_ascii=False), analysis[:1000], int(time.time())))
    con.commit()


def set_decision_status(con, message_id: int, status: str, error: str = "") -> None:
    con.execute(
        "UPDATE reply_decisions SET status=?,error=?,updated_at=? WHERE message_id=?",
        (status, error[:300], int(time.time()), message_id))
    con.commit()
