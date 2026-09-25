"""Send one manually labelled, calibrated favorite to File Transfer Assistant.

This is a one-shot physical send. An uncertain result is never retried.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply import config
from auto_reply.core import store
from auto_reply.wx import sticker_send, sync, uia_send


def main():
    cfg = config.load_config()
    sync.sync_once(cfg, verbose=False)
    con = store.connect(config.data_dir(cfg) / "auto_reply.db")
    try:
        busy = con.execute("SELECT 1 FROM incoming_events WHERE status='sending' "
                           "LIMIT 1").fetchone()
        if busy:
            raise RuntimeError("自动回复正在发送，稍后再执行自测")
        calibration = con.execute(
            "SELECT dll_sha256,visible_count FROM sticker_calibration WHERE id=1"
        ).fetchone()
        favorites = sticker_send.calibrated_favorites(cfg, con)
        if calibration is None or not favorites:
            raise RuntimeError("收藏表情校准已失效")
        labelled = {row[0] for row in con.execute(
            "SELECT DISTINCT md5 FROM sticker_assignments")}
        choices = [md5 for md5 in favorites if md5 in labelled]
        if not choices:
            raise RuntimeError("没有已人工分类且校准有效的表情")
        md5 = choices[0]
        pid, _ = uia_send._window()
        if sticker_send._dll_hash(pid) != calibration[0]:
            raise RuntimeError("微信版本与表情校准时不同")
        print("即将向文件传输助手发送一张已人工分类的收藏表情；只尝试一次。", flush=True)
        status, _ = sticker_send._click_and_confirm(
            cfg, con, "filehelper", "文件传输助手", favorites.index(md5), md5,
            calibration[1])
        print("表情发送及本地消息核对：" + status, flush=True)
        if status != "confirmed":
            raise RuntimeError("发送结果未确认，不会重试")
    finally:
        con.close()


if __name__ == "__main__":
    main()
