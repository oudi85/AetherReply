"""A draft-mode worker run saves candidates and never reaches the sender."""

import copy
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply import cli
from auto_reply.config import DEFAULTS
from auto_reply.core import control, dashboard, events, store
from auto_reply.wx import uia_send


def run_once(*, switch_during_generation: bool):
    with tempfile.TemporaryDirectory(prefix="auto_reply_draft_") as tmp:
        db = Path(tmp) / "mirror.sqlite"
        con = store.connect(db)
        now = int(time.time())
        con.execute("INSERT INTO contacts(wxid,nickname) VALUES('wxid_test','Test')")
        mid = con.execute(
            "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,"
            "src_db,src_rowid) VALUES('wxid_test',0,1,'hello',?,'test',1)",
            (now,)).lastrowid
        con.execute(
            "INSERT INTO incoming_events(message_id,talker,seen_at) VALUES(?,?,?)",
            (mid, "wxid_test", now - 10))
        con.commit()
        control.set_mode(con, "auto" if switch_during_generation else "draft")
        con.close()

        cfg = copy.deepcopy(DEFAULTS)
        cfg["llm"]["api_key"] = "test-only"
        cfg["auto_send"].update(enabled=switch_during_generation,
                                allow_talkers=["wxid_test"])
        cfg["sync"].update(poll_seconds=0, debounce_seconds=2)

        def generate(_cfg, active_con, _talker):
            if switch_during_generation:
                control.set_mode(active_con, "draft")
            return {"analysis": "test", "candidates": [{"text": "reply"}]}

        with (patch.object(cli, "_mirror", side_effect=lambda *_a, **_k: store.connect(db)),
              patch.object(cli.sync, "sync_once", return_value={"stale": []}),
              patch.object(cli.cfgmod, "load_config", return_value=cfg),
              patch.object(cli.suggest, "suggest", side_effect=generate),
              patch.object(uia_send, "send_once", side_effect=AssertionError("不得发送")),
              patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt)):
            cli.cmd_watch_auto(cfg)

        con = store.connect(db)
        assert con.execute("SELECT status FROM incoming_events WHERE message_id=?",
                           (mid,)).fetchone()[0] == "drafted"
        decision = con.execute(
            "SELECT status,candidates_json FROM reply_decisions WHERE message_id=?",
            (mid,)).fetchone()
        assert decision["status"] == "drafted"
        assert '"reply"' in decision["candidates_json"]
        assert not events.ready_batches(con, now=now + 100)
        snap = dashboard.load_snapshot(con, ["wxid_test"], selected="wxid_test", now=now)
        assert snap["mode"] == "draft"
        assert snap["counts"]["drafted"] == 1
        card = next(item["decision"] for item in snap["chat"]["items"]
                    if item["decision"] is not None)
        assert card["bucket"] == "drafted" and card["candidates"] == ["reply"]
        con.close()


if __name__ == "__main__":
    run_once(switch_during_generation=False)
    run_once(switch_during_generation=True)
    print("[+] 仅生成模式与生成中切换均不调用发送器")
