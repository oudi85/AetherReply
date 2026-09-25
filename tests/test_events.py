"""验证首次水位、来讯过滤、去重、延迟与重启后继续。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_reply.core import events, store


def add(con, sender, created, content="test"):
    con.execute(
        "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,"
        "src_db,src_table,src_rowid) VALUES(?,?,?,?,?,?,?,?)",
        ("wxid_test", sender, 1, content, created, "message_0.db", "Msg_test",
         con.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0]))
    con.commit()


def main():
    with tempfile.TemporaryDirectory(prefix="auto_reply_events_") as tmp:
        path = Path(tmp) / "mirror.sqlite"
        con = store.connect(path)
        add(con, 0, 900000)
        assert events.enqueue_new(con, now=1000000)["baseline"] == 1
        assert not events.ready_batches(con, now=1000010)

        con.execute(
            "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,"
            "src_db,src_table,src_rowid) VALUES(?,?,?,?,?,?,?,?)",
            ("filehelper", 0, 1, "my self-test", 1000001,
             "message_0.db", "Msg_filehelper", 1))
        con.commit()
        assert events.enqueue_new(con, now=1000002)["queued"] == 0
        assert not events.ready_batches(con, now=1000010)

        add(con, 0, 1000001)
        add(con, 1, 1000002)
        add(con, 0, 900000)
        result = events.enqueue_new(con, now=1000003)
        assert (result["queued"], result["skipped"]) == (1, 1)
        assert events.enqueue_new(con, now=1000004)["queued"] == 0
        assert not events.ready_batches(con, now=1000004)
        batch = events.ready_batches(con, now=1000006)[0]
        assert batch["message_count"] == 1
        events.defer(con, batch["talker"], batch["newest_id"], now=1000006)
        assert not events.ready_batches(con, now=1000030)
        events.mark_done(con, batch["talker"], batch["newest_id"])
        con.close()

        con = store.connect(path)
        add(con, 0, 1000070)
        assert events.enqueue_new(con, now=1000071)["queued"] == 1
        add(con, 0, 1000071)
        assert events.enqueue_new(con, now=1000072)["queued"] == 1
        assert not events.ready_batches(con, now=1000073)
        last = events.ready_batches(con, now=1000074)[0]
        assert last["message_count"] == 2
        events.mark_sending(con, last["talker"], last["newest_id"])
        assert not events.ready_batches(con, now=1000100)
        events.mark_result(con, last["talker"], last["newest_id"], "uncertain")
        assert not events.ready_batches(con, now=1000100)
        con.close()
    print("[+] 事件水位、去重、过期过滤与重启恢复测试通过")


if __name__ == "__main__":
    main()
