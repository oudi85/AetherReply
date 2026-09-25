"""左侧会话列表：自动回复名单里的联系人，或搜索结果。"""

import tkinter as tk
import zlib

from . import anim, fx
from . import theme as T
from .widgets import ScrollCanvas
from ..core.dashboard import format_clock


def palette(talker: str) -> int:
    return zlib.crc32(talker.encode("utf-8"))


class SessionList(ScrollCanvas):
    def __init__(self, parent, *, bg, on_select, on_add, on_remove):
        super().__init__(parent, bg=bg, takefocus=1)
        self.on_select, self.on_add, self.on_remove = on_select, on_add, on_remove
        self.rows, self.mode, self.selected, self.now = [], "sessions", None, 0
        self.allowed = set()
        self.empty = None
        self._sig, self._width = None, 0
        self.row_h = T.S(68)
        self.sel = fx.RRect(self, (0, 0, 1, 1), T.S(12), T.SELECTED, tags=("chrome",))
        self.hover = fx.RRect(self, (0, 0, 1, 1), T.S(12), T.HOVER, tags=("chrome",))
        self.bar = fx.RRect(self, (0, 0, 1, 1), T.S(2), T.GREEN, tags=("chrome",))
        for r in (self.sel, self.hover, self.bar):
            r.state(False)
        self._sel_y, self._hover_i = None, None
        self._imgs = []
        self.menu = tk.Menu(self, tearoff=0, font=T.font(12), bg=T.SURFACE, fg=T.TEXT,
                            activebackground=T.SELECTED, activeforeground=T.TEXT, bd=0)
        self.menu.add_command(label="移出自动回复名单", command=lambda: self._remove(self._menu_t))
        self._menu_t = None
        self.bind("<Configure>", self._resized)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _: self._set_hover(None), add="+")
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Button-3>", self._context)
        self.bind("<Key-Delete>", lambda _: self._remove(self.selected))
        self.bind("<Up>", lambda _: self._step(-1))
        self.bind("<Down>", lambda _: self._step(1))

    # ---------- 数据 ----------
    def show_sessions(self, sessions, now, allowed):
        self.now, self.allowed = now, set(allowed)
        sig = ("s", tuple((s["talker"], s["name"], s["badge"], s["last_ts"], s["last_text"])
                          for s in sessions), now // 60)
        self.mode = "sessions"
        if sig != self._sig:
            self._sig = sig
            self.rows = sessions
            self._render()

    def show_results(self, results, allowed, term):
        self.allowed = set(allowed)
        sig = ("r", term, tuple((r["wxid"], r["label"]) for r in results), tuple(sorted(allowed)))
        self.mode = "results"
        if sig != self._sig:
            self._sig = sig
            self.rows = results
            self._render(reset=True)

    def select(self, talker, animate=True):
        self.selected = talker
        self._place_selection(animate)

    def _talker(self, row):
        return row["talker"] if self.mode == "sessions" else row["wxid"]

    # ---------- 绘制 ----------
    def _resized(self, e):
        if abs(e.width - self._width) > 1:
            self._width = e.width
            self._render()
        else:
            self.set_content(self.content_h)

    def _render(self, reset=False):
        w = self.winfo_width()
        if w < T.S(80):
            return
        self.delete("row")
        self._imgs = []
        self._hover_i = None
        self.hover.state(False)
        if not self.rows:
            self._render_empty(w)
            self.set_content(0)
            self._place_selection(False)
            return
        y = T.S(4)
        for i, row in enumerate(self.rows):
            self._render_row(i, row, y, w)
            y += self.row_h
        self.set_content(y + T.S(8))
        if reset:
            self.scroll_to(0, animate=False)
        self._place_selection(False)
        self.tag_raise("thumb")

    def _render_empty(self, w):
        h = max(self.view_h(), T.S(240))
        cy = h * .38
        if self.mode == "results":
            glyph, title, sub = T.I.SEARCH, "没有找到联系人", "换个昵称、备注或 wxid 试试"
        else:
            glyph, title, sub = T.I.PEOPLE, "名单还是空的", "在上方搜索联系人并加入"
        self.create_oval(w / 2 - T.S(30), cy - T.S(30), w / 2 + T.S(30), cy + T.S(30),
                         fill=T.SURFACE_2, outline="", tags="row")
        self.create_text(w / 2, cy, text=glyph, font=T.icon(22), fill=T.SUBTLE, tags="row")
        self.create_text(w / 2, cy + T.S(52), text=title, font=T.font(13, "bold"),
                         fill=T.TEXT_2, tags="row")
        self.create_text(w / 2, cy + T.S(76), text=sub, font=T.font(11.5), fill=T.SUBTLE,
                         tags="row")

    def _render_row(self, i, row, y, w):
        talker = self._talker(row)
        name = row["name"] if self.mode == "sessions" else row["label"]
        tag = ("row", f"r{i}")
        d = T.S(44)
        ax, cy = T.S(18), y + self.row_h // 2
        img = fx.avatar(d, palette(talker))
        self._imgs.append(img)
        self.create_image(ax, cy, image=img, anchor="w", tags=tag)
        self.create_text(ax + d // 2, cy, text=T.initial(name), font=T.font(16, "bold"),
                         fill="#FFFFFF", tags=tag)
        x = ax + d + T.S(12)
        right = w - T.S(18)
        if self.mode == "sessions":
            badge = row["badge"]
            if badge:
                fg = T.STATUS.get(badge, (T.MUTED,))[0]
                dot = fx.dot(T.S(14), fg, T.SURFACE, T.S(2))
                self._imgs.append(dot)
                self.create_image(ax + d - T.S(2), cy - d // 2 + T.S(5), image=dot, tags=tag)
            when = format_clock(row["last_ts"], self.now) if row["last_ts"] else ""
            ft = T.font(11)
            self.create_text(right, cy - T.S(11), text=when, font=ft, fill=T.SUBTLE,
                             anchor="e", tags=tag)
            fn = T.font(13.5, "bold")
            self.create_text(x, cy - T.S(11), anchor="w", tags=tag, font=fn, fill=T.TEXT,
                             text=T.elide(name, fn, right - x - ft.measure(when) - T.S(8)))
            fp = T.font(12)
            px = x
            if badge in ("review", "retrying", "drafted"):
                fg, _, label = T.STATUS[badge]
                label = f"[{label}]"
                self.create_text(px, cy + T.S(12), text=label, font=fp, fill=fg, anchor="w",
                                 tags=tag)
                px += fp.measure(label) + T.S(3)
            preview = ("我：" if row["last_mine"] else "") + (row["last_text"] or "")
            self.create_text(px, cy + T.S(12), anchor="w", tags=tag, font=fp, fill=T.MUTED,
                             text=T.elide(preview, fp, right - px))
        else:
            added = talker in self.allowed
            fb = T.font(12, "bold")
            label = "已加入" if added else "+ 加入"
            bw, bh = fb.measure(label) + T.S(22), T.S(28)
            bx = right - bw
            if added:
                self.create_text(right, cy, text=label, font=T.font(12), fill=T.SUBTLE,
                                 anchor="e", tags=tag)
            else:
                pill = fx.RRect(self, (bx, cy - bh // 2, right, cy + bh // 2), bh // 2,
                                T.GREEN_SOFT, tags=("row", f"add{i}"))
                self.create_text(bx + bw / 2, cy - 1, text=label, font=fb, fill=T.GREEN_DEEP,
                                 tags=("row", f"add{i}"))
                self.tag_bind(f"add{i}", "<Enter>", lambda _, p=pill: (
                    p.set(fill=T.blend(T.GREEN_SOFT, T.GREEN, .18)), self.configure(cursor="hand2")))
                self.tag_bind(f"add{i}", "<Leave>", lambda _, p=pill: (
                    p.set(fill=T.GREEN_SOFT), self.configure(cursor="")))
            fn = T.font(13.5, "bold")
            self.create_text(x, cy - T.S(10), anchor="w", tags=tag, font=fn, fill=T.TEXT,
                             text=T.elide(name, fn, bx - x - T.S(8)))
            fs = T.font(11, family=T.MONO)
            self.create_text(x, cy + T.S(12), anchor="w", tags=tag, font=fs, fill=T.SUBTLE,
                             text=T.elide(talker, fs, bx - x - T.S(8)))

    # ---------- 选中与悬停 ----------
    def _row_box(self, i):
        y = T.S(4) + i * self.row_h
        return (T.S(8), y + T.S(2), self.winfo_width() - T.S(8), y + self.row_h - T.S(2))

    def _index_of(self, talker):
        for i, row in enumerate(self.rows):
            if self._talker(row) == talker:
                return i
        return None

    def _place_selection(self, animate=True):
        i = self._index_of(self.selected) if self.selected else None
        if i is None:
            self.sel.state(False)
            self.bar.state(False)
            self._sel_y = None
            return
        x0, y0, x1, y1 = self._row_box(i)
        start = self._sel_y
        self.sel.state(True)
        self.bar.state(self.mode == "sessions")
        bh = T.S(24)

        def put(y):
            self.sel.place((x0, y, x1, y + (y1 - y0)))
            mid = y + (y1 - y0) / 2
            self.bar.place((x0 + T.S(1), mid - bh / 2, x0 + T.S(4), mid + bh / 2))
            self.tag_lower("chrome")
            self.hover.lower()
        if start is None or not animate:
            put(y0)
        else:
            anim.animate(("sel", id(self)), 240, lambda t: put(start + (y0 - start) * t))
        self._sel_y = y0
        if self._hover_i == i:
            self.hover.state(False)
        # 选中项滚动到可见区域
        if y0 < self.y:
            self.scroll_to(y0 - T.S(4))
        elif y1 > self.y + self.view_h():
            self.scroll_to(y1 - self.view_h() + T.S(4))

    def _row_at(self, e):
        y = self.canvasy(e.y) - T.S(4)
        i = int(y // self.row_h)
        return i if 0 <= i < len(self.rows) and y >= 0 else None

    def _motion(self, e):
        self._set_hover(None if self.thumb_hit(e) else self._row_at(e))

    def _set_hover(self, i):
        if i == self._hover_i:
            return
        self._hover_i = i
        if i is None or self._talker(self.rows[i]) == self.selected:
            anim.cancel(("hover", id(self)))
            self.hover.state(False)
            return
        self.hover.place(self._row_box(i))
        self.hover.state(True)
        self.hover.lower()
        anim.animate(("hover", id(self)), 150,
                     lambda t: self.hover.set(fill=T.blend(T.SURFACE, T.HOVER, t)))

    def _click(self, e):
        if self.thumb_hit(e):
            return
        self.focus_set()
        i = self._row_at(e)
        if i is None:
            return
        talker = self._talker(self.rows[i])
        if self.mode == "results" and f"add{i}" in self.gettags("current"):
            self.on_add(talker)
            return
        self.select(talker)
        self.on_select(talker)

    def _step(self, delta):
        if not self.rows:
            return
        i = self._index_of(self.selected)
        i = 0 if i is None else max(0, min(len(self.rows) - 1, i + delta))
        talker = self._talker(self.rows[i])
        self.select(talker)
        self.on_select(talker)

    def _context(self, e):
        i = self._row_at(e)
        if i is None or self.mode != "sessions":
            return
        self._menu_t = self._talker(self.rows[i])
        self.menu.tk_popup(e.x_root, e.y_root)

    def _remove(self, talker):
        if talker and self.mode == "sessions" and talker in self.allowed:
            self.on_remove(talker)
