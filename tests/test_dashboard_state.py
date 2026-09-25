"""The UI and worker share pause state and reserve a safe Jev boundary."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import control, decision, preferences, store


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = store.connect(root / "mirror.sqlite")
        assert not control.is_paused(con)
        control.set_paused(con, True)
        assert control.is_paused(con)
        control.set_paused(con, False)
        assert not control.is_paused(con)
        control.set_mode(con, "draft")
        assert control.current_mode(con) == "draft"
        control.set_paused(con, True)
        assert control.current_mode(con) == "paused"
        assert control.resume_mode(con) == "draft"
        control.set_paused(con, False)
        assert control.current_mode(con) == "draft"
        con.execute("INSERT INTO messages(talker,is_sender,msg_type,content,"
                    "create_time,src_db,src_rowid) VALUES('wxid_test',0,1,'hi',1,'test',1)")
        con.commit()
        control.record_decision(con, 1, "default", 0, [{"text": "hello"}], "greeting")
        row = con.execute("SELECT status,candidates_json FROM reply_decisions "
                          "WHERE message_id=1").fetchone()
        assert row["status"] == "ready"
        assert json.loads(row["candidates_json"])[0]["text"] == "hello"
        control.set_decision_status(con, 1, "sent")
        assert con.execute("SELECT status FROM reply_decisions WHERE message_id=1").fetchone()[0] == "sent"
        assert decision.select_candidate({}, con, "wxid_test", [{"text": "hello"}]).index == 0
        try:
            decision.select_candidate({"decision": {"provider": "jev"}}, con,
                                      "wxid_test", [{"text": "hello"}])
        except decision.DecisionUnavailable:
            pass
        else:
            raise AssertionError("未配置的 Jev 不得进入发送路径")
        con.close()

        cfg = root / "config.toml"
        cfg.write_text('[llm]\nreasoning_effort = "high"\nthinking = true\n'
                       '[auto_send]\nallow_talkers = []\n', encoding="utf-8")
        old_path = preferences.CONFIG_PATH
        preferences.CONFIG_PATH = cfg
        try:
            preferences.set_reasoning_effort("max")
            preferences.set_thinking(False)
            preferences.set_allowlist(["wxid_test"])
        finally:
            preferences.CONFIG_PATH = old_path
        import tomllib
        result = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert result["llm"]["reasoning_effort"] == "max"
        assert result["llm"]["thinking"] is False
        assert result["auto_send"]["allow_talkers"] == ["wxid_test"]
    print("[+] 仪表盘暂停、配置和 Jev 预留接口测试通过")


if __name__ == "__main__":
    main()
