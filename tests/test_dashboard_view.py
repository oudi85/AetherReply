"""控制台统计口径、聊天视图数据、同步心跳、无黑窗进程查询、快捷键边界与演示模式隔离。不发送任何消息。"""

import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.config import PROJECT_ROOT
from auto_reply.core import control, dashboard, store


def add_event(con, talker, status, attempts=0, sending_at=None, content="hi"):
    rowid = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0]
    mid = con.execute(
        "INSERT INTO messages(talker,is_sender,msg_type,content,create_time,src_db,src_rowid) "
        "VALUES(?,0,1,?,?,'test',?)", (talker, content, int(time.time()), rowid)).lastrowid
    con.execute("INSERT INTO incoming_events(message_id,talker,seen_at,status,attempts) "
                "VALUES(?,?,?,?,?)", (mid, talker, int(time.time()), status, attempts))
    if sending_at is not None:
        control.record_decision(con, mid, "default", 0, [{"text": "ok"}])
        con.execute("UPDATE reply_decisions SET status='sending',updated_at=? "
                    "WHERE message_id=?", (sending_at, mid))
    con.commit()
    return mid


def test_counts_and_health(root: Path):
    con = store.connect(root / "mirror.sqlite")
    now = int(time.time())
    con.execute("INSERT INTO contacts(wxid,nickname) VALUES('wxid_a','A'),('wxid_b','B')")
    add_event(con, "wxid_a", "pending")                    # 待处理
    add_event(con, "wxid_a", "pending", attempts=2)        # 重试中，不重复计入待处理
    add_event(con, "wxid_a", "skipped")                    # 正常跳过，不算失败
    add_event(con, "wxid_a", "done")
    add_event(con, "wxid_a", "sent")
    add_event(con, "wxid_a", "uncertain")                  # 待核对
    add_event(con, "wxid_b", "sending", sending_at=now - 600)  # 中断的发送 → 待核对
    add_event(con, "wxid_b", "sending")                    # 同批较早一条，按会话判定
    add_event(con, "wxid_c", "uncertain")                  # 不在名单，不计
    snap = dashboard.load_snapshot(con, ["wxid_a", "wxid_b"], now=now)
    assert snap["counts"] == {"waiting": 1, "retrying": 1, "drafted": 0, "sent": 1,
                              "review": 3}, snap["counts"]
    assert sum(snap["counts"].values()) == 6
    assert {s["talker"]: s["badge"] for s in snap["sessions"]} == {
        "wxid_a": "review", "wxid_b": "review"}
    assert snap["chat"] is None and snap["mode"] == "auto"

    fresh = add_event(con, "wxid_a", "sending", sending_at=now - 5)
    snap = dashboard.load_snapshot(con, ["wxid_a"], selected="wxid_a", now=now)
    assert snap["counts"]["review"] == 1, "刚开始的发送是瞬时状态，不算待核对"
    chat = snap["chat"]
    assert chat["talker"] == "wxid_a" and chat["name"] == "A"
    item = next(i for i in chat["items"] if i["id"] == fresh)
    assert item["decision"]["bucket"] == "sending" and item["decision"]["candidates"] == ["ok"]
    assert chat["pending"] is None, "已有候选的来讯不再显示生成中"
    assert dashboard.load_chat(con, "wxid_none", now=now)["items"] == []

    # 心跳：只有成功同步才刷新；之后的问题会盖过旧的成功，成功又会清除旧问题
    assert control.worker_health(con) == {"sync_ok_at": None, "issue": None}
    control.record_sync_ok(con, now=now - 10)

    def view(mode="auto", running=True, enabled=True, at=now):
        return dashboard.health_view(control.worker_health(con), running=running, mode=mode,
                                     enabled=enabled, now=at)
    v = view()
    assert v["state"] == ("ok", "运行中") and v["sync"][0] == "ok" and v["issue"] is None
    assert view("paused")["state"] == ("warn", "已暂停")
    assert view(enabled=False)["state"][0] == "warn"
    assert view("draft", enabled=False)["state"] == ("ok", "运行中"), "仅生成不需要开启自动发送"
    control.record_worker_issue(con, "同步未完成：message_0.db（缺少密钥）", now=now - 5)
    assert view()["issue"][0].startswith("同步未完成")
    control.record_sync_ok(con, now=now)
    assert view()["issue"] is None
    stale = view(at=now + dashboard.SYNC_STALE_SECONDS + 1)
    assert stale["sync"][0] == "warn", "进程活着但长时间未成功同步时不能显示正常"
    assert view(running=False)["state"][0] == "bad"

    control.record_generation_failure(con, fresh, "TimeoutError: x")
    row = con.execute("SELECT status,error FROM reply_decisions WHERE message_id=?",
                      (fresh,)).fetchone()
    assert row["status"] == "generate_failed" and row["error"] == "TimeoutError: x"
    con.close()


