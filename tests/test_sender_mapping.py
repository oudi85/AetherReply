"""WeChat 4.x sender IDs are local to each message database."""

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import store
from auto_reply.wx.sync import _import_messages_4x, _repair_sender_flags_4x


def main():
    me = "wxid_me"
    other = "wxid_other"
    table = "Msg_" + hashlib.md5(other.encode()).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "message_0.sqlite"
        with sqlite3.connect(source) as raw:
            raw.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
            raw.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(7,?,0)", (me,))
            raw.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(4,?,1)", (other,))
            raw.execute(f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY, '
                        'local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, '
                        'message_content TEXT)')
            raw.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?)', [
                (1, 1, 4, 100, "incoming"),
                (2, 1, 7, 101, "outgoing"),
            ])
        raw.close()
        mirror = store.connect(root / "mirror.sqlite")
        mirror.execute("INSERT INTO contacts(wxid) VALUES(?)", (other,))
        cols = ["local_id", "local_type", "real_sender_id", "create_time",
                "message_content"]
        assert _import_messages_4x(mirror, source, "message_0.db", [(table, cols)], me) == 2
        assert mirror.execute(
            "SELECT is_sender FROM messages ORDER BY src_rowid").fetchall()[0][0] == 0
        assert mirror.execute(
            "SELECT is_sender FROM messages ORDER BY src_rowid").fetchall()[1][0] == 1

        mirror.execute("UPDATE messages SET is_sender=0")
        mirror.execute("INSERT INTO incoming_events(message_id,talker,seen_at) "
                       "SELECT id,talker,102 FROM messages WHERE content='outgoing'")
        mirror.commit()
        _repair_sender_flags_4x(mirror, source, "message_0.db", me)
        assert [r[0] for r in mirror.execute(
            "SELECT is_sender FROM messages ORDER BY src_rowid")] == [0, 1]
        assert mirror.execute("SELECT count(*) FROM incoming_events").fetchone()[0] == 0
        mirror.close()
    print("[+] 微信 4.x 发送者映射与旧镜像修复测试通过")


if __name__ == "__main__":
    main()
