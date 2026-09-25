"""Manual sticker approval and two-stage selection stay inside calibrated favorites."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import reply_plan, stickers, store, suggest
from auto_reply.wx import sticker_send


def main():
    with tempfile.TemporaryDirectory() as root:
        con = store.connect(Path(root) / "mirror.sqlite")
        ids = ["a" * 32, "b" * 32, "c" * 32]
        for order, md5 in enumerate(ids):
            con.execute("INSERT INTO stickers(md5,favorite_order,updated_at) "
                        "VALUES(?,?,1)", (md5, order))
        con.commit()
        stickers.set_tags(con, ids[0], ["视觉模型猜的开心"])
        stickers.save_category(con, "安慰", "对方难过时轻轻安慰，不适合严肃事务")
        stickers.assign(con, ids[1], "安慰", "摸摸头，适合朋友低落时")
        stickers.assign(con, ids[2], "安慰", "抱抱，适合关系亲近的人")
        cfg = {"llm": {"thinking": True}, "reply_plan": {"enabled": True,
               "max_parts": 3, "allow_stickers": True}}
        with patch.object(sticker_send, "calibrated_favorites", return_value=ids[:2]):
            assert stickers.available_categories(cfg, con) == {
                "安慰": "对方难过时轻轻安慰，不适合严肃事务"}
            assert stickers.available_for_reply(cfg, con) == {ids[1]: "摸摸头，适合朋友低落时"}
            assert stickers.options_in_category(cfg, con, "安慰") == [
                {"md5": ids[1], "description": "摸摸头，适合朋友低落时"}]
            assert ids[0] not in stickers.available_for_reply(cfg, con)

            raw = {"parts": [{"type": "text", "text": "抱抱"},
                             {"type": "sticker_category", "category": "安慰"}]}
            candidate = reply_plan.normalize_candidates(
                cfg, [raw], allowed_categories={"安慰"})[0]
            try:
                reply_plan.prepare(con, 1, candidate["parts"])
            except ValueError:
                pass
            else:
                raise AssertionError("unresolved category entered send ledger")
            with patch.object(stickers.llm, "chat_json", return_value={"md5": ids[1]}) as model:
                resolved = suggest.resolve_selected(cfg, con, candidate, "今天有点难受")
                assert model.call_args.args[0]["llm"]["thinking"] is False
            assert resolved["parts"][-1] == {"type": "sticker", "md5": ids[1]}
            with patch.object(stickers.llm, "chat_json", return_value={"md5": None}):
                assert len(suggest.resolve_selected(cfg, con, candidate,
                                                    "今天有点难受")["parts"]) == 1
            with patch.object(stickers.llm, "chat_json", return_value={"md5": ids[2]}):
                try:
                    suggest.resolve_selected(cfg, con, candidate, "今天有点难受")
                except ValueError:
                    pass
                else:
                    raise AssertionError("uncalibrated sticker selected")
            stickers.unassign(con, ids[1], "安慰")
            assert not stickers.available_categories(cfg, con)
        expected = [ids[2], ids[1]]
        con.execute("INSERT INTO sticker_calibration(id,dll_sha256,favorites_json,"
                    "visible_count,calibrated_at) VALUES(1,'test',?,?,1)",
                    (json.dumps({"items": expected, "source_stamp": [1]}), 2))
        con.commit()
        with (patch.object(sticker_send, "_favorite_stamp", return_value=[2]),
              patch.object(sticker_send.stickers, "scan") as scan):
            assert sticker_send.calibrated_favorites(cfg, con) == expected
            scan.assert_called_once()
        record = json.loads(con.execute(
            "SELECT favorites_json FROM sticker_calibration WHERE id=1").fetchone()[0])
        assert record["source_stamp"] == [2]
        con.execute("UPDATE stickers SET favorite_order=4 WHERE md5=?", (ids[0],))
        con.commit()
        with patch.object(sticker_send, "_favorite_stamp", return_value=[2]):
            assert sticker_send.calibrated_favorites(cfg, con) == []
        con.close()
    print("[+] 人工分类、校准边界、DS 分类内选择与弃选通过")


if __name__ == "__main__":
    main()