def test_live_backend_guards():
    """LiveBackend writes modes and thinking only to the supplied test files."""
    from auto_reply.ui import LiveBackend
    from auto_reply.ui import backend as live_module
    from auto_reply.core import preferences, store

    assert LiveBackend.capabilities == {"draft_mode": True, "thinking_toggle": True}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mirror.sqlite"
        cfg = Path(tmp) / "config.toml"
        store.connect(db).close()
        cfg.write_text('[llm]\nthinking = true\n[auto_send]\nallow_talkers = []\n',
                       encoding="utf-8")
        old_db, old_cfg = live_module.DB, preferences.CONFIG_PATH
        live_module.DB, preferences.CONFIG_PATH = db, cfg
        try:
            backend = LiveBackend()
            backend.set_mode("draft")
            with closing(backend.connect()) as con:
                assert backend.current_mode(con) == "draft"
            backend.set_mode("paused")
            with closing(backend.connect()) as con:
                assert control.resume_mode(con) == "draft"
            backend.set_mode("draft")
            backend.set_thinking(False)
            import tomllib
            assert tomllib.loads(cfg.read_text(encoding="utf-8"))["llm"]["thinking"] is False
            try:
                backend.set_thinking("false")
            except ValueError:
                pass
            else:
                raise AssertionError("思考开关只接受布尔值")
        finally:
            live_module.DB, preferences.CONFIG_PATH = old_db, old_cfg


def test_pid_lookup_spawns_nothing():
    from auto_reply.wx import paths
    original = subprocess.Popen

    def forbidden(*args, **kwargs):
        raise AssertionError(f"不应启动子进程：{args[0]}")
    subprocess.Popen = forbidden
    try:
        assert isinstance(paths.list_wechat_pids(), list)
    finally:
        subprocess.Popen = original


def _pump(root, seconds=1.0):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(.02)


def test_demo_ui_isolated():
    import sqlite3
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    from auto_reply.ui import Dashboard
    from auto_reply.ui.demo import DemoBackend

    # 真实镜像库会被后台任务持续写入，不能用 mtime 判断；改为记录演示期间打开过的所有库
    opened, real_connect = [], sqlite3.connect
    sqlite3.connect = lambda path, *a, **k: opened.append(Path(path)) or real_connect(path, *a, **k)
    config_file = PROJECT_ROOT / "config.toml"
    before = config_file.stat().st_mtime_ns if config_file.exists() else None
    backend = DemoBackend()
    assert PROJECT_ROOT not in backend.db_path.parents
    root = tk.Tk()
    root.withdraw()
    asked = []
    orig = (messagebox.askyesno, messagebox.showerror, messagebox.showinfo)
    messagebox.askyesno = lambda *a, **k: asked.append(a) or True
    messagebox.showerror = messagebox.showinfo = lambda *a, **k: asked.append(a)
    try:
        d = Dashboard(root, backend)
        _pump(root)
        assert d.tiles["retrying"].value == 2 and d.tiles["review"].value == 1
        assert d.tiles["drafted"].value == 1
        assert not d.mode_seg.disabled and d.think_switch.enabled
        # 默认选中最近的会话，并在聊天视图里显示
        assert d.selected == d.sessions.rows[0]["talker"]
        assert d.chat_view.chat and d.chat_view.chat["talker"] == d.selected
        assert d.sticker_btn.winfo_ismapped(), "主界面应直接显示表情包标注入口"

        # 主界面入口直接打开可看图、可人工分类的标注窗口；演示数据不触碰真实库。
        d.show_sticker_labeler()
        _pump(root, .3)
        labeler = d.sticker_win
        assert labeler and labeler.winfo_exists()
        d.show_sticker_labeler()
        assert d.sticker_win is labeler, "再次点击应聚焦已有窗口"

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(labeler))
        sticker_lists = [w for w in widgets if isinstance(w, ttk.Treeview)
                         and len(w.get_children()) == 3]
        assert sticker_lists, "标注窗口应显示三张带缩略图的演示表情"
        assert all(sticker_lists[0].item(item, "image")
                   for item in sticker_lists[0].get_children())
        assert any(isinstance(w, tk.Label) and w.cget("image") for w in widgets), \
            "选择表情后应显示图片"
        answers = iter(("庆祝", "朋友分享好消息时"))
        old_ask = simpledialog.askstring
        simpledialog.askstring = lambda *a, **k: next(answers)
        try:
            new_btn = next(w for w in widgets if isinstance(w, ttk.Button)
                           and w.cget("text") == "新建分类")
            new_btn.invoke()
        finally:
            simpledialog.askstring = old_ask
        note_entry = next(w for w in widgets if isinstance(w, ttk.Entry)
                          and not isinstance(w, ttk.Combobox))
        note_entry.insert(0, "收到好消息时表示祝贺")
        save_btn = next(w for w in widgets if isinstance(w, ttk.Button)
                        and w.cget("text") == "保存标注")
        save_btn.invoke()
        with closing(backend.connect()) as demo_con:
            assert demo_con.execute("SELECT COUNT(*) FROM sticker_assignments").fetchone()[0] == 1

        # 快捷键边界：窗口级（root 及其所有子控件都会触发）不绑定 Space/Delete，
        # Delete 只绑定在名单上，搜索框里的空格和 Delete 只是普通输入
        window_keys = set(root.bind()) | set(root.bind_class("all"))
        assert not {k for k in window_keys if "space" in k.lower() or "delete" in k.lower()}, window_keys
        assert not [k for k in d.search.entry.bind()
                    if "delete" in k.lower() or "space" in k.lower()]
        assert "<Key-Delete>" in d.sessions.bind()
        assert any("Control" in k and k.lower().endswith("p>") for k in root.bind())

        # 搜索只读联系人表，结果里区分已加入
        d.search.set("示例")
        _pump(root, .8)
        assert d.sessions.mode == "results" and d.results
        d.clear_search()
        _pump(root, .4)
        assert d.sessions.mode == "sessions"

        d.select("demo_wang")
        _pump(root, .6)
        assert d.chat_view.chat["talker"] == "demo_wang"
        assert any(i["decision"] and i["decision"]["bucket"] == "drafted"
                   for i in d.chat_view.chat["items"])

        d.remove_contact("demo_lin")
        _pump(root, .6)
        assert asked and "demo_lin" not in backend.allowlist()
        # 模式切换与暂停只写演示库
        d.change_mode("draft")
        _pump(root, .4)
        with closing(backend.connect()) as con:
            assert backend.current_mode(con) == "draft"
        d.toggle_pause()
        _pump(root, .6)
        with closing(backend.connect()) as con:
            assert control.is_paused(con) and backend.current_mode(con) == "paused"
        d.toggle_pause()
        _pump(root, .6)
        with closing(backend.connect()) as con:
            assert backend.current_mode(con) == "draft", "恢复时回到暂停前的模式"
        d.change_effort("max")
        d.change_thinking(False)
        _pump(root, .4)
        assert backend.config()["llm"]["reasoning_effort"] == "max"
        assert backend.config()["llm"]["thinking"] is False
        assert d.effort_seg.inactive
        assert backend.start_worker()  # 演示模式只返回提示，不调用计划任务
        d.close()
    finally:
        if "d" in locals() and not d.closed:
            d.close()
        if "d" in locals():
            d.pool.shutdown(wait=True)
        messagebox.askyesno, messagebox.showerror, messagebox.showinfo = orig
        sqlite3.connect = real_connect
        backend.close()
    after = config_file.stat().st_mtime_ns if config_file.exists() else None
    assert before == after, "演示模式不得修改真实配置"
    assert opened and all(PROJECT_ROOT.resolve() not in p.resolve().parents for p in opened), opened
    assert "auto_reply.wx.uia_send" not in sys.modules, "控制台不应加载发送模块"


