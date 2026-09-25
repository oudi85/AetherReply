"""中间的聊天视图：微信式气泡 + 每条来讯下的 AI 回复卡片。只读展示，不发送任何内容。"""

import math
import tkinter as tk

from . import anim, fx
from . import theme as T
from .sessions import palette
from .widgets import ScrollCanvas
from ..core.dashboard import format_clock

SPARK = chr(0x2726)
COLLAPSED = ("sent", "done", "skipped")
CHECKED = ("sent", "sending", "review", "uncertain")


class ChatView(ScrollCanvas):
    def __init__(self, parent, *, on_copy):
        super().__init__(parent, bg=T.CHAT_BG)
        self.on_copy = on_copy
        self.chat, self.mode, self.now = None, "auto", 0
        self.expanded, self.analysis = set(), set()
        self._sig, self._width, self._job, self._typing = None, 0, None, None
        self._imgs, self._texts = [], {}
        self.menu = tk.Menu(self, tearoff=0, font=T.font(12), bg=T.SURFACE, fg=T.TEXT,
                            activebackground=T.SELECTED, activeforeground=T.TEXT, bd=0)
        self.menu.add_command(label="复制", command=lambda: self.on_copy(self._menu_text))
        self._menu_text = ""
        self.bind("<Configure>", self._resized)

    # ---------- 外部接口 ----------
    def show(self, chat, mode, now):
        switched = (chat or {}).get("talker") != (self.chat or {}).get("talker")
        retry = chat and any(i["decision"] and i["decision"]["bucket"] == "retrying"
                             for i in chat["items"][-6:])
        sig = (chat and chat["sig"], mode, now // 5 if retry else 0)
        self.chat, self.mode, self.now = chat, mode, now
        if switched:
            self.expanded.clear()
            self.analysis.clear()
        if sig != self._sig or switched:
            self._sig = sig
            follow = switched or self.at_bottom()
            self._render()
            if switched:
                self.scroll_bottom(animate=False)
                self._enter()
            elif follow:
                self.scroll_bottom()

    def _resized(self, e):
        if abs(e.width - self._width) > 1:
            self._width = e.width
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(60, self._rerender)
        else:
            bottom = self.at_bottom()
            self.set_content(self.content_h)
            if bottom:
                self.scroll_bottom(animate=False)

    def _rerender(self):
        self._job = None
        bottom = self.at_bottom()
        self._render()
        if bottom:
            self.scroll_bottom(animate=False)

    def _enter(self):
        off = T.S(16)
        moved = [0.0]
        self.move("msg", 0, off)
        moved[0] = off

        def step(t):
            want = off * (1 - t)
            self.move("msg", 0, want - moved[0])
            moved[0] = want
        anim.animate(("chat-in", id(self)), 280, step)

    # ---------- 绘制 ----------
    def _render(self):
        w = self.winfo_width()
        if w < T.S(160):
            return
        anim.cancel(("chat-in", id(self)))
        if self._typing:
            self.after_cancel(self._typing)
            self._typing = None
        self.delete("msg")
        self._imgs, self._texts = [], {}
        chat = self.chat
        if chat is None or not chat["items"]:
            self._empty(w, chat is None)
            self.set_content(0)
            return
        L = {"w": w, "px": T.S(24), "av": T.S(38), "gap": T.S(10)}
        L["max"] = min(int(w * .62), T.S(540))
        y = T.S(14)
        last = None
        for item in chat["items"]:
            if last is None or item["ts"] - last > 300:
                y = self._time(item["ts"], y, w)
            last = item["ts"]
            if item["type"] == 10000:
                y = self._system(item, y, w)
                continue
            y = self._bubble(item, y, L)
            if item["decision"] and not item["mine"]:
                y = self._decision(item, y, L)
        if chat["pending"]:
            y = self._pending(y, L)
        self.set_content(y + T.S(22))
        self.tag_raise("thumb")

    def _empty(self, w, nothing_selected):
        h = max(self.view_h(), T.S(300))
        cy = h * .42
        d = T.S(76)
        self._logo = fx.logo_photo(d, T.CHAT_BG)
        self._imgs.append(self._logo)
        self.create_image(w / 2, cy, image=self._logo, tags="msg")
        title = "选择一个会话" if nothing_selected else "暂无消息记录"
        sub = "在左侧名单或搜索结果中点击联系人" if nothing_selected else "同步到新消息后会显示在这里"
        self.create_text(w / 2, cy + d * .5 + T.S(34), text=title, font=T.font(15, "bold"),
                         fill=T.TEXT_2, tags="msg")
        self.create_text(w / 2, cy + d * .5 + T.S(60), text=sub, font=T.font(12),
                         fill=T.SUBTLE, tags="msg")

    def _time(self, ts, y, w):
        y += T.S(10)
        self.create_text(w / 2, y + T.S(9), text=format_clock(ts, self.now), font=T.font(11),
                         fill=T.SUBTLE, tags="msg")
        return y + T.S(30)

    def _system(self, item, y, w):
        f = T.font(11)
        text = T.elide(item["text"], f, int(w * .7))
        tw = f.measure(text) + T.S(20)
        h = T.S(24)
        fx.RRect(self, (w / 2 - tw / 2, y, w / 2 + tw / 2, y + h), T.S(6),
                 T.blend(T.CHAT_BG, "#000000", .05), tags=("msg",))
        self.create_text(w / 2, y + h / 2, text=text, font=f, fill=T.MUTED, tags="msg")
        return y + h + T.S(14)

    def _avatar(self, x, y, d, key, label, mine):
        img = fx.avatar(d, palette(key), mine)
        self._imgs.append(img)
        self.create_image(x, y, image=img, anchor="nw", tags="msg")
        self.create_text(x + d / 2, y + d / 2, text=label, font=T.font(14 if mine else 15, "bold"),
                         fill="#FFFFFF", tags="msg")

    def _bubble(self, item, y, L):
        mine, av, px, gap = item["mine"], L["av"], L["px"], L["gap"]
        f = T.font(13.5)
        bx_pad, by_pad = T.S(13), T.S(10)
        tag = f"m{item['id']}"
        media = item["type"] != 1
        tid = self.create_text(0, 0, text=item["text"] or " ", font=f, anchor="nw",
                               width=L["max"] - 2 * bx_pad, tags=("msg", tag),
                               fill=T.MUTED if media else T.TEXT)
        x0, y0, x1, y1 = self.bbox(tid)
        bw, bh = x1 - x0 + 2 * bx_pad, max(y1 - y0 + 2 * by_pad, av)
        w = L["w"]
        name = self.chat["name"]
        if mine:
            ax = w - px - av
            bx1 = ax - gap
            bx0 = bx1 - bw
            fill = T.BUBBLE_OUT
            self._avatar(ax, y, av, "me", "我", True)
        else:
            ax = px
            bx0 = ax + av + gap
            bx1 = bx0 + bw
            fill = T.BUBBLE_IN
            self._avatar(ax, y, av, self.chat["talker"], T.initial(name), False)
        fx.RRect(self, (bx0, y, bx1, y + bh), T.S(8), fill, tags=("msg", tag))
        # 气泡小尖角，指向头像
        ty = y + min(T.S(19), bh / 2)
        s = T.S(5)
        if mine:
            pts = (bx1 - 1, ty - s, bx1 + s, ty, bx1 - 1, ty + s)
        else:
            pts = (bx0 + 1, ty - s, bx0 - s, ty, bx0 + 1, ty + s)
        self.create_polygon(*pts, fill=fill, outline="", tags=("msg", tag))
        self.coords(tid, bx0 + bx_pad, y + (bh - (y1 - y0)) / 2)
        self.tag_raise(tid)
        self._texts[tag] = item["text"]
        self.tag_bind(tag, "<Button-3>", lambda e, t=item["text"]: self._context(e, t))
        y += bh
        if item["ai"]:
            self.create_text(bx1, y + T.S(5), text=f"{SPARK} AI 自动回复", anchor="ne",
                             font=T.font(10.5), fill=T.blend(T.INDIGO, T.CHAT_BG, .25), tags="msg")
            y += T.S(20)
        return y + T.S(14)

    def _context(self, e, text):
        self._menu_text = text
        self.menu.tk_popup(e.x_root, e.y_root)

    def _decision(self, item, y, L):
        d = item["decision"]
        mid = item["id"]
        bucket = d["bucket"]
        x = L["px"] + L["av"] + L["gap"]
        fg, soft, label = T.STATUS.get(bucket, (T.MUTED, T.SURFACE_3, bucket))
        if d["status"] == "generate_failed" and bucket in ("waiting", "retrying"):
            fg, soft, label = T.AMBER, T.AMBER_SOFT, "生成失败"
        collapsible = bucket in COLLAPSED
        if collapsible and mid not in self.expanded:
            return self._collapsed(item, y - T.S(6), x, label, fg)
        cw = max(T.S(280), min(L["w"] - x - L["px"] - T.S(36), T.S(470)))
        pad = T.S(16)
        top = y - T.S(2)
        tag = f"d{mid}"
        cy = top + pad + T.S(9)
        self.create_text(x + pad, cy, text=f"{SPARK} AI 回复", anchor="w",
                         font=T.font(12, "bold"), fill=T.INDIGO, tags=("msg", tag))
        # 状态胶囊
        fs = T.font(11, "bold")
        chip_w, chip_h = fs.measure(label) + T.S(18), T.S(22)
        right = x + cw - pad
        if collapsible:
            fold = self.create_text(right, cy, text=T.I.UP, font=T.icon(10), fill=T.SUBTLE,
                                    anchor="e", tags=("msg", tag, f"fold{mid}"))
            self.tag_bind(f"fold{mid}", "<Button-1>", lambda _: self._toggle(self.expanded, mid))
            self._hand(f"fold{mid}", fold, T.SUBTLE, T.TEXT_2)
            right -= T.S(22)
        fx.RRect(self, (right - chip_w, cy - chip_h / 2, right, cy + chip_h / 2), chip_h / 2,
                 soft, tags=("msg", tag))
        self.create_text(right - chip_w / 2, cy - 1, text=label, font=fs, fill=fg,
                         tags=("msg", tag))
        yy = cy + T.S(20)
        cands = d["candidates"]
        chosen = d["selected"] if d["status"] in CHECKED or bucket in CHECKED else None
        tone = T.GREEN if bucket == "sent" else T.INDIGO
        band_soft = T.GREEN_SOFT if bucket == "sent" else T.INDIGO_SOFT
        fc = T.font(13)
        for i, text in enumerate(cands):
            yy = self._candidate(mid, i, text, x + pad - T.S(4), yy, cw - 2 * pad + T.S(8),
                                 fc, i == chosen, tone, band_soft)
        if not cands and d["status"] != "generate_failed":
            self.create_text(x + pad, yy + T.S(10), text="暂无候选", anchor="w",
                             font=T.font(12), fill=T.SUBTLE, tags="msg")
            yy += T.S(24)
        notes = []
        if d["error"] and bucket != "review":
            notes.append((T.I.WARN, d["error"], T.RED))
        if bucket == "retrying":
            left = max(0, (d["next_attempt_at"] or 0) - self.now)
            when = f"约 {left} 秒后自动重试" if left else "即将自动重试"
            notes.append((T.I.REFRESH, f"已失败 {d['attempts']} 次 · {when}", T.AMBER))
        elif bucket == "review":
            reason = f"{d['error']} · " if d["error"] else "未能确认是否送达 · "
            notes.append((T.I.WARN, reason + "不会自动重发，请在微信中核对", T.RED))
        elif bucket == "drafted":
            notes.append((T.I.COPY, "仅生成，未发送 · 点击候选复制", T.INDIGO))
        fn = T.font(11.5)
        for glyph, text, color in notes:
            yy += T.S(4)
            self.create_text(x + pad, yy, text=glyph, font=T.icon(10.5), fill=color,
                             anchor="nw", tags="msg")
            tid = self.create_text(x + pad + T.S(18), yy - T.S(2), text=text, font=fn, fill=color,
                                   anchor="nw", width=cw - 2 * pad - T.S(18), tags="msg")
            yy = self.bbox(tid)[3] + T.S(4)
        if d["analysis"]:
            open_ = mid in self.analysis
            yy += T.S(4)
            atag = f"an{mid}"
            t1 = self.create_text(x + pad, yy, text="思路", font=T.font(11.5, "bold"),
                                  fill=T.MUTED, anchor="nw", tags=("msg", atag))
            self.create_text(self.bbox(t1)[2] + T.S(4), yy + T.S(3),
                             text=T.I.UP if open_ else T.I.DOWN, font=T.icon(9), fill=T.MUTED,
                             anchor="nw", tags=("msg", atag))
            self.tag_bind(atag, "<Button-1>", lambda _: self._toggle(self.analysis, mid))
            self._hand(atag)
            yy = self.bbox(t1)[3] + T.S(4)
            if open_:
                tid = self.create_text(x + pad, yy, text=d["analysis"], font=fn, fill=T.TEXT_2,
                                       anchor="nw", width=cw - 2 * pad, tags="msg")
                yy = self.bbox(tid)[3] + T.S(4)
        bottom = yy + pad - T.S(4)
        img, g = fx.ai_card(int(cw), int(bottom - top), bg=T.CHAT_BG)
        self._imgs.append(img)
        card = self.create_image(x - g, top - g, image=img, anchor="nw", tags="msg")
        self.tag_lower(card)
        return bottom + T.S(18)

    def _candidate(self, mid, i, text, x, y, w, f, chosen, tone, band_soft):
        tag = f"c{mid}_{i}"
        mark_w = T.S(26)
        tid = self.create_text(x + T.S(8) + mark_w, y + T.S(8), text=text, font=f, anchor="nw",
                               width=w - mark_w - T.S(40), fill=T.TEXT, tags=("msg", tag))
        th = self.bbox(tid)[3] - self.bbox(tid)[1]
        h = th + T.S(16)
        base = band_soft if chosen else T.SURFACE
        band = fx.RRect(self, (x, y, x + w, y + h), T.S(9), base, tags=("msg", tag))
        self.tag_raise(tid)
        mx, my = x + T.S(8) + mark_w / 2 - T.S(3), y + T.S(8) + f.metrics("linespace") / 2
        if chosen:
            dot = fx.dot(T.S(18), tone)
            self._imgs.append(dot)
            self.create_image(mx, my, image=dot, tags=("msg", tag))
            self.create_text(mx, my, text=T.I.CHECK, font=T.icon(9), fill="#FFFFFF",
                             tags=("msg", tag))
        else:
            self.create_text(mx, my, text=str(i + 1), font=T.font(11.5, "bold", T.NUM),
                             fill=T.SUBTLE, tags=("msg", tag))
        copy = self.create_text(x + w - T.S(14), my, text=T.I.COPY, font=T.icon(11),
                                fill=T.MUTED, anchor="e", state="hidden", tags=("msg", tag))

        def enter(_):
            band.set(fill=T.blend(base, T.TEXT, .05) if chosen else T.SURFACE_2)
            self.itemconfigure(copy, state="normal")
            self.configure(cursor="hand2")

        def leave(_):
            band.set(fill=base)
            self.itemconfigure(copy, state="hidden")
            self.configure(cursor="")
        self.tag_bind(tag, "<Enter>", enter)
        self.tag_bind(tag, "<Leave>", leave)
        self.tag_bind(tag, "<ButtonRelease-1>", lambda _: self._copied(text, copy))
        return y + h + T.S(4)

    def _copied(self, text, item):
        self.on_copy(text)
        self.itemconfigure(item, text=T.I.CHECK, fill=T.GREEN)
        self.after(1200, lambda: self._restore_copy(item))

    def _restore_copy(self, item):
        try:
            self.itemconfigure(item, text=T.I.COPY, fill=T.MUTED)
        except tk.TclError:
            pass

    def _collapsed(self, item, y, x, label, fg):
        mid = item["id"]
        n = len(item["decision"]["candidates"])
        tag = f"fold{mid}"
        f = T.font(11)
        text = f"{SPARK} AI {label}" + (f" · {n} 个候选" if n else "")
        t = self.create_text(x + T.S(2), y + T.S(4), text=text, font=f, anchor="nw",
                             fill=T.blend(T.INDIGO, T.CHAT_BG, .3), tags=("msg", tag))
        self.create_text(self.bbox(t)[2] + T.S(4), y + T.S(7), text=T.I.DOWN,
                                font=T.icon(8.5), fill=T.blend(T.INDIGO, T.CHAT_BG, .3),
                                anchor="nw", tags=("msg", tag))
        self.tag_bind(tag, "<Button-1>", lambda _: self._toggle(self.expanded, mid))
        self._hand(tag)
        self.tag_bind(tag, "<Enter>", lambda _: self.itemconfigure(tag, fill=T.INDIGO), add="+")
        self.tag_bind(tag, "<Leave>", lambda _: self.itemconfigure(
            tag, fill=T.blend(T.INDIGO, T.CHAT_BG, .3)), add="+")
        return self.bbox(t)[3] + T.S(16)

    def _hand(self, tag, item=None, normal=None, hover=None):
        def enter(_):
            self.configure(cursor="hand2")
            if item:
                self.itemconfigure(item, fill=hover)

        def leave(_):
            self.configure(cursor="")
            if item:
                self.itemconfigure(item, fill=normal)
        self.tag_bind(tag, "<Enter>", enter, add="+")
        self.tag_bind(tag, "<Leave>", leave, add="+")

    def _toggle(self, group, mid):
        group.symmetric_difference_update({mid})
        y = self.y
        self._render()
        self.scroll_to(y, animate=False)

    def _pending(self, y, L):
        """最新来讯还没有候选：在来讯下方显示 AI 生成中的小胶囊。"""
        x = L["px"] + L["av"] + L["gap"]
        paused = self.mode == "paused"
        if paused:
            text, color = "已暂停 · 恢复后才会生成回复", T.AMBER
        elif self.chat["pending"] == "retrying":
            text, color = "等待重试", T.AMBER
        else:
            text, color = ("正在生成候选" if self.mode == "draft" else "正在生成回复"), T.INDIGO
        f = T.font(11.5)
        h = T.S(30)
        dots_w = 0 if paused else T.S(34)
        w = T.S(14) + f.measure(f"{SPARK} {text}") + dots_w + T.S(14)
        y -= T.S(4)
        soft = T.AMBER_SOFT if color == T.AMBER else T.INDIGO_SOFT
        fx.RRect(self, (x, y, x + w, y + h), h / 2, soft,
                 T.blend(soft, color, .18), tags=("msg",))
        cy = y + h / 2
        self.create_text(x + T.S(14), cy - 1, text=f"{SPARK} {text}", font=f, anchor="w",
                         fill=color, tags="msg")
        if not paused:
            r = T.S(2.5)
            cx0 = x + w - T.S(14) - dots_w + T.S(8)
            dots = [self.create_oval(cx0 + k * T.S(9) - r, cy - r, cx0 + k * T.S(9) + r, cy + r,
                                     fill=soft, outline="", tags="msg") for k in range(3)]
            start = [0]

            def tick():
                start[0] += 1
                for k, item in enumerate(dots):
                    v = (math.sin(start[0] * .3 - k * .9) + 1) / 2
                    try:
                        self.itemconfigure(item, fill=T.blend(T.blend(soft, color, .3), color, v))
                    except tk.TclError:
                        return
                self._typing = self.after(50, tick)
            tick()
        return y + h + T.S(16)
