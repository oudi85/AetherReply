"""控制台主窗口。界面与后台分进程：关闭界面不影响计划任务。

数据库、配置与进程查询都在单个后台线程里串行执行，Tk 主线程只负责绘制。
"""

import argparse
import ctypes
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from ctypes import wintypes
from tkinter import messagebox

from . import anim, fx
from . import theme as T
from .chat import SPARK, ChatView
from .sessions import SessionList, palette
from .widgets import (Button, Card, Chip, IconButton, PulseDot, SearchField, Segmented,
                      Switch, Tile, Toast, Tooltip)
from ..core import dashboard

TITLE = "AetherReply · 控制台"
MODES = (("auto", "自动发送", T.GREEN), ("draft", "仅生成", T.INDIGO), ("paused", "暂停", T.AMBER))
MODE_TOAST = {"auto": "已开启自动发送", "draft": "仅生成：只生成候选，不会发送",
              "paused": "已暂停：不会再发送新回复"}
EFFORTS = (("low", "轻量", T.TEXT), ("high", "标准", T.TEXT), ("max", "深度", T.TEXT))
PENDING_HINT = "待后台接入"


class StatusCapsule(tk.Canvas):
    def __init__(self, parent, *, bg):
        self.h = T.S(38)
        super().__init__(parent, bg=bg, height=self.h, highlightthickness=0, bd=0)
        self.rect = fx.RRect(self, (0, 0, 1, 1), self.h // 2, T.SURFACE, T.BORDER)
        self.dot = PulseDot(self, bg=T.SURFACE)
        self.dot_win = self.create_window(0, self.h // 2, window=self.dot, anchor="w")
        self.state_t = self.create_text(0, 0, font=T.font(12.5, "bold"), anchor="w")
        self.sep = self.create_text(0, 0, text="·", font=T.font(12), fill=T.SUBTLE, anchor="w")
        self.sync_t = self.create_text(0, 0, font=T.font(12), anchor="w", fill=T.MUTED)
        self.value = None

    def set(self, level, text, sync_level, sync_text):
        value = (level, text, sync_level, sync_text)
        if value == self.value:
            return
        self.value = value
        self.dot.set(level)
        pad, cy = T.S(12), self.h // 2
        fb, fn = T.font(12.5, "bold"), T.font(12)
        x = pad
        self.coords(self.dot_win, x - T.S(2), cy)
        x += T.S(20)
        self.itemconfigure(self.state_t, text=text, fill=T.TEXT if level == "ok" else T.LEVEL[level])
        self.coords(self.state_t, x, cy - 1)
        x += fb.measure(text) + T.S(8)
        shown = "normal" if sync_text else "hidden"
        self.itemconfigure(self.sep, state=shown)
        self.itemconfigure(self.sync_t, state=shown, text=sync_text or "",
                           fill=T.MUTED if sync_level == "ok" else T.LEVEL[sync_level])
        self.coords(self.sep, x, cy - 1)
        x += fn.measure("·") + T.S(8)
        self.coords(self.sync_t, x, cy - 1)
        if sync_text:
            x += fn.measure(sync_text)
        w = x + pad + T.S(2)
        self.configure(width=w)
        self.rect.place((0, 0, w, self.h))


class Banner(tk.Canvas):
    """顶部问题提示条：高度补间展开/收起。"""

    def __init__(self, parent, *, bg):
        super().__init__(parent, bg=bg, height=1, highlightthickness=0, bd=0)
        self.bg, self.value, self.h, self.shown = bg, None, T.S(50), 0.0
        self.rect = fx.RRect(self, (0, 0, 1, 1), T.S(12), T.AMBER_SOFT)
        self.glyph = self.create_text(0, 0, text=T.I.WARN, font=T.icon(12.5), anchor="w")
        self.text = self.create_text(0, 0, font=T.font(12.5), anchor="w")
        self.side = self.create_text(0, 0, font=T.font(11.5), anchor="e")
        self.bind("<Configure>", lambda _: self._layout())

    def set(self, message, color=T.AMBER, soft=T.AMBER_SOFT, side=""):
        value = (message, color, soft, side)
        if value == self.value:
            return
        opening = bool(message) != bool(self.value and self.value[0])
        self.value = value
        if message:
            self.rect.set(fill=soft, outline=T.blend(soft, color, .25))
            for item in (self.glyph, self.text):
                self.itemconfigure(item, fill=color)
            self.itemconfigure(self.text, text=message)
            self.itemconfigure(self.side, text=side, fill=T.blend(color, soft, .35))
            self._layout()
        if opening:
            start, end = self.shown, 1.0 if message else 0.0

            def step(t):
                self.shown = start + (end - start) * t
                self.configure(height=max(1, int(self.h * self.shown)))
            anim.animate(("banner", id(self)), 300, step)

    def _layout(self):
        w = self.winfo_width()
        top = T.S(4)
        body = self.h - T.S(10)
        self.rect.place((T.S(12), top, w - T.S(12), top + body))
        cy = top + body // 2
        self.coords(self.glyph, T.S(28), cy)
        self.coords(self.text, T.S(52), cy - 1)
        self.coords(self.side, w - T.S(28), cy - 1)
        if self.value and self.value[0]:
            f = T.font(12.5)
            room = w - T.S(90) - T.font(11.5).measure(self.value[3] or "")
            self.itemconfigure(self.text, text=T.elide(self.value[0], f, room))


class Suggestions(tk.Canvas):
    """仅生成模式下最新一条来讯的候选；点击复制，由你在微信里自己发送。"""

    def __init__(self, parent, *, bg, on_copy):
        super().__init__(parent, bg=bg, height=1, highlightthickness=0, bd=0)
        self.bg, self.on_copy, self.items, self.shown = bg, on_copy, [], 0.0
        self.h = T.S(64)
        self.bind("<Configure>", lambda _: self._draw())

    def set(self, candidates):
        if candidates == self.items:
            return
        opening = bool(candidates) != bool(self.items)
        self.items = list(candidates)
        self._draw()
        if opening:
            start, end = self.shown, 1.0 if candidates else 0.0

            def step(t):
                self.shown = start + (end - start) * t
                self.configure(height=max(1, int(self.h * self.shown)))
            anim.animate(("sugg", id(self)), 280, step)

    def _draw(self):
        self.delete("all")
        if not self.items:
            return
        w = self.winfo_width()
        self.create_line(0, 0, w, 0, fill=T.blend(T.CHAT_BG, "#000000", .05))
        cy = self.h // 2
        x = T.S(22)
        head = self.create_text(x, cy, text=f"{SPARK} 建议回复", font=T.font(12, "bold"),
                                fill=T.INDIGO, anchor="w")
        x = self.bbox(head)[2] + T.S(14)
        f = T.font(12.5)
        ch = T.S(34)
        for i, text in enumerate(self.items):
            room = w - x - T.S(20)
            if room < T.S(80):
                break
            label = T.elide(text, f, min(room - T.S(28), T.S(280)))
            cw = f.measure(label) + T.S(28)
            tag = f"s{i}"
            rect = fx.RRect(self, (x, cy - ch / 2, x + cw, cy + ch / 2), ch / 2, T.SURFACE,
                            T.blend(T.SURFACE, T.INDIGO, .25), tags=(tag,))
            self.create_text(x + cw / 2, cy - 1, text=label, font=f, fill=T.TEXT, tags=(tag,))
            self.tag_bind(tag, "<Enter>", lambda _, r=rect: (
                r.set(fill=T.INDIGO_SOFT, outline=T.blend(T.SURFACE, T.INDIGO, .5)),
                self.configure(cursor="hand2")))
            self.tag_bind(tag, "<Leave>", lambda _, r=rect: (
                r.set(fill=T.SURFACE, outline=T.blend(T.SURFACE, T.INDIGO, .25)),
                self.configure(cursor="")))
            self.tag_bind(tag, "<ButtonRelease-1>", lambda _, t=text: self.on_copy(t))
            x += cw + T.S(8)


class Dashboard:
    def __init__(self, root: tk.Tk, backend):
        self.root, self.backend = root, backend
        fx.reset()
        T.init(root)
        anim.init(root)
        self.caps = dict(getattr(backend, "capabilities", {}))
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard")
        self.closed = False
        self.snapshot = None
        self.selected, self.selected_name = None, ""
        self.allowed, self.names = [], {}
        self.results, self.term = [], ""
        self.mode, self._resume_mode = "auto", "auto"
        self._loading = self._again = False
        self._refresh_job = self._search_job = None
        self._mode_pending = self._effort_pending = self._think_pending = False
        self.log_win = None
        self.sticker_win = self.sticker_con = None

        title = TITLE + ("（演示模式）" if backend.demo else "")
        root.title(title)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(T.S(1360), int(sw * .92)), min(T.S(860), int(sh * .88))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - T.S(16))}")
        root.minsize(min(T.S(1100), w), min(T.S(660), h))
        root.configure(bg=T.BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._icons = [fx.logo_photo(64), fx.logo_photo(32)]
        root.iconphoto(True, *self._icons)
        fx.set_caption(root, T.BG)
        self.toast = Toast(root)
        self._build()
        self._bind_keys()
        self.refresh()

    # ---------- 后台线程 ----------
    def _submit(self, fn, done=None, fail=None):
        future = self.pool.submit(fn)

        def poll():
            if self.closed:
                return
            if not future.done():
                self.root.after(30, poll)
                return
            try:
                result = future.result()
            except Exception as exc:
                (fail or self._action_failed)(exc)
                return
            if done:
                done(result)
        self.root.after(30, poll)

    def _action_failed(self, exc):
        messagebox.showerror("操作失败", f"{type(exc).__name__}: {exc}", parent=self.root)

    # ---------- 布局 ----------
    def _build(self):
        S = T.S
        top = tk.Frame(self.root, bg=T.BG)
        top.pack(fill="x", padx=S(26), pady=(S(16), S(4)))
        top.columnconfigure(0, weight=1, uniform="side")
        top.columnconfigure(2, weight=1, uniform="side")
        brand = tk.Frame(top, bg=T.BG)
        brand.grid(row=0, column=0, sticky="w")
        self._logo = fx.logo_photo(S(32), T.BG)
        tk.Label(brand, image=self._logo, bg=T.BG, bd=0).pack(side="left")
        tk.Label(brand, text="自动回复", font=T.font(17, "bold"), bg=T.BG, fg=T.TEXT).pack(
            side="left", padx=(S(8), 0))
        if self.backend.demo:
            Chip(brand, "演示", bg=T.BG, fg=T.INDIGO, soft=T.INDIGO_SOFT, bold=True,
                 font_px=11, height=22).pack(side="left", padx=(S(10), 0), pady=(S(2), 0))

        self.mode_seg = Segmented(top, MODES, self.change_mode, bg=T.BG, dots=True, height=40,
                                  seg_px=20, font_px=13)
        self.mode_seg.grid(row=0, column=1)
        if not self.caps.get("draft_mode"):
            # 后台还不认识仅生成模式；只改界面会让后台照常发送，因此必须禁用
            self.mode_seg.set_disabled(
                ["draft"], lambda _: self.toast.show(f"仅生成模式{PENDING_HINT}", "info"))

        right = tk.Frame(top, bg=T.BG)
        right.grid(row=0, column=2, sticky="e")
        self.log_btn = IconButton(right, T.I.LOG, self.show_log, bg=T.BG, tip="运行记录")
        self.log_btn.pack(side="right")
        self.refresh_btn = IconButton(right, T.I.REFRESH, self.manual_refresh, bg=T.BG,
                                      tip="刷新  F5")
        self.refresh_btn.pack(side="right", padx=(0, S(2)))
        self.capsule = StatusCapsule(right, bg=T.BG)
        self.capsule.pack(side="right", padx=(0, S(10)))
        self.capsule.set("warn", "读取中", "ok", "")

        self.banner = Banner(self.root, bg=T.BG)
        self.banner.pack(fill="x", padx=S(14))

        body = tk.Frame(self.root, bg=T.BG)
        body.pack(fill="both", expand=True, padx=S(14), pady=(0, S(12)))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_sessions(body)
        self._build_chat(body)
        self._build_inspector(body)

    def _build_sessions(self, parent):
        S = T.S
        card = Card(parent, width=S(330))
        card.grid(row=0, column=0, sticky="nsw")
        b = card.body
        head = tk.Frame(b, bg=T.SURFACE)
        head.pack(fill="x", padx=S(16), pady=(S(12), S(8)))
        self.list_title = tk.Label(head, text="会话", font=T.font(16, "bold"), bg=T.SURFACE,
                                   fg=T.TEXT)
        self.list_title.pack(side="left")
        self.count_label = tk.Label(head, text="", font=T.font(12), bg=T.SURFACE, fg=T.SUBTLE)
        self.count_label.pack(side="left", padx=(S(8), 0), pady=(S(3), 0))
        self.search = SearchField(b, bg=T.SURFACE, placeholder="搜索联系人，加入名单",
                                  on_change=self._search_changed, on_submit=self._search_submit,
                                  on_escape=self.clear_search)
        self.search.pack(fill="x", padx=S(10), pady=(0, S(6)))
        self.sessions = SessionList(b, bg=T.SURFACE, on_select=self.select,
                                    on_add=self.add_contact, on_remove=self.remove_contact)
        self.sessions.pack(fill="both", expand=True, pady=(0, S(6)))

    def _build_chat(self, parent):
        S = T.S
        head_h = S(66)
        card = Card(parent, fill=T.SURFACE, fill2=T.CHAT_BG, split=head_h)
        card.grid(row=0, column=1, sticky="nsew")
        b = card.body
        b.configure(bg=T.CHAT_BG)
        head = self.chat_head = tk.Frame(b, bg=T.SURFACE, height=head_h)
        head.pack(fill="x")
        head.pack_propagate(False)
        self.head_avatar = tk.Canvas(head, bg=T.SURFACE, width=S(40), height=S(40),
                                     highlightthickness=0, bd=0)
        self.head_avatar.pack(side="left", padx=(S(18), S(12)))
        names = tk.Frame(head, bg=T.SURFACE)
        names.pack(side="left", fill="y")
        self.head_name = tk.Label(names, text="", font=T.font(15, "bold"), bg=T.SURFACE,
                                  fg=T.TEXT, anchor="w")
        self.head_name.pack(side="left")
        self.head_chip = Chip(names, "", bg=T.SURFACE, dot=True, font_px=11, height=22)
        self.head_chip.pack(side="left", padx=(S(10), 0))
        self.head_btn = Button(head, "移出名单", self._head_action, bg=T.SURFACE, variant="soft",
                               height=32, font_px=12)
        self.head_hint = tk.Label(head, text="", font=T.font(11.5), bg=T.SURFACE, fg=T.SUBTLE)
        # 头部与下方聊天区的分隔线由卡片背景画出
        tk.Frame(b, bg=T.CHAT_BG, height=1).pack(fill="x")
        self.suggest = Suggestions(b, bg=T.CHAT_BG, on_copy=self.copy_text)
        self.suggest.pack(side="bottom", fill="x")
        self.chat_view = ChatView(b, on_copy=self.copy_text)
        self.chat_view.pack(fill="both", expand=True)
        self._paint_head()

    def _build_inspector(self, parent):
        S = T.S
        card = Card(parent, width=S(316))
        card.grid(row=0, column=2, sticky="nse")
        b = card.body
        wrap = tk.Frame(b, bg=T.SURFACE)
        wrap.pack(fill="both", expand=True, padx=S(16), pady=(S(12), S(14)))

        def section(text, top=0):
            tk.Label(wrap, text=text, font=T.font(12, "bold"), bg=T.SURFACE, fg=T.TEXT_2,
                     anchor="w").pack(fill="x", pady=(top, S(8)))
        section("处理概况")
        self.tiles = {}
        big = tk.Frame(wrap, bg=T.SURFACE)
        big.pack(fill="x")
        small = tk.Frame(wrap, bg=T.SURFACE)
        small.pack(fill="x", pady=(S(8), 0))
        for frame, keys, is_big in ((big, ("sent", "drafted"), True),
                                    (small, ("waiting", "retrying", "review"), False)):
            for i, key in enumerate(keys):
                fg, _, label = T.STATUS[key]
                tile = Tile(frame, label, fg, bg=T.SURFACE, big=is_big)
                tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else S(8), 0))
                frame.columnconfigure(i, weight=1, uniform="t")
                self.tiles[key] = tile

        section("回复生成", S(26))
        self.model_chip = Chip(wrap, "", bg=T.SURFACE, glyph=T.I.BOT, fg=T.TEXT_2,
                               soft=T.SURFACE_2, font_px=12, height=30, px=12)
        self.model_chip.pack(anchor="w")
        row = tk.Frame(wrap, bg=T.SURFACE)
        row.pack(fill="x", pady=(S(16), S(10)))
        tk.Label(row, text="深度思考", font=T.font(13), bg=T.SURFACE, fg=T.TEXT).pack(side="left")
        self.think_switch = Switch(row, self.change_thinking, bg=T.SURFACE, value=True)
        self.think_switch.pack(side="right")
        if not self.caps.get("thinking_toggle"):
            self.think_switch.set_enabled(False, lambda: self.toast.show(
                f"深度思考开关{PENDING_HINT}", "info"))
            Tooltip(self.think_switch, PENDING_HINT)
        self.effort_seg = Segmented(wrap, EFFORTS, self.change_effort, bg=T.SURFACE,
                                    width=250, height=36, font_px=12.5)
        self.effort_seg.pack(anchor="w")
        self.jev_chip = Chip(wrap, "", bg=T.SURFACE, glyph=T.I.WARN, font_px=11.5, height=28)
        self.jev_chip.pack(anchor="w", pady=(S(14), 0))

        foot = tk.Frame(wrap, bg=T.SURFACE)
        foot.pack(side="bottom", fill="x")
        self.sticker_btn = Button(foot, "表情包标注", self.show_sticker_labeler,
                                  bg=T.SURFACE, variant="indigo", height=40, width=250)
        self.sticker_btn.pack(fill="x", pady=(0, S(8)))
        self.start_btn = Button(foot, "启动后台", self.start_worker, bg=T.SURFACE,
                                variant="primary", icon=T.I.PLAY, height=40, width=250)
        self.foot = foot

    def _bind_keys(self):
        self.root.bind("<F5>", lambda _: self.manual_refresh())
        for key in ("<Control-f>", "<Control-F>"):
            self.root.bind(key, lambda _: (self.search.focus_input(), "break")[1])
        # 暂停/恢复必须带 Ctrl，避免在输入框里打字时误触；不绑定全局 Space/Delete
        for key in ("<Control-p>", "<Control-P>"):
            self.root.bind(key, lambda _: (self.toggle_pause(), "break")[1])
        self.root.bind("<Escape>", lambda _: self.clear_search() if self.term
                       else self.root.focus_set())

    # ---------- 刷新 ----------
    def manual_refresh(self):
        self.refresh_btn.spin()
        self.refresh()

    def refresh(self):
        if self.closed:
            return
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self._loading:
            self._again = True
            return
        self._loading = True
        selected = self.selected
        self._submit(lambda: self._load(selected), self._loaded, self._load_failed)

    def _load(self, selected) -> dict:  # 后台线程
        cfg = self.backend.config()
        allowed = list(cfg["auto_send"].get("allow_talkers", []))
        with closing(self.backend.connect()) as con:
            snap = dashboard.load_snapshot(con, allowed, selected=selected)
            snap["mode"] = self.backend.current_mode(con)
        llm = cfg.get("llm", {})
        snap.update(selected=selected, allowed=allowed, running=self.backend.worker_running(),
                    enabled=bool(cfg["auto_send"].get("enabled")),
                    effort=llm.get("reasoning_effort", "high"), thinking=bool(llm.get("thinking")),
                    model=llm.get("model", ""),
                    provider=cfg.get("decision", {}).get("provider", "default"))
        return snap

    def _schedule(self):
        self._loading = False
        if self.closed:
            return
        if self._again:
            self._again = False
            self.refresh()
        else:
            self._refresh_job = self.root.after(3000, self.refresh)

    def _load_failed(self, exc):
        self._schedule()
        self.banner.set(f"状态读取失败：{type(exc).__name__}: {exc}", T.RED, T.RED_SOFT)
        self.capsule.set("bad", "状态读取失败", "ok", "")

    def _loaded(self, snap):
        try:
            self._render(snap)
        except Exception as exc:
            self.banner.set(f"界面刷新失败：{type(exc).__name__}: {exc}", T.RED, T.RED_SOFT)
        finally:
            self._schedule()

    def _render(self, snap):
        first = self.snapshot is None
        self.snapshot = snap
        self.allowed = snap["allowed"]
        self.names.update({s["talker"]: s["name"] for s in snap["sessions"]})
        if not self._mode_pending:
            self.mode = snap["mode"]
            if self.mode != "paused":
                self._resume_mode = self.mode
            self.mode_seg.set(self.mode)
        view = dashboard.health_view(snap["health"], running=snap["running"], mode=snap["mode"],
                                     enabled=snap["enabled"], now=snap["now"])
        self.capsule.set(*view["state"], *view["sync"])
        if view["issue"]:
            message, at = view["issue"]
            self.banner.set(message, side=dashboard.format_age(snap["now"] - at))
        elif snap["mode"] == "auto" and not snap["enabled"]:
            self.banner.set("config.toml 中 auto_send.enabled 未开启，后台不会自动发送")
        else:
            self.banner.set(None)

        for key, tile in self.tiles.items():
            tile.set(snap["counts"].get(key, 0))
        self.model_chip.set(snap["model"] or "未配置模型", T.TEXT_2, T.SURFACE_2, T.I.BOT)
        if not self._think_pending:
            self.think_switch.set(snap["thinking"], animate=not first)
        self.effort_seg.set_inactive(not self.think_switch.value)
        if not self._effort_pending:
            self.effort_seg.set(snap["effort"])
        if snap["provider"] == "jev":
            self.jev_chip.set("Jev 接口未接通，发送前会停下", T.RED, T.RED_SOFT, T.I.WARN)
        else:
            self.jev_chip.set("")
        if snap["running"]:
            self.start_btn.pack_forget()
        elif not self.start_btn.winfo_ismapped():
            self.start_btn.set_enabled(True)
            self.start_btn.pack(fill="x")

        self._paint_count()
        if not self.term:
            self.sessions.show_sessions(snap["sessions"], snap["now"], self.allowed)
            if self.selected is None and snap["sessions"]:
                self.select(snap["sessions"][0]["talker"])
                return
        if snap["selected"] == self.selected:
            chat = snap["chat"]
            if chat is not None:
                self.selected_name = chat["name"]
            self.chat_view.show(chat, snap["mode"], snap["now"])
            self._paint_head()
            self._paint_suggestions(chat)

    def _paint_head(self):
        talker = self.selected
        cv = self.head_avatar
        cv.delete("all")
        self.head_btn.pack_forget()
        self.head_hint.pack_forget()
        if talker is None:
            self.head_name.configure(text="")
            self.head_chip.set("")
            return
        name = self.selected_name or self.names.get(talker, talker)
        d = T.S(40)
        self._head_img = fx.avatar(d, palette(talker))
        cv.create_image(0, 0, image=self._head_img, anchor="nw")
        cv.create_text(d / 2, d / 2, text=T.initial(name), font=T.font(15, "bold"), fill="#FFFFFF")
        self.head_name.configure(text=T.elide(name, T.font(15, "bold"), T.S(300)))
        if talker in self.allowed:
            fg, soft, label = {"auto": (T.GREEN, T.GREEN_SOFT, "自动回复中"),
                               "draft": (T.INDIGO, T.INDIGO_SOFT, "仅生成"),
                               "paused": (T.AMBER, T.AMBER_SOFT, "已暂停")}[self.mode]
            self.head_chip.set(label, fg, soft)
            self.head_btn.set_text("移出名单", "soft", icon=None)
        else:
            self.head_chip.set("未加入名单", T.MUTED, T.SURFACE_3)
            self.head_btn.set_text("加入名单", "primary", icon=T.I.ADD)
        self.head_btn.pack(side="right", padx=(0, T.S(18)))

    def _paint_suggestions(self, chat):
        items = chat["items"] if chat else []
        last = items[-1] if items else None
        d = last and not last["mine"] and last["decision"]
        self.suggest.set(d["candidates"][:3] if d and d["bucket"] == "drafted" else [])

    # ---------- 会话与名单 ----------
    def select(self, talker):
        if talker == self.selected:
            return
        self.selected = talker
        self.selected_name = self.names.get(talker) or next(
            (r["label"] for r in self.results if r["wxid"] == talker), "")
        self.sessions.select(talker)
        self._paint_head()
        self.refresh()

    def _head_action(self):
        if self.selected is None:
            return
        if self.selected in self.allowed:
            self.remove_contact(self.selected)
        else:
            self.add_contact(self.selected)

    def _search_changed(self, text):
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(250, self.search_contacts)

    def _search_submit(self):
        if len(self.results) == 1:
            self.add_contact(self.results[0]["wxid"])

    def _paint_count(self):
        if self.term:
            self.list_title.configure(text="搜索结果")
            self.count_label.configure(text=f"{len(self.results)} 个")
        else:
            self.list_title.configure(text="会话")
            self.count_label.configure(text=f"{len(self.allowed)} 人" if self.allowed else "")

    def clear_search(self):
        self.search.set("")
        self.root.focus_set()

    def search_contacts(self):
        self._search_job = None
        term = self.search.get()
        self.term = term
        if not term:
            self.results = []
            self._paint_count()
            if self.snapshot:
                self.sessions._sig = None
                self.sessions.show_sessions(self.snapshot["sessions"], self.snapshot["now"],
                                            self.allowed)
                self.sessions.select(self.selected, animate=False)
            return

        def work():
            with closing(self.backend.connect()) as con:
                return dashboard.search_contacts(con, term)

        def done(rows):
            if term != self.term:
                return  # 输入已变化，丢弃旧结果
            self.results = rows
            self._paint_count()
            self.sessions.show_results(rows, self.allowed, term)
            self.sessions.select(self.selected, animate=False)

        self._submit(work, done)

    def add_contact(self, talker):
        from ..core.contacts import display_name

        def work():
            with closing(self.backend.connect()) as con:
                label = display_name(con, talker)  # 与后台发送前相同的唯一性校验
            allowed = self.backend.allowlist()
            if talker in allowed:
                return label, False
            self.backend.set_allowlist(allowed + [talker])
            return label, True

        def done(result):
            label, added = result
            self.allowed = self.allowed + [talker] if added else self.allowed
            self.toast.show(f"已加入名单：{label}" if added else f"{label} 已在名单中")
            if self.term:
                self.sessions.show_results(self.results, self.allowed, self.term)
            self._paint_head()
            self.refresh()

        self._submit(work, done, lambda exc: self.toast.show(f"无法加入：{exc}", "warn"))

    def remove_contact(self, talker):
        label = self.names.get(talker, talker)
        if not messagebox.askyesno("移出名单",
                                   f"移出「{label}」后，TA 的新消息将不再自动回复。\n确定移出吗？",
                                   parent=self.root):
            return

        def work():
            self.backend.set_allowlist([t for t in self.backend.allowlist() if t != talker])

        def done(_):
            self.allowed = [t for t in self.allowed if t != talker]
            self.toast.show(f"已移出名单：{label}")
            self._paint_head()
            self.refresh()

        self._submit(work, done)

    # ---------- 模式与生成设置 ----------
    def change_mode(self, mode):
        prev = self.mode
        self.mode = mode
        self._mode_pending = True
        self._paint_head()

        def done(_):
            self._mode_pending = False
            if mode != "paused":
                self._resume_mode = mode
            self.toast.show(MODE_TOAST[mode], "warn" if mode == "paused" else "ok")
            self.refresh()

        def fail(exc):
            self._mode_pending = False
            self.mode = prev
            self.mode_seg.set(prev)
            self._paint_head()
            self.toast.show(str(exc) or type(exc).__name__, "warn")

        self._submit(lambda: self.backend.set_mode(mode), done, fail)

    def toggle_pause(self):
        target = self._resume_mode if self.mode == "paused" else "paused"
        self.mode_seg.set(target)
        self.change_mode(target)

    def change_effort(self, value):
        self._effort_pending = True

        def done(_):
            self._effort_pending = False
            self.toast.show(f"思考强度：{dict((k, v) for k, v, _ in EFFORTS)[value]}")

        def fail(exc):
            self._effort_pending = False
            self._action_failed(exc)
            self.refresh()

        self._submit(lambda: self.backend.set_effort(value), done, fail)

    def change_thinking(self, on):
        self._think_pending = True
        self.effort_seg.set_inactive(not on)

        def done(_):
            self._think_pending = False
            self.toast.show("深度思考已开启" if on else "深度思考已关闭")

        def fail(exc):
            self._think_pending = False
            self.think_switch.set(not on)
            self.effort_seg.set_inactive(on)
            self.toast.show(str(exc) or type(exc).__name__, "warn")

        self._submit(lambda: self.backend.set_thinking(on), done, fail)

    def start_worker(self):
        def work():
            if self.backend.worker_running():
                return "自动回复后台已经在运行。"
            return self.backend.start_worker()

        def done(error):
            if error:
                self.start_btn.set_enabled(True)
                self.toast.show(error, "info")
            else:
                self.toast.show("已请求启动后台任务")
                self.root.after(1500, self.refresh)

        self.start_btn.set_enabled(False)
        self._submit(work, done)

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.toast.show("已复制，可在微信中粘贴发送")

    # ---------- 运行记录 ----------
    def show_sticker_labeler(self):
        if self.sticker_win is not None and self.sticker_win.winfo_exists():
            self.sticker_win.deiconify()
            self.sticker_win.lift()
            return
        try:
            from ..sticker_labeler import open_labeler
            con = self.backend.connect()
            self.sticker_con = con
            self.sticker_win = open_labeler(
                self.backend.config(), con, parent=self.root,
                on_close=self._sticker_closed, demo=self.backend.demo)
        except Exception as exc:
            self._sticker_closed()
            self._action_failed(exc)

    def _sticker_closed(self):
        if self.sticker_con is not None:
            self.sticker_con.close()
        self.sticker_con = self.sticker_win = None

    def show_log(self):
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.deiconify()
            self.log_win.lift()
            return
        S = T.S
        win = self.log_win = tk.Toplevel(self.root)
        win.title("运行记录")
        win.geometry(f"{S(920)}x{S(560)}")
        win.configure(bg=T.BG)
        win.iconphoto(False, *self._icons)
        fx.set_caption(win, T.BG)
        bar = tk.Frame(win, bg=T.BG)
        bar.pack(fill="x", padx=S(26), pady=(S(16), S(2)))
        tk.Label(bar, text="运行记录", font=T.font(16, "bold"), bg=T.BG, fg=T.TEXT).pack(side="left")
        tk.Label(bar, text="最近 400 行", font=T.font(11.5), bg=T.BG, fg=T.SUBTLE).pack(
            side="left", padx=S(10), pady=(S(4), 0))

        def open_folder():
            try:
                self.backend.open_log_folder()
            except Exception as exc:
                self.toast.show(str(exc), "info")
        IconButton(bar, T.I.FOLDER, open_folder, bg=T.BG, tip="打开日志文件夹").pack(side="right")
        card = Card(win)
        card.pack(fill="both", expand=True, padx=S(14), pady=(0, S(12)))
        box = tk.Text(card.body, bg=T.SURFACE, fg=T.TEXT_2, relief="flat", wrap="word",
                      font=T.font(11.5, family=T.MONO), padx=S(16), pady=S(12), borderwidth=0,
                      highlightthickness=0, spacing1=S(2), spacing3=S(2),
                      selectbackground=T.GREEN_SOFT, selectforeground=T.TEXT)
        box.pack(fill="both", expand=True)
        for tag, color in (("warn", T.AMBER), ("err", T.RED), ("ok", T.GREEN_DEEP),
                           ("start", T.BLUE)):
            box.tag_configure(tag, foreground=color)
        win.bind("<Escape>", lambda _: win.destroy())
        last = [None]

        def tag_for(line):
            if line.startswith("[!]"):
                return "warn"
            if line.startswith("[x]") or "Traceback" in line or "Error:" in line:
                return "err"
            if line.startswith(("[✓]", "[+]")):
                return "ok"
            if line.startswith("[20"):
                return "start"
            return ()

        def load():
            if self.closed or not win.winfo_exists():
                return
            lines = self.backend.read_log(400)
            if lines != last[0]:
                last[0] = lines
                at_end = box.yview()[1] >= .999
                box.configure(state="normal")
                box.delete("1.0", "end")
                for line in lines:
                    box.insert("end", line + "\n", tag_for(line))
                box.configure(state="disabled")
                if at_end:
                    box.see("end")
            win.after(2000, load)

        load()
        box.see("end")

    def close(self):
        self.closed = True
        if self.sticker_win is not None and self.sticker_win.winfo_exists():
            self.sticker_win.destroy()
        self._sticker_closed()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)  # 动画与刷新定时器，避免销毁后回调报错
        self.root.destroy()


def _focus_existing(title: str) -> None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--demo", action="store_true",
                        help="演示模式：临时数据，不读写真实配置、数据库和计划任务")
    args = parser.parse_args(argv)
    mutex = kernel = None
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        mutex = kernel.CreateMutexW(None, False, "AutoReplyDashboard" +
                                    ("Demo" if args.demo else ""))
        if ctypes.get_last_error() == 183:
            _focus_existing(TITLE + ("（演示模式）" if args.demo else ""))
            return
    if args.demo:
        from .demo import DemoBackend
        backend = DemoBackend()
    else:
        from ..core import store
        from .backend import DB, LiveBackend
        store.connect(DB).close()
        backend = LiveBackend()
    try:
        root = tk.Tk()
        Dashboard(root, backend)
        root.mainloop()
    finally:
        backend.close()
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
