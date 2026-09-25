"""画布自绘控件：卡片、按钮、分段选择、开关、呼吸灯、搜索框、提示、滚动区域。"""

import tkinter as tk

from . import anim, fx
from . import theme as T

KEY = "#0B0C0F"  # 浮层透明色；深色浮层的抗锯齿边缘与它混合后仍是深色，不会发白


def _canvas(parent, bg, **kw):
    return tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0, **kw)


class Card(tk.Canvas):
    """白色圆角卡片 + 柔和投影；内容放进 .body。split 为 body 顶部起算的分隔位置。"""

    def __init__(self, parent, *, bg=T.BG, fill=T.SURFACE, fill2=None, split=None,
                 radius=16, **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, **kw)
        self.bg, self.fill, self.fill2 = bg, fill, fill2
        self.margin, self.radius = T.S(12), T.S(radius)
        self.inset = int(self.radius * .3) + 1
        self.split = split
        self.body = tk.Frame(self, bg=fill)
        self._img = self.create_image(0, 0, anchor="nw")
        self._win = self.create_window(0, 0, anchor="nw", window=self.body)
        self._job = None
        self._size = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e):
        m = self.margin + self.inset
        self.coords(self._win, m, m)
        self.itemconfigure(self._win, width=max(1, e.width - 2 * m),
                           height=max(1, e.height - 2 * m))
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(40, self._paint)

    def set_split(self, split):
        if split != self.split:
            self.split = split
            self._size = None
            self._paint()

    def _paint(self):
        self._job = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4 or (w, h) == self._size:
            return
        self._size = (w, h)
        split = self.margin + self.inset + self.split if self.split else None
        img = fx.panel(w, h, margin=self.margin, radius=self.radius, fill=self.fill,
                       bg=self.bg, fill2=self.fill2, split=split)
        self.itemconfigure(self._img, image=img)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.win, self.job = widget, text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _enter(self, _):
        self._hide()
        self.job = self.widget.after(450, self._show)

    def _show(self):
        self.job = None
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        f = T.font(11.5)
        pad_x, pad_y = T.S(10), T.S(6)
        w, h = f.measure(text) + 2 * pad_x, f.metrics("linespace") + 2 * pad_y
        win = self.win = tk.Toplevel(self.widget)
        win.overrideredirect(True)
        win.configure(bg=KEY)
        win.attributes("-transparentcolor", KEY)
        cv = _canvas(win, KEY, width=w, height=h)
        cv.pack()
        fx.RRect(cv, (0, 0, w, h), T.S(7), "#24272C")
        cv.create_text(w // 2, h // 2, text=text, fill="#F4F5F7", font=f)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - w // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + T.S(6)
        win.geometry(f"+{x}+{y}")
        win.attributes("-alpha", 0.0)
        anim.animate(("tip", id(self)), 140, lambda t: win.attributes("-alpha", .96 * t))

    def _hide(self, _=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.win is not None:
            anim.cancel(("tip", id(self)))
            self.win.destroy()
            self.win = None


class Toast:
    """底部居中的深色提示，淡入上浮，约 2.2 秒后淡出。"""

    def __init__(self, root):
        self.root, self.win, self.job = root, None, None

    def show(self, text, kind="ok"):
        self._close()
        glyph, color = {"ok": (T.I.CHECK, "#4ADE80"), "warn": (T.I.WARN, "#FBBF24"),
                        "info": (T.I.CHAT, "#A5B4FC")}.get(kind, (T.I.CHECK, "#4ADE80"))
        f, fi = T.font(12.5), T.icon(12)
        pad, gap = T.S(18), T.S(10)
        h = T.S(42)
        w = pad * 2 + fi.measure(glyph) + gap + f.measure(text)
        win = self.win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=KEY)
        win.attributes("-transparentcolor", KEY)
        cv = _canvas(win, KEY, width=w, height=h)
        cv.pack()
        fx.RRect(cv, (0, 0, w, h), h // 2, "#222428")
        cv.create_text(pad, h // 2, text=glyph, fill=color, font=fi, anchor="w")
        cv.create_text(pad + fi.measure(glyph) + gap, h // 2, text=text, fill="#F5F6F8",
                       font=f, anchor="w")
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y0 = self.root.winfo_rooty() + self.root.winfo_height() - h - T.S(36)
        rise = T.S(14)
        win.attributes("-alpha", 0.0)
        win.geometry(f"+{x}+{y0 + rise}")

        def step(t):
            win.attributes("-alpha", .97 * t)
            win.geometry(f"+{x}+{int(y0 + rise * (1 - t))}")
        anim.animate(("toast",), 260, step)
        self.job = self.root.after(2300, self._fade)

    def _fade(self):
        win = self.win
        if win is None:
            return
        anim.animate(("toast",), 220, lambda t: win.attributes("-alpha", .97 * (1 - t)),
                     self._close)

    def _close(self):
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        anim.cancel(("toast",))
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class Button(tk.Canvas):
    def __init__(self, parent, text, command, *, bg, variant="soft", icon=None,
                 height=34, font_px=12.5, px=14, radius=9, width=None):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bg, self.command, self.variant = bg, command, variant
        self.icon_glyph, self.h, self.px, self.fixed_w = icon, T.S(height), T.S(px), width
        self.f, self.fi = T.font(font_px, "bold" if variant == "primary" else "normal"), T.icon(font_px)
        self.enabled, self._t, self.text = True, 0.0, text
        self.rect = None
        self._layout()
        self.bind("<Enter>", lambda _: self._hover(1))
        self.bind("<Leave>", lambda _: self._hover(0))
        self.bind("<ButtonRelease-1>", self._click)

    def _colors(self):
        v = self.variant
        if v == "primary":
            base, hover, fg = T.GREEN, T.GREEN_DEEP, "#FFFFFF"
        elif v == "danger":
            base, hover, fg = T.RED_SOFT, T.blend(T.RED_SOFT, T.RED, .15), T.RED
        elif v == "indigo":
            base, hover, fg = T.INDIGO_SOFT, T.blend(T.INDIGO_SOFT, T.INDIGO, .15), T.INDIGO
        elif v == "ghost":
            base, hover, fg = self.bg, T.SURFACE_3, T.TEXT_2
        else:
            base, hover, fg = T.SURFACE_3, T.blend(T.SURFACE_3, T.TEXT, .08), T.TEXT_2
        if not self.enabled:
            return T.blend(base, self.bg, .5), T.blend(base, self.bg, .5), T.SUBTLE
        return base, hover, fg

    def _layout(self):
        self.delete("all")
        gap = T.S(6) if self.icon_glyph and self.text else 0
        iw = self.fi.measure(self.icon_glyph) if self.icon_glyph else 0
        tw = self.f.measure(self.text) if self.text else 0
        w = self.fixed_w and T.S(self.fixed_w) or iw + gap + tw + 2 * self.px
        self.configure(width=w, height=self.h)
        base, hover, fg = self._colors()
        self.rect = fx.RRect(self, (0, 0, w, self.h), T.S(9), T.blend(base, hover, self._t))
        x = (w - iw - gap - tw) // 2
        self.items = []
        if self.icon_glyph:
            self.items.append(self.create_text(x, self.h // 2, text=self.icon_glyph, font=self.fi,
                                               fill=fg, anchor="w"))
        if self.text:
            self.items.append(self.create_text(x + iw + gap, self.h // 2 - 1, text=self.text,
                                               font=self.f, fill=fg, anchor="w"))

    def _hover(self, on):
        if not self.enabled:
            return
        start = self._t

        def step(t):
            self._t = start + (on - start) * t
            base, hover, _ = self._colors()
            self.rect.set(fill=T.blend(base, hover, self._t))
        anim.animate(("btn", id(self)), 140, step)

    def _click(self, e):
        if self.enabled and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.command()

    def set_enabled(self, enabled):
        if enabled != self.enabled:
            self.enabled = enabled
            self._t = 0.0
            self.configure(cursor="hand2" if enabled else "arrow")
            self._layout()

    def set_text(self, text, variant=None, icon=...):
        if icon is not ...:
            self.icon_glyph = icon
        if text != self.text or (variant and variant != self.variant):
            self.text, self.variant = text, variant or self.variant
            self._layout()


class IconButton(tk.Canvas):
    def __init__(self, parent, glyph, command, *, bg, tip=None, size=34, glyph_px=14,
                 color=T.TEXT_2):
        d = T.S(size)
        super().__init__(parent, bg=bg, width=d, height=d, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.bg, self.command, self._t, self.color = bg, command, 0.0, color
        self.rect = fx.RRect(self, (0, 0, d, d), T.S(9), bg)
        self.glyph = self.create_text(d // 2, d // 2, text=glyph, font=T.icon(glyph_px), fill=color)
        self.bind("<Enter>", lambda _: self._hover(1))
        self.bind("<Leave>", lambda _: self._hover(0))
        self.bind("<ButtonRelease-1>", lambda e: command())
        if tip:
            Tooltip(self, tip)

    def _hover(self, on):
        start = self._t

        def step(t):
            self._t = start + (on - start) * t
            self.rect.set(fill=T.blend(self.bg, T.SURFACE_3, self._t))
            self.itemconfigure(self.glyph, fill=T.blend(self.color, T.TEXT, self._t))
        anim.animate(("ibtn", id(self)), 140, step)

    def spin(self):
        """刷新时旋转一下的替代效果：图标轻微闪烁。"""
        anim.animate(("ispin", id(self)), 500, lambda t: self.itemconfigure(
            self.glyph, fill=T.blend(T.GREEN, self.color, t)))


class Chip(tk.Canvas):
    """小圆角标签：状态、模型名、提示。"""

    def __init__(self, parent, text="", *, bg, fg=T.TEXT_2, soft=T.SURFACE_3, glyph=None,
                 font_px=11.5, height=24, px=10, dot=False, bold=False):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0)
        self.bg, self.h, self.px = bg, T.S(height), T.S(px)
        self.f, self.fi = T.font(font_px, "bold" if bold else "normal"), T.icon(font_px - .5)
        self.dot = dot
        self.value = None
        self.set(text, fg, soft, glyph)

    def set(self, text, fg=None, soft=None, glyph=None):
        value = (text, fg, soft, glyph)
        if value == self.value:
            return
        self.value = value
        self.delete("all")
        if not text:
            self.configure(width=1, height=1)
            return
        lead = 0
        if glyph:
            lead = self.fi.measure(glyph) + T.S(5)
        elif self.dot:
            lead = T.S(6) + T.S(6)
        w = self.px * 2 + lead + self.f.measure(text)
        self.configure(width=w, height=self.h)
        fx.RRect(self, (0, 0, w, self.h), self.h // 2, soft)
        x = self.px
        if glyph:
            self.create_text(x, self.h // 2, text=glyph, font=self.fi, fill=fg, anchor="w")
        elif self.dot:
            self._dot = fx.dot(T.S(6), fg)
            self.create_image(x, self.h // 2, image=self._dot, anchor="w")
        self.create_text(x + lead, self.h // 2 - 1, text=text, font=self.f, fill=fg, anchor="w")


class Segmented(tk.Canvas):
    """分段选择：白色滑块在浅灰轨道上补间滑动。options: [(value, label, color)]"""

    def __init__(self, parent, options, command, *, bg, height=36, width=None, font_px=12.5,
                 dots=False, seg_px=18):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bg, self.options, self.command, self.dots = bg, options, command, dots
        self.f = T.font(font_px, "bold")
        self.h, self.pad = T.S(height), T.S(3)
        self.disabled, self.on_disabled, self.inactive = set(), None, False
        dot_w = T.S(13) if dots else 0
        if width:
            total = T.S(width)
            seg = (total - 2 * self.pad) / len(options)
            self.segs = [seg] * len(options)
        else:
            self.segs = [self.f.measure(label) + dot_w + 2 * T.S(seg_px) for _, label, _ in options]
            total = int(sum(self.segs) + 2 * self.pad)
        self.w = total
        self.configure(width=total, height=self.h)
        self.value, self._x = None, None
        self.track = fx.RRect(self, (0, 0, total, self.h), T.S(10), T.SURFACE_3)
        self.shadow = fx.RRect(self, (0, 0, 1, 1), T.S(8), T.blend(T.SURFACE_3, "#000000", .08))
        self.pill = fx.RRect(self, (0, 0, 1, 1), T.S(8), T.SURFACE)
        self.labels, self.dot_items, self._dots = [], [], {}
        x = self.pad
        for i, (value, label, color) in enumerate(options):
            cx = x + self.segs[i] / 2
            tx = cx + dot_w / 2
            if dots:
                self.dot_items.append(self.create_image(tx - self.f.measure(label) / 2 - T.S(7),
                                                        self.h // 2, anchor="e"))
            self.labels.append(self.create_text(tx, self.h // 2 - 1, text=label, font=self.f,
                                                fill=T.MUTED))
            x += self.segs[i]
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _: self._paint_labels(None))
        self._tip = Tooltip(self, self._tip_text)
        self._hover_i = None

    def _seg_box(self, i):
        x0 = self.pad + sum(self.segs[:i])
        return x0, x0 + self.segs[i]

    def _index_at(self, x):
        pos = self.pad
        for i, seg in enumerate(self.segs):
            if pos <= x < pos + seg:
                return i
            pos += seg
        return None

    def _tip_text(self):
        i = self._hover_i
        if i is not None and self.options[i][0] in self.disabled:
            return self.disabled_tip
        return None

    disabled_tip = "待后台接入"

    def _motion(self, e):
        i = self._index_at(e.x)
        if i != self._hover_i:
            self._hover_i = i
            self._paint_labels(i)

    def _click(self, e):
        i = self._index_at(e.x)
        if i is None or self.inactive:
            return
        value = self.options[i][0]
        if value in self.disabled:
            if self.on_disabled:
                self.on_disabled(value)
            self._nudge()
            return
        if value != self.value:
            self.set(value)
            self.command(value)

    def _nudge(self):
        if self._x is None:
            return
        x0, x1 = self._x
        amp = T.S(3)
        import math

        def step(t):
            dx = math.sin(t * math.pi * 3) * amp * (1 - t)
            self._place(x0 + dx, x1 + dx)
        anim.animate(("seg", id(self)), 320, step, ease=lambda t: t)

    def _place(self, x0, x1):
        p = self.pad
        self.pill.place((x0, p, x1, self.h - p))
        self.shadow.place((x0, p + 1, x1, self.h - p + 1))

    def _paint_labels(self, hover):
        for i, (value, label, color) in enumerate(self.options):
            if value == self.value:
                fg = color if not self.inactive else T.MUTED
            elif value in self.disabled or self.inactive:
                fg = T.SUBTLE
            else:
                fg = T.TEXT_2 if i == hover else T.MUTED
            self.itemconfigure(self.labels[i], fill=fg)
            if self.dots:
                c = color if value == self.value and not self.inactive else (
                    T.blend(color, T.SURFACE_3, .55) if value not in self.disabled else T.SUBTLE)
                self._dots[i] = fx.dot(T.S(7), c)
                self.itemconfigure(self.dot_items[i], image=self._dots[i])

    def set(self, value, animate=True):
        if value not in [o[0] for o in self.options]:
            return
        i = [o[0] for o in self.options].index(value)
        target = self._seg_box(i)
        self.value = value
        self._paint_labels(self._hover_i)
        if self._x is None or not animate or not self.winfo_ismapped():
            self._x = target
            self._place(*target)
            return
        sx0, sx1 = self._x
        self._x = target

        def step(t):
            self._place(sx0 + (target[0] - sx0) * t, sx1 + (target[1] - sx1) * t)
        anim.animate(("seg", id(self)), 280, step)

    def set_disabled(self, values, on_disabled=None):
        self.disabled = set(values)
        self.on_disabled = on_disabled
        self._paint_labels(self._hover_i)

    def set_inactive(self, inactive):
        """整体置灰（例如深度思考关闭时的思考强度）。"""
        if inactive != self.inactive:
            self.inactive = inactive
            self.pill.set(fill=T.blend(T.SURFACE, T.SURFACE_3, .5) if inactive else T.SURFACE)
            self.configure(cursor="arrow" if inactive else "hand2")
            self._paint_labels(None)


class Switch(tk.Canvas):
    def __init__(self, parent, command, *, bg, value=False):
        self.w, self.h = T.S(40), T.S(22)
        super().__init__(parent, bg=bg, width=self.w, height=self.h, highlightthickness=0,
                         bd=0, cursor="hand2")
        self.bg, self.command = bg, command
        self.value, self.enabled, self._t = value, True, 1.0 if value else 0.0
        self.on_disabled = None
        self.track = fx.RRect(self, (0, 0, self.w, self.h), self.h // 2, T.SUBTLE)
        kd = self.h - T.S(4)
        self._knob, self.kpad = fx.knob(kd)
        self.kd = kd
        self.knob = self.create_image(0, 0, image=self._knob, anchor="nw")
        self._paint()
        self.bind("<ButtonRelease-1>", self._click)

    def _paint(self):
        on_color = T.GREEN
        off_color = "#D3D7DD"
        color = T.blend(off_color, on_color, self._t)
        if not self.enabled:
            color = T.blend(color, self.bg, .45)
        self.track.set(fill=color)
        x = T.S(2) + (self.w - self.kd - T.S(4)) * self._t
        self.coords(self.knob, x - self.kpad, T.S(2) - self.kpad)

    def _click(self, _):
        if not self.enabled:
            if self.on_disabled:
                self.on_disabled()
            return
        self.set(not self.value)
        self.command(self.value)

    def set(self, value, animate=True):
        if value == self.value and (self._t in (0.0, 1.0)):
            return
        self.value = value
        start, end = self._t, 1.0 if value else 0.0
        if not animate:
            self._t = end
            self._paint()
            return

        def step(t):
            self._t = start + (end - start) * t
            self._paint()
        anim.animate(("sw", id(self)), 220, step)

    def set_enabled(self, enabled, on_disabled=None):
        self.on_disabled = on_disabled
        if enabled != self.enabled:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self._paint()


class PulseDot(tk.Canvas):
    """运行状态灯：正常时绿色光环呼吸，警告时缓慢琥珀色，异常为静止红点。"""

    def __init__(self, parent, *, bg, size=18):
        self.d = T.S(size)
        super().__init__(parent, bg=bg, width=self.d, height=self.d, highlightthickness=0, bd=0)
        self.item = self.create_image(self.d // 2, self.d // 2)
        self.level, self.i, self.job = None, 0, None
        self.set("ok")

    def set(self, level):
        if level == self.level:
            return
        self.level = level
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        if level == "bad":
            self._still = fx.dot(int(self.d * .4), T.RED)
            self.itemconfigure(self.item, image=self._still)
            return
        self.frames = fx.glow_frames(T.LEVEL[level], self.d)
        self._step()

    def _step(self):
        try:
            self.itemconfigure(self.item, image=self.frames[self.i % len(self.frames)])
        except tk.TclError:
            return
        self.i += 1
        slow = self.level == "warn"
        # 每轮扩散后停顿一下，节奏更像呼吸
        delay = (95 if slow else 60) if self.i % len(self.frames) else (900 if slow else 500)
        self.job = self.after(delay, self._step)


class SearchField(tk.Canvas):
    def __init__(self, parent, *, bg, placeholder, on_change, on_submit=None, on_escape=None):
        self.ring = T.S(3)
        self.fh = T.S(36)
        super().__init__(parent, bg=bg, height=self.fh + 2 * self.ring, highlightthickness=0,
                         bd=0, cursor="xterm")
        self.bg, self.on_change = bg, on_change
        self._t = 0.0
        self.glow = fx.RRect(self, (0, 0, 1, 1), T.S(12), T.GREEN_SOFT)
        self.field = fx.RRect(self, (0, 0, 1, 1), T.S(10), T.SURFACE_2, T.SURFACE_2)
        self.glow.state(False)
        fi = T.icon(12.5)
        self.icon = self.create_text(0, 0, text=T.I.SEARCH, font=fi, fill=T.SUBTLE, anchor="w")
        self.clear = self.create_text(0, 0, text=T.I.CLOSE, font=T.icon(10), fill=T.SUBTLE,
                                      state="hidden")
        self.var = tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.var, relief="flat", bd=0, font=T.font(12.5),
                              bg=T.SURFACE_2, fg=T.TEXT, insertbackground=T.TEXT,
                              insertwidth=T.S(1), highlightthickness=0,
                              selectbackground=T.GREEN_SOFT, selectforeground=T.TEXT)
        self.hint = tk.Label(self, text=placeholder, font=T.font(12.5), bg=T.SURFACE_2,
                             fg=T.SUBTLE, bd=0, cursor="xterm")
        self.win = self.create_window(0, 0, anchor="w", window=self.entry)
        self.hint_win = self.create_window(0, 0, anchor="w", window=self.hint)
        self.bind("<Configure>", self._layout)
        self.bind("<Button-1>", lambda _: self.entry.focus_set())
        self.hint.bind("<Button-1>", lambda _: self.entry.focus_set())
        self.tag_bind(self.clear, "<Button-1>", lambda _: self.set(""))
        self.tag_bind(self.clear, "<Enter>", lambda _: self.itemconfigure(self.clear, fill=T.TEXT_2))
        self.tag_bind(self.clear, "<Leave>", lambda _: self.itemconfigure(self.clear, fill=T.SUBTLE))
        self.entry.bind("<FocusIn>", lambda _: self._focus(1))
        self.entry.bind("<FocusOut>", lambda _: self._focus(0))
        if on_submit:
            self.entry.bind("<Return>", lambda _: on_submit())
        if on_escape:
            self.entry.bind("<Escape>", lambda _: on_escape())
        self.var.trace_add("write", self._changed)

    def _layout(self, e=None):
        w = self.winfo_width()
        r = self.ring
        self.glow.place((0, 0, w, self.fh + 2 * r))
        self.field.place((r, r, w - r, r + self.fh))
        cy = r + self.fh // 2
        self.coords(self.icon, r + T.S(12), cy)
        self.coords(self.clear, w - r - T.S(16), cy)
        x = r + T.S(36)
        self.coords(self.win, x, cy)
        self.coords(self.hint_win, x, cy)
        self.itemconfigure(self.win, width=max(1, w - x - r - T.S(30)))

    def _focus(self, on):
        start = self._t
        self.glow.state(True)

        def step(t):
            self._t = start + (on - start) * t
            fill = T.blend(T.SURFACE_2, T.SURFACE, self._t)
            self.field.set(fill=fill, outline=T.blend(T.SURFACE_2, T.GREEN, self._t))
            self.glow.set(fill=T.blend(self.bg, T.GREEN_SOFT, self._t))
            self.entry.configure(bg=fill)
            self.hint.configure(bg=fill)
            self.itemconfigure(self.icon, fill=T.blend(T.SUBTLE, T.GREEN, self._t))

        def done():
            if not on:
                self.glow.state(False)
        anim.animate(("search", id(self)), 180, step, done)

    def _changed(self, *_):
        text = self.var.get()
        self.itemconfigure(self.clear, state="normal" if text else "hidden")
        self.itemconfigure(self.hint_win, state="hidden" if text else "normal")
        self.on_change(text)

    def get(self):
        return self.var.get().strip()

    def set(self, text):
        self.var.set(text)

    def focus_input(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")


class Tile(tk.Canvas):
    """统计块：数字滚动计数。"""

    def __init__(self, parent, label, color, *, bg, big=False):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0,
                         height=T.S(84 if big else 64))
        self.bg, self.label, self.color, self.big = bg, label, color, big
        self.value, self.shown = 0, 0.0
        self.rect = fx.RRect(self, (0, 0, 1, 1), T.S(12), T.SURFACE_2)
        self._dot = fx.dot(T.S(7), color)
        self.dot = self.create_image(0, 0, image=self._dot, anchor="w")
        self.lbl = self.create_text(0, 0, text=label, font=T.font(11.5), fill=T.MUTED, anchor="w")
        self.num = self.create_text(0, 0, text="0", anchor="w", fill=T.TEXT,
                                    font=T.font(30 if big else 21, "bold", T.NUM))
        self.bind("<Configure>", self._layout)

    def _layout(self, e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.rect.place((0, 0, w, h))
        px = T.S(14)
        top = T.S(18) if self.big else T.S(15)
        self.coords(self.dot, px, top)
        self.coords(self.lbl, px + T.S(12), top)
        self.coords(self.num, px - T.S(1), h - (T.S(28) if self.big else T.S(21)))

    def set(self, value):
        if value == self.value:
            return
        start, self.value = self.shown, value

        def step(t):
            self.shown = start + (value - start) * t
            self.itemconfigure(self.num, text=str(round(self.shown)))
        anim.animate(("tile", id(self)), 700, step)
        if value > start:
            anim.animate(("tileflash", id(self)), 900, lambda t: self.itemconfigure(
                self.num, fill=T.blend(self.color, T.TEXT, t)))


# ---------- 滚动区域 ----------
_scrollers: dict = {}


def _wheel(event):
    try:
        w = event.widget.winfo_containing(event.x_root, event.y_root)
    except (tk.TclError, KeyError):
        return None
    while w is not None:
        s = _scrollers.get(str(w))
        if s is not None:
            s.wheel(event.delta)
            return "break"
        w = w.master
    return None


class ScrollCanvas(tk.Canvas):
    """内容直接画在画布上；平滑滚轮 + 悬停时出现的细滚动条。"""

    def __init__(self, parent, *, bg, **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, yscrollincrement=1, **kw)
        if not _scrollers:
            self.bind_all("<MouseWheel>", _wheel, add="+")
        _scrollers[str(self)] = self
        self.bind("<Destroy>", lambda e: _scrollers.pop(str(self), None)
                  if e.widget is self else None, add="+")
        self.bg = bg
        self.content_h, self.y, self._target = 0, 0.0, 0.0
        self.thumb = fx.RRect(self, (0, 0, 1, 1), T.S(3), T.blend(bg, "#000000", .17),
                              tags=("thumb",))
        self.thumb.state(False)
        self._hovering, self._grab = False, None
        self.bind("<Enter>", lambda _: self._show_thumb(True), add="+")
        self.bind("<Leave>", lambda _: self._show_thumb(False), add="+")
        self.tag_bind("thumb", "<ButtonPress-1>", self._grab_thumb)
        self.tag_bind("thumb", "<B1-Motion>", self._drag_thumb)
        self.tag_bind("thumb", "<ButtonRelease-1>", lambda _: setattr(self, "_grab", None))

    def view_h(self):
        return max(1, self.winfo_height())

    def max_y(self):
        return max(0, self.content_h - self.view_h())

    def set_content(self, h):
        self.content_h = int(h)
        self.configure(scrollregion=(0, 0, self.winfo_width(), max(self.content_h, self.view_h())))
        self._apply(min(self.y, self.max_y()))
        self._target = self.y

    def _apply(self, y):
        self.y = max(0.0, min(float(y), self.max_y()))
        total = max(self.content_h, self.view_h())
        self.yview_moveto(self.y / total)
        self._place_thumb()

    def scroll_to(self, y, animate=True, ms=260):
        y = max(0.0, min(float(y), self.max_y()))
        self._target = y
        if not animate:
            anim.cancel(("scroll", id(self)))
            self._apply(y)
            return
        start = self.y
        anim.animate(("scroll", id(self)), ms, lambda t: self._apply(start + (y - start) * t))

    def scroll_bottom(self, animate=True):
        self.scroll_to(self.max_y(), animate)

    def at_bottom(self):
        return self.y >= self.max_y() - T.S(6)

    def wheel(self, delta):
        if self.max_y() <= 0:
            return
        self.scroll_to(self._target - delta / 120 * T.S(96), ms=220)
        self._show_thumb(True)

    def _thumb_box(self):
        vh = self.view_h()
        if self.content_h <= vh:
            return None
        th = max(T.S(36), vh * vh / self.content_h)
        pad = T.S(4)
        top = self.y + pad + (vh - th - 2 * pad) * (self.y / self.max_y())
        w = self.winfo_width()
        return (w - T.S(9), top, w - T.S(4), top + th)

    def _place_thumb(self):
        box = self._thumb_box()
        if box is None:
            self.thumb.state(False)
            return
        self.thumb.place(box)
        self.tag_raise("thumb")
        self.thumb.state(self._hovering or self._grab is not None)

    def _show_thumb(self, on):
        self._hovering = on
        self._place_thumb()

    def _grab_thumb(self, e):
        self._grab = (e.y, self.y)
        return "break"

    def _drag_thumb(self, e):
        if self._grab is None:
            return
        vh = self.view_h()
        th = max(T.S(36), vh * vh / self.content_h)
        span = max(1, vh - th - T.S(8))
        y0, sy = self._grab
        self.scroll_to(sy + (e.y - y0) * self.max_y() / span, animate=False)

    def thumb_hit(self, e):
        return self._grab is not None or "thumb" in self.gettags("current")
