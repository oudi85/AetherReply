"""Focused checks for portable style and multi-part send boundaries."""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import control, dashboard, reply_plan, store, voice, voice_pack
from auto_reply.core.contacts import SendError


def add(con, talker, mine, kind, body, when):
    rowid = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0]
    cur = con.execute(
        "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,src_db,src_rowid) "
        "VALUES(?,?,?,?,?,'test',?)", (talker, int(mine), kind, body, when, rowid))
    con.commit()
    return cur.lastrowid


def main():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "old.sqlite"
        con = store.connect(path)
        add(con, "friend", False, 1, "来了吗", 1000)
        add(con, "friend", True, 1, "到了", 1005)
        add(con, "friend", True, 1, "马上上楼", 1010)
        add(con, "friend", False, 1, "好", 1200)
        add(con, "friend", True, 1, "嗯", 1210)
        voice.rebuild(con)
        profile = json.loads(con.execute(
            "SELECT profile_json FROM voice_profiles WHERE scope='global'").fetchone()[0])
        assert profile["multi_part_pct"] == 50
        pack = Path(root) / "style.voicepack"
        voice_pack.export_pack(con, "wxid_owner", "long-password", pack)
        assert b"\xe5\x88\xb0\xe4\xba\x86" not in pack.read_bytes()
        moved = store.connect(Path(root) / "new.sqlite")
        voice_pack.import_pack(moved, "wxid_owner", "long-password", pack)
        assert "连发多条" in voice.guidance(moved, "friend", "来了吗")["profile"]
        try:
            voice_pack.import_pack(moved, "another_owner", "long-password", pack)
        except ValueError:
            pass
        else:
            raise AssertionError("cross-account import accepted")

        plan_cfg = {"reply_plan": {"enabled": True, "max_parts": 3,
                                    "allow_stickers": True}}
        sticker_id = "a" * 32
        candidate = reply_plan.normalize_candidates(plan_cfg, [{"parts": [
            {"type": "text", "text": "哈哈"},
            {"type": "sticker", "md5": sticker_id}]}], {sticker_id: "开心"})[0]
        assert len(candidate["parts"]) == 2 and "[表情：开心]" in candidate["text"]
        try:
            reply_plan.normalize_candidates(plan_cfg, [{"parts": [
                {"type": "sticker", "md5": "b" * 32}]}], {sticker_id: "开心"})
        except ValueError:
            pass
        else:
            raise AssertionError("unlisted sticker accepted")

        now = int(time.time())
        incoming = add(con, "friend", False, 1, "测试", now)
        control.set_mode(con, "auto")
        cfg = {"auto_send": {"enabled": True, "allow_talkers": ["friend"]}}
        parts = [{"type": "text", "text": "第一条"},
                 {"type": "text", "text": "第二条"}]
        calls = []
        sent_ids = []

        def sender(_cfg, active, talker, text):
            calls.append(text)
            sent_ids.append(add(active, talker, True, 1, text, now + len(calls)))
            return "confirmed"

        with patch.object(reply_plan.time, "sleep") as pause:
            assert reply_plan.send(con, cfg, lambda: cfg, sender, "friend", incoming,
                                   now, parts) == "confirmed"
        pause.assert_not_called()
        assert calls == ["第一条", "第二条"]
        assert [r[0] for r in con.execute(
            "SELECT status FROM reply_parts WHERE message_id=? ORDER BY ordinal",
            (incoming,))] == ["confirmed", "confirmed"]
        control.record_decision(con, incoming, "default", 0,
                                [{"text": "第一条 / 第二条", "parts": parts}])
        control.set_decision_status(con, incoming, "sent")
        chat = dashboard.load_chat(con, "friend", now=now + 30)
        assert all(item["ai"] for item in chat["items"] if item["id"] in sent_ids)
        assert voice.rebuild(con)["excluded_auto_messages"] >= 2
        try:
            reply_plan.prepare(con, incoming, parts)
        except ValueError:
            pass
        else:
            raise AssertionError("confirmed parts were replayable")

        incoming2 = add(con, "friend", False, 1, "再试", now + 10)
        def interrupted(_cfg, active, talker, text):
            if text == "第一条":
                add(active, talker, True, 1, text, now + 11)
                add(active, talker, False, 1, "插话", now + 12)
            return "confirmed"
        assert reply_plan.send(con, cfg, lambda: cfg, interrupted, "friend",
                               incoming2, now + 10, parts) == "uncertain"
        assert [r[0] for r in con.execute(
            "SELECT status FROM reply_parts WHERE message_id=? ORDER BY ordinal",
            (incoming2,))] == ["confirmed", "stopped"]

        incoming3 = add(con, "friend", False, 1, "预检查", now + 20)
        def preflight(*_):
            raise SendError("before click")
        try:
            reply_plan.send(con, cfg, lambda: cfg, preflight, "friend",
                            incoming3, now + 20, parts)
        except SendError:
            pass
        else:
            raise AssertionError("preflight did not propagate")
        assert con.execute("SELECT status FROM reply_parts WHERE message_id=? "
                           "AND ordinal=0", (incoming3,)).fetchone()[0] == "preflight"
        con.close()
        moved.close()
    print("[+] 风格包账号绑定、连发习惯与逐条发送防重通过")


if __name__ == "__main__":
    main()