def test_ui_without_capabilities():
    """后台尚未支持时：仅生成置灰、深度思考开关只读；即使被调用也会回退，不改变模式。"""
    import tkinter as tk
    from auto_reply.ui import Dashboard
    from auto_reply.ui.demo import DemoBackend

    class Limited(DemoBackend):
        capabilities = {"draft_mode": False, "thinking_toggle": False}

        def set_mode(self, mode):
            if mode == "draft":
                raise NotImplementedError("仅生成模式待后台接入")
            super().set_mode(mode)

        def set_thinking(self, enabled):
            raise NotImplementedError("深度思考开关待后台接入")

    backend = Limited()
    root = tk.Tk()
    root.withdraw()
    try:
        d = Dashboard(root, backend)
        _pump(root)
        assert "draft" in d.mode_seg.disabled and not d.think_switch.enabled
        d.change_mode("draft")
        deadline = time.time() + 3
        while time.time() < deadline and (d.mode != "auto" or d.mode_seg.value != "auto"):
            _pump(root, .1)
        with closing(backend.connect()) as con:
            assert backend.current_mode(con) == "auto"
        assert d.mode == "auto" and d.mode_seg.value == "auto", "失败后界面回到原模式"
        before = backend.config()["llm"]["thinking"]
        d.change_thinking(not before)
        deadline = time.time() + 3
        while time.time() < deadline and d._think_pending:
            _pump(root, .1)
        assert d.think_switch.value == before and backend.config()["llm"]["thinking"] == before
        d.close()
    finally:
        if "d" in locals() and not d.closed:
            d.close()
        if "d" in locals():
            d.pool.shutdown(wait=True)
        backend.close()


def main():
    with tempfile.TemporaryDirectory(prefix="auto_reply_view_") as tmp:
        test_counts_and_health(Path(tmp))
    test_live_backend_guards()
    test_pid_lookup_spawns_nothing()
    test_demo_ui_isolated()
    test_ui_without_capabilities()
    print("[+] 控制台统计、聊天数据、心跳、实盘适配、快捷键与演示隔离测试通过")


if __name__ == "__main__":
    main()
