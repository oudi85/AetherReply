"""Only human-authored examples shape replies; other chats' incoming text stays local."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import control, decision, store, suggest, voice


def add(con, talker, mine, body, when):
    mid = con.execute(
        "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,src_db,src_rowid) "
        "VALUES(?,?,1,?,?,'test',?)",
        (talker, int(mine), body, when,
         con.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0])).lastrowid
    con.commit()
    return mid


def main():
    with tempfile.TemporaryDirectory(prefix="auto_reply_voice_") as tmp:
        con = store.connect(Path(tmp) / "mirror.sqlite")
        add(con, "friend_a", False, "秘密合同甲乙", 1000)
        add(con, "friend_a", True, "好的", 1001)
        add(con, "friend_b", False, "私人医疗安排", 2000)
        add(con, "friend_b", True, "好的", 2001)
        add(con, "small_account", False, "有空吗", 3000)
        add(con, "small_account", True, "可以", 3001)
        add(con, "small_account", False, "今天有空吗", 4000)
        auto_in = add(con, "friend_a", False, "自动回复测试", 5000)
        auto_out = add(con, "friend_a", True, "AI自测文字", 5001)
        control.record_decision(con, auto_in, "default", 0, [{"text": "AI自测文字"}])
        control.set_decision_status(con, auto_in, "sent")

        built = voice.rebuild(con)
        assert built["excluded_auto_messages"] == 1
        assert not con.execute(
            "SELECT 1 FROM voice_examples WHERE reply_id=?", (auto_out,)).fetchone()
        assert not con.execute(
            "SELECT 1 FROM voice_profiles WHERE scope='talker:small_account'").fetchone()
        guidance = voice.guidance(con, "small_account", "今天有空吗")
        assert "样本不足" in guidance["profile"]
        assert "好的" in guidance["examples"]
        assert "秘密合同甲乙" not in guidance["examples"]
        assert "私人医疗安排" not in guidance["examples"]
        assert "AI自测文字" not in guidance["examples"]

        captured = []
        def fake_chat(_cfg, messages, **_):
            captured.extend(messages)
            return {"analysis": "test", "candidates": [{"text": "可以"}]}
        with patch.object(suggest.llm, "chat_json", side_effect=fake_chat):
            result = suggest.suggest({"llm": {"redact_sensitive": True}},
                                     con, "small_account")
        assert result["candidates"][0]["text"] == "可以"
        prompt = captured[-1]["content"]
        assert "本人表达习惯" in prompt and "样本不足" in prompt
        assert "秘密合同甲乙" not in prompt and "私人医疗安排" not in prompt
        assert "AI自测文字" not in prompt

        ranked = decision.select_candidate({}, con, "small_account", [
            {"text": "非常感谢您的理解与支持，祝您生活愉快。"}, {"text": "可以"}])
        assert ranked.index == 1
        assert decision.select_candidate({}, con, "small_account", [
            {"text": "行"}, {"text": "可以"}]).index == 0
        assert con.execute("SELECT COUNT(*) FROM voice_examples").fetchone()[0] >= 2

        for i in range(20):
            add(con, "friend_b", False, f"约第{i}次", 6000 + i * 10)
            add(con, "friend_b", True, "哈哈可以呀", 6001 + i * 10)
        voice.rebuild(con)
        personal = voice.guidance(con, "friend_b", "约第20次")
        assert "对当前联系人已有的表达习惯" in personal["profile"]
        assert "当前联系人中本人真实回复过的情境" in personal["examples"]
        con.close()
    with tempfile.TemporaryDirectory(prefix="auto_reply_voice_order_") as tmp:
        con = store.connect(Path(tmp) / "mirror.sqlite")
        add(con, "z_old", True, "很久以前的一条长消息", 100)
        add(con, "a_new", True, "好", 200)
        with patch.object(voice, "GLOBAL_SAMPLE_LIMIT", 1):
            voice.rebuild(con)
        row = con.execute("SELECT profile_json FROM voice_profiles WHERE scope='global'").fetchone()
        assert json.loads(row[0])["median_length"] == 1, "全局样本必须按时间取最近消息"
        con.close()
    print("[+] 本人风格、联系人差异、稀疏回退、AI 排除与跨会话隐私边界通过")


if __name__ == "__main__":
    main()
