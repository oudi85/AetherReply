"""控制台演示模式：临时数据库 + 内存配置，完全不接触真实配置、镜像库、计划任务与发送入口。

所有联系人和对话都是虚构的。启动：pythonw tools/run_dashboard.py --demo
"""

import copy
import hashlib
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageDraw

from ..config import DEFAULTS
from ..core import control, store

DEMO_CONTACTS = [
    ("demo_lin", "示例·林同学", ""), ("demo_jie", "", "示例·阿杰"),
    ("demo_wang", "示例·王老师", ""), ("demo_zhou", "", "示例·小周"),
    ("demo_mei", "示例·美美", ""), ("demo_chen_1", "", "示例·小陈"),
    ("demo_chen_2", "", "示例·小陈"), ("demo_he", "示例·何经理", ""),
]
DEMO_ALLOWED = ["demo_lin", "demo_jie", "demo_wang", "demo_zhou", "demo_mei"]


def _seed(path: Path) -> None:
    now = int(time.time())
    con = store.connect(path)
    con.executemany("INSERT INTO contacts(wxid,remark,nickname) VALUES(?,?,?)",
                    DEMO_CONTACTS)
    rowid = 0

    def msg(talker, sender, content, ago, msg_type=1):
        nonlocal rowid
        rowid += 1
        return con.execute(
            "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,"
            "src_db,src_rowid) VALUES(?,?,?,?,?,'demo',?)",
            (talker, sender, msg_type, content, now - ago, rowid)).lastrowid

    def event(mid, talker, status, attempts=0, retry_in=0):
        con.execute("INSERT INTO incoming_events(message_id,talker,seen_at,status,"
                    "attempts,next_attempt_at) VALUES(?,?,?,?,?,?)",
                    (mid, talker, now, status, attempts, now + retry_in if retry_in else 0))

    def decide(mid, status, cands, selected=0, analysis="", error=""):
        control.record_decision(con, mid, "default", selected,
                                [{"text": c} for c in cands], analysis)
        if status != "ready":
            control.set_decision_status(con, mid, status, error)

    # 林同学：完整的自动回复往来 + 一条发送结果不明
    t = "demo_lin"
    msg(t, 1, "这周的读书会你去吗", 26 * 3600)
    msg(t, 0, "去的，上次那本还没看完哈哈", 26 * 3600 - 60)
    mid = msg(t, 0, "明天下午有空一起吃饭吗？", 3 * 3600)
    event(mid, t, "sent")
    decide(mid, "sent", ["有空呀，几点合适？", "可以，你想吃什么？", "明天下午我看下安排再回你"],
           analysis="对方发出邀约，语气轻松，适合直接答应并确认时间。")
    msg(t, 1, "有空呀，几点合适？", 3 * 3600 - 20)
    mid = msg(t, 0, "那我们五点半见？老地方", 3 * 3600 - 300)
    event(mid, t, "sent")
    decide(mid, "sent", ["好呀，五点半老地方见", "没问题～"], analysis="确认时间地点即可。")
    msg(t, 1, "好呀，五点半老地方见", 3 * 3600 - 320)
    msg(t, 0, "", 40 * 60, msg_type=47)
    mid = msg(t, 0, "对了，要不要叫上阿杰", 38 * 60)
    event(mid, t, "uncertain")
    decide(mid, "uncertain", ["可以啊，你问问他", "叫上吧，人多热闹"], selected=1,
           analysis="对方征求意见，表示赞同即可。", error="按下 Enter 后未能在消息库确认")

    # 阿杰：生成失败等待重试，最新一条正在生成
    t = "demo_jie"
    msg(t, 1, "周末有安排吗", 5 * 3600)
    mid = msg(t, 0, "周末去爬山？", 30 * 60)
    event(mid, t, "done")
    mid = msg(t, 0, "文件我发你邮箱了，看下", 12 * 60)
    event(mid, t, "pending", attempts=2, retry_in=55)
    decide(mid, "preflight", ["收到，我马上看"], error="发送前核对失败：微信主窗口未能置前")
    mid = msg(t, 0, "在吗", 25)
    event(mid, t, "pending")

    # 王老师：仅生成模式留下的候选，还没有回复
    t = "demo_wang"
    msg(t, 0, "同学们，下周三的课调到周四上午第二节。", 2 * 86400)
    msg(t, 1, "收到，谢谢老师", 2 * 86400 - 120)
    mid = msg(t, 0, "你的报告初稿写得不错，结论部分再补充一些数据支撑，周五前发我。", 50 * 60)
    event(mid, t, "drafted")
    decide(mid, "drafted", ["好的老师，我周五前补充完发您", "谢谢老师，我这两天把数据补上",
                             "收到，我再完善一下结论部分"], analysis="师长语气，需礼貌确认并给出时间。")

    # 小周：生成失败
    t = "demo_zhou"
    msg(t, 1, "链接发你了", 6 * 86400)
    msg(t, 0, "", 6 * 86400 - 100, msg_type=49)
    mid = msg(t, 0, "那个活动你报名了没", 70 * 60)
    event(mid, t, "pending", attempts=1, retry_in=40)
    control.record_generation_failure(con, mid, "TimeoutError: 模型请求超时")

    # 美美：已发送的简短寒暄
    t = "demo_mei"
    mid = msg(t, 0, "生日快乐呀🎂", 9 * 3600)
    event(mid, t, "sent")
    decide(mid, "sent", ["谢谢美美！！", "哈哈谢谢～"], analysis="收到祝福，热情回应。")
    msg(t, 1, "谢谢美美！！", 9 * 3600 - 15)
    control.record_sync_ok(con)
    con.commit()
    con.close()


