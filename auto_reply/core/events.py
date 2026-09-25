"""把新导入的来讯持久化为待处理事件；不负责发送。"""

import time


def enqueue_new(con, *, max_age_seconds: int = 300, now: int | None = None) -> dict:
    """首次运行建立水位，不把历史消息当作来讯。之后按镜像 ID 去重入队。"""
    now = int(time.time()) if now is None else now
    con.execute("BEGIN IMMEDIATE")
    try:
        newest = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        cursor = con.execute(
            "SELECT last_id FROM event_cursor WHERE name='incoming'").fetchone()
        if cursor is None:
            con.execute("INSERT INTO event_cursor(name,last_id) VALUES('incoming',?)",
                        (newest,))
            result = {"baseline": newest, "queued": 0, "skipped": 0}
        else:
            old = cursor[0]
            eligible = con.execute(
                "SELECT COUNT(*) FROM messages WHERE id>? AND id<=? "
                "AND is_sender=0 AND talker<>'filehelper' "
                "AND msg_type=1 AND content<>''",
                (old, newest)).fetchone()[0]
            con.execute(
                "INSERT OR IGNORE INTO incoming_events(message_id,talker,seen_at) "
                "SELECT id,talker,? FROM messages WHERE id>? AND id<=? "
                "AND is_sender=0 AND talker<>'filehelper' "
                "AND msg_type=1 AND content<>'' AND create_time>=?",
                (now, old, newest, now - max_age_seconds))
            queued = con.execute("SELECT changes()").fetchone()[0]
            con.execute("UPDATE event_cursor SET last_id=? WHERE name='incoming'",
                        (newest,))
            result = {"baseline": 0, "queued": queued,
                      "skipped": eligible - queued}
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise


def ready_batches(con, *, debounce_seconds: int = 2,
                  now: int | None = None, limit: int = 100):
    now = int(time.time()) if now is None else now
    return con.execute(
        "SELECT talker, MAX(message_id) newest_id, COUNT(*) message_count "
        "FROM incoming_events WHERE status='pending' GROUP BY talker "
        "HAVING MAX(seen_at)<=? AND MAX(next_attempt_at)<=? "
        "ORDER BY newest_id LIMIT ?",
        (now - debounce_seconds, now, limit)).fetchall()


def mark_done(con, talker: str, newest_id: int) -> None:
    con.execute(
        "UPDATE incoming_events SET status='done' "
        "WHERE talker=? AND message_id<=? AND status='pending'",
        (talker, newest_id))
    con.commit()


def mark_sending(con, talker: str, newest_id: int) -> None:
    """Persist an attempt before pressing Enter so a crash cannot auto-repeat it."""
    con.execute(
        "UPDATE incoming_events SET status='sending' "
        "WHERE talker=? AND message_id<=? AND status='pending'",
        (talker, newest_id))
    con.commit()


def mark_drafted(con, talker: str, newest_id: int) -> None:
    """Finalize a generated batch without entering any UIA send state."""
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "UPDATE incoming_events SET status='drafted' "
            "WHERE talker=? AND message_id<=? AND status='pending'",
            (talker, newest_id))
        con.execute(
            "UPDATE reply_decisions SET status='drafted',error='',updated_at=? "
            "WHERE message_id=?", (int(time.time()), newest_id))
        con.commit()
    except Exception:
        con.rollback()
        raise


def skip_pending(con, talker: str, newest_id: int) -> None:
    con.execute(
        "UPDATE incoming_events SET status='skipped' "
        "WHERE talker=? AND message_id<=? AND status='pending'",
        (talker, newest_id))
    con.commit()


def mark_result(con, talker: str, newest_id: int, status: str) -> None:
    if status not in ("sent", "uncertain"):
        raise ValueError(status)
    con.execute(
        "UPDATE incoming_events SET status=? "
        "WHERE talker=? AND message_id<=? AND status='sending'",
        (status, talker, newest_id))
    con.commit()


def retry_preflight(con, talker: str, newest_id: int, *, seconds: int = 60,
                    now: int | None = None) -> None:
    """Only for failures known to occur before Enter was attempted."""
    now = int(time.time()) if now is None else now
    con.execute(
        "UPDATE incoming_events SET status='pending',attempts=attempts+1,"
        "next_attempt_at=? WHERE talker=? AND message_id<=? AND status='sending'",
        (now + seconds, talker, newest_id))
    con.commit()


def defer(con, talker: str, newest_id: int, *, seconds: int = 60,
          now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    con.execute(
        "UPDATE incoming_events SET attempts=attempts+1, next_attempt_at=? "
        "WHERE talker=? AND message_id<=? AND status='pending'",
        (now + seconds, talker, newest_id))
    con.commit()