def _seed_stickers(path: Path) -> None:
    """Synthetic images keep labeler demos away from real WeChat data."""
    con = store.connect(path)
    samples = [("开心", "#FFE9A8", "smile"),
               ("安慰", "#DCD6FF", "heart"),
               ("疑问", "#C6F2E2", "question")]
    for order, (caption, color, face) in enumerate(samples):
        image = Image.new("RGB", (256, 256), color)
        draw = ImageDraw.Draw(image)
        draw.ellipse((35, 35, 221, 221), fill="#FFF8EB", outline="#E2BA77", width=4)
        if face == "smile":
            draw.ellipse((83, 105, 98, 120), fill="#624B3A")
            draw.ellipse((158, 105, 173, 120), fill="#624B3A")
            draw.arc((88, 95, 172, 184), 20, 160, fill="#D8796B", width=7)
        elif face == "heart":
            draw.polygon([(128, 180), (74, 125), (75, 100), (100, 85),
                          (128, 108), (156, 85), (181, 100), (182, 125)], fill="#F97895")
        else:
            draw.arc((100, 78, 165, 145), 170, 350, fill="#5B5FEF", width=9)
            draw.line((152, 124, 128, 151), fill="#5B5FEF", width=9)
            draw.ellipse((122, 174, 136, 188), fill="#5B5FEF")
        asset = path.parent / f"demo-sticker-{order}.png"
        image.save(asset)
        md5 = hashlib.md5(asset.read_bytes()).hexdigest()
        con.execute("INSERT INTO stickers(md5,caption,asset_path,favorite_order,updated_at) "
                    "VALUES(?,?,?,?,?)", (md5, f"示例·{caption}", str(asset), order, int(time.time())))
    con.commit()
    con.close()


class DemoBackend:
    demo = True
    capabilities = {"draft_mode": True, "thinking_toggle": True}

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="auto_reply_demo_")
        self.db_path = Path(self._tmp.name) / "demo.db"
        _seed(self.db_path)
        _seed_stickers(self.db_path)
        self._last_heartbeat = time.monotonic()
        self._mode = "auto"
        self._cfg = copy.deepcopy(DEFAULTS)
        self._cfg["llm"].update(model="deepseek-flash", reasoning_effort="high", thinking=True)
        self._cfg["auto_send"].update(enabled=True, allow_talkers=list(DEMO_ALLOWED))

    def config(self) -> dict:
        return copy.deepcopy(self._cfg)

    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=2)
        con.row_factory = sqlite3.Row
        if time.monotonic() - self._last_heartbeat > 20:
            self._last_heartbeat = time.monotonic()
            try:
                control.record_sync_ok(con)
            except Exception:
                con.close()
                raise
        return con

    def allowlist(self) -> list[str]:
        return list(self._cfg["auto_send"]["allow_talkers"])

    def set_allowlist(self, talkers: list[str]) -> None:
        clean = list(dict.fromkeys(t.strip() for t in talkers if t.strip()))
        if len(clean) > 30:
            raise ValueError("联系人列表无效")
        self._cfg["auto_send"]["allow_talkers"] = clean

    def set_effort(self, value: str) -> None:
        if value not in ("low", "high", "max"):
            raise ValueError("思考强度只能是 low、high 或 max")
        self._cfg["llm"]["reasoning_effort"] = value

    def set_thinking(self, enabled: bool) -> None:
        self._cfg["llm"]["thinking"] = bool(enabled)

    def current_mode(self, con) -> str:
        return "paused" if control.is_paused(con) else self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "draft", "paused"):
            raise ValueError(f"未知模式：{mode}")
        with closing(self.connect()) as con:
            control.set_paused(con, mode == "paused")
        if mode != "paused":
            self._mode = mode

    def worker_running(self) -> bool:
        return True

    def start_worker(self) -> str | None:
        return "演示模式不会启动后台任务。"

    def read_log(self, max_lines: int = 400) -> list[str]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        return [f"[{stamp}] 自动回复进程启动（演示）",
                "[i] 自动回复已启动；允许 5 个一对一会话；每 5s 检查一次。",
                "[!] 回复生成失败，稍后重试：TimeoutError",
                "[!] 发送前核对失败，稍后重试：微信主窗口未能置前",
                "[✓] 已确认发送"][-max_lines:]

    def open_log_folder(self) -> None:
        raise RuntimeError("演示模式没有日志文件夹。")

    def close(self) -> None:
        self._tmp.cleanup()
