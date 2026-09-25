"""用 Pillow 生成抗锯齿圆角、柔和阴影与光晕贴图；Tk 画布本身不做抗锯齿。

带透明度的贴图都由“整幅纯色/渐变 RGB + 抗锯齿 alpha 蒙版”合成，边缘不会发黑。
"""

from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

from . import theme as T

_SS = 4
_corner_cache: dict = {}
_images: OrderedDict = OrderedDict()


def reset() -> None:
    """PhotoImage 绑定在创建它的 Tk 实例上；新建根窗口前清空缓存。"""
    _images.clear()
    _corner_cache.clear()


def _remember(key, factory, limit=160):
    if key in _images:
        _images.move_to_end(key)
        return _images[key]
    img = factory()
    _images[key] = img
    while len(_images) > limit:
        _images.popitem(last=False)
    return img


@lru_cache(maxsize=512)
def _circle_mask(d: int) -> Image.Image:
    big = Image.new("L", (d * _SS, d * _SS), 0)
    ImageDraw.Draw(big).ellipse((0, 0, d * _SS - 1, d * _SS - 1), fill=255)
    return big.resize((d, d), Image.LANCZOS)


def _rr_mask(size, box, r) -> Image.Image:
    w, h = size
    x0, y0, x1, y1 = (int(v) for v in box)
    r = max(1, min(int(r), (x1 - x0) // 2, (y1 - y0) // 2))
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    if x1 - r - 1 >= x0 + r:
        d.rectangle((x0 + r, y0, x1 - r - 1, y1 - 1), fill=255)
    if y1 - r - 1 >= y0 + r:
        d.rectangle((x0, y0 + r, x1 - 1, y1 - r - 1), fill=255)
    c = _circle_mask(2 * r)
    m.paste(c.crop((0, 0, r, r)), (x0, y0))
    m.paste(c.crop((r, 0, 2 * r, r)), (x1 - r, y0))
    m.paste(c.crop((0, r, r, 2 * r)), (x0, y1 - r))
    m.paste(c.crop((r, r, 2 * r, 2 * r)), (x1 - r, y1 - r))
    return m


def _shadow_mask(size, box, r, blur, alpha, dy) -> Image.Image:
    w, h = size
    k = 3
    sm = _rr_mask((w // k + 1, h // k + 1),
                  (box[0] // k, (box[1] + dy) // k, box[2] // k, (box[3] + dy) // k),
                  max(1, r // k))
    sm = sm.filter(ImageFilter.GaussianBlur(max(blur / k, .5))).resize((w, h), Image.BILINEAR)
    return sm.point(lambda v: v * alpha // 255)


def _rgba(fill_rgb: Image.Image, alpha: Image.Image) -> Image.Image:
    img = fill_rgb.copy()
    img.putalpha(alpha)
    return img


def _vgradient(size, top: str, bottom: str) -> Image.Image:
    w, h = size
    grad = Image.linear_gradient("L").resize((w, h))
    return Image.composite(Image.new("RGB", size, T.rgb(bottom)),
                           Image.new("RGB", size, T.rgb(top)), grad)


# ---------- 画布上的圆角矩形（九宫格：四角贴图 + 两个矩形） ----------
def _corners(r: int, fill: str, outline: str | None):
    key = (r, fill, outline)
    if key not in _corner_cache:
        d = 2 * r
        alpha = _circle_mask(d)
        if outline:
            inner = Image.new("L", (d, d), 0)
            inner.paste(_circle_mask(d - 2), (1, 1))
            color = Image.composite(Image.new("RGB", (d, d), T.rgb(fill)),
                                    Image.new("RGB", (d, d), T.rgb(outline)), inner)
        else:
            color = Image.new("RGB", (d, d), T.rgb(fill))
        img = _rgba(color, alpha)
        _corner_cache[key] = [ImageTk.PhotoImage(img.crop(b)) for b in
                              ((0, 0, r, r), (r, 0, d, r), (0, r, r, d), (r, r, d, d))]
    return _corner_cache[key]


class RRect:
    """画布上的抗锯齿圆角矩形，可改色、移动；所有子项共用一个标签。"""
    _seq = 0

    def __init__(self, cv, box, r, fill, outline=None, tags=()):
        RRect._seq += 1
        self.cv, self.tag = cv, f"rr{RRect._seq}"
        self.tags = (self.tag, *tags) if isinstance(tags, tuple) else (self.tag, tags)
        self.r, self.fill, self.outline = r, fill, outline
        self.items, self.visible = [], True
        self.place(box)

    def place(self, box):
        cv = self.cv
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        r = max(0, min(int(self.r), (x1 - x0) // 2, (y1 - y0) // 2))
        self.box, self._r = (x0, y0, x1, y1), r
        coords = [(x0 + r, y0, x1 - r, y1), (x0, y0 + r, x1, y1 - r)]
        pts = [(x0, y0), (x1 - r, y0), (x0, y1 - r), (x1 - r, y1 - r)]
        lines = [(x0 + r, y0, x1 - r, y0), (x0 + r, y1 - 1, x1 - r, y1 - 1),
                 (x0, y0 + r, x0, y1 - r), (x1 - 1, y0 + r, x1 - 1, y1 - r)]
        if not self.items:
            self.rects = [cv.create_rectangle(*c, fill=self.fill, width=0, tags=self.tags)
                          for c in coords]
            self.imgs = [cv.create_image(*p, anchor="nw", tags=self.tags) for p in pts]
            self.lines = [cv.create_line(*ln, fill=self.outline or self.fill, tags=self.tags,
                                         state="normal" if self.outline else "hidden")
                          for ln in lines]
            self.items = self.rects + self.imgs + self.lines
            self._paint()
        else:
            for item, c in zip(self.rects, coords):
                cv.coords(item, *c)
            for item, p in zip(self.imgs, pts):
                cv.coords(item, *p)
            for item, ln in zip(self.lines, lines):
                cv.coords(item, *ln)
            if r != self._painted_r:
                self._paint()

    def _paint(self):
        self._painted_r = self._r
        shown = "normal" if self.visible else "hidden"
        corners = _corners(self._r, self.fill, self.outline) if self._r > 0 else None
        for i, item in enumerate(self.imgs):
            if corners:
                self.cv.itemconfigure(item, image=corners[i], state=shown)
            else:
                self.cv.itemconfigure(item, state="hidden")
        for item in self.rects:
            self.cv.itemconfigure(item, fill=self.fill)
        for item in self.lines:
            self.cv.itemconfigure(item, fill=self.outline or self.fill,
                                  state=shown if self.outline else "hidden")

    def set(self, fill=None, outline=...):
        changed = False
        if fill is not None and fill != self.fill:
            self.fill, changed = fill, True
        if outline is not ... and outline != self.outline:
            self.outline, changed = outline, True
        if changed:
            self._paint()

    def lower(self):
        self.cv.tag_lower(self.tag)

    def state(self, visible: bool):
        if visible != self.visible:
            self.visible = visible
            for item in self.rects:
                self.cv.itemconfigure(item, state="normal" if visible else "hidden")
            self._paint()

    def delete(self):
        self.cv.delete(self.tag)


# ---------- 整块贴图 ----------
def panel(w, h, *, margin, radius, fill, bg, border=T.BORDER, fill2=None, split=None,
          shadow=True, glow=None):
    """卡片背景：柔和投影 + 1px 描边 + 圆角填充；split 以下改用 fill2（聊天区）。"""
    key = ("panel", w, h, margin, radius, fill, bg, border, fill2, split, shadow, glow)

    def make():
        size = (w, h)
        base = Image.new("RGB", size, T.rgb(bg))
        box = (margin, margin, w - margin, h - margin)
        if shadow:
            color = T.rgb(glow) if glow else (22, 30, 45)
            base.paste(color, (0, 0, w, h),
                       _shadow_mask(size, box, radius, T.S(10), 40 if glow else 24, T.S(4)))
            base.paste(color, (0, 0, w, h),
                       _shadow_mask(size, box, radius, T.S(1.5), 16, T.S(1)))
        base.paste(T.rgb(border), (0, 0, w, h), _rr_mask(size, box, radius))
        inner = _rr_mask(size, (box[0] + 1, box[1] + 1, box[2] - 1, box[3] - 1), radius - 1)
        base.paste(T.rgb(fill), (0, 0, w, h), inner)
        if fill2 and split:
            lower = inner.copy()
            ImageDraw.Draw(lower).rectangle((0, 0, w, split), fill=0)
            base.paste(T.rgb(fill2), (0, 0, w, h), lower)
            ImageDraw.Draw(base).line((box[0] + 1, split, box[2] - 2, split), fill=T.rgb(T.DIVIDER))
        return ImageTk.PhotoImage(base)
    return _remember(key, make, limit=24)


def ai_card(w, h, *, bg, accent=(T.INDIGO, T.VIOLET), glow=T.INDIGO):
    """聊天流里的 AI 回复卡片：带主题色光晕和左侧渐变光条。返回 (图像, 外边距)。"""
    g = T.S(14)
    key = ("ai", w, h, bg, accent, glow)

    def make():
        size = (w + 2 * g, h + 2 * g)
        base = Image.new("RGB", size, T.rgb(bg))
        box = (g, g, g + w, g + h)
        r = T.S(14)
        base.paste(T.rgb(glow), (0, 0, *size), _shadow_mask(size, box, r, T.S(12), 46, T.S(4)))
        base.paste(T.rgb(T.blend(glow, "#FFFFFF", .78)), (0, 0, *size), _rr_mask(size, box, r))
        base.paste((255, 255, 255), (0, 0, *size),
                   _rr_mask(size, (box[0] + 1, box[1] + 1, box[2] - 1, box[3] - 1), r - 1))
        bar_w, pad = T.S(3), T.S(16)
        bar_h = max(1, h - 2 * pad)
        bar = _vgradient((bar_w, bar_h), *accent)
        base.paste(bar, (g + T.S(6), g + pad),
                   _rr_mask((bar_w, bar_h), (0, 0, bar_w, bar_h), bar_w // 2 + 1))
        return ImageTk.PhotoImage(base)
    return _remember(key, make), g


def avatar(d: int, palette: int, mine: bool = False):
    key = ("avatar", d, palette, mine)

    def make():
        top, bottom = ("#4ADE80", T.GREEN) if mine else T.AVATARS[palette % len(T.AVATARS)]
        return ImageTk.PhotoImage(_rgba(_vgradient((d, d), top, bottom), _circle_mask(d)))
    return _remember(key, make)


def dot(d: int, fill: str, ring: str | None = None, ring_w: int = 0):
    key = ("dot", d, fill, ring, ring_w)

    def make():
        if ring:
            inner = Image.new("L", (d, d), 0)
            inner.paste(_circle_mask(d - 2 * ring_w), (ring_w, ring_w))
            color = Image.composite(Image.new("RGB", (d, d), T.rgb(fill)),
                                    Image.new("RGB", (d, d), T.rgb(ring)), inner)
        else:
            color = Image.new("RGB", (d, d), T.rgb(fill))
        return ImageTk.PhotoImage(_rgba(color, _circle_mask(d)))
    return _remember(key, make)


def glow_frames(color: str, d: int, n: int = 28):
    """呼吸光点：核心实心圆 + 向外扩散淡出的光环。"""
    key = ("glow", color, d, n)

    def make():
        frames = []
        core = max(2, int(d * .36))
        core_mask = Image.new("L", (d, d), 0)
        core_mask.paste(_circle_mask(core), ((d - core) // 2, (d - core) // 2))
        solid = Image.new("RGB", (d, d), T.rgb(color))
        for i in range(n):
            p = i / n
            ring = max(core, int(core + (d - core) * p))
            halo = Image.new("L", (d, d), 0)
            halo.paste(_circle_mask(ring), ((d - ring) // 2, (d - ring) // 2))
            halo = halo.point(lambda v, a=(1 - p) ** 1.6 * 150: int(v * a / 255))
            frames.append(ImageTk.PhotoImage(_rgba(solid, _max(halo, core_mask))))
        return frames
    return _remember(key, make)


def _max(a: Image.Image, b: Image.Image) -> Image.Image:
    return ImageChops.lighter(a, b)


def knob(d: int):
    """开关旋钮：白色圆 + 轻微投影；贴图比旋钮大 2*pad。"""
    pad = T.S(2)
    key = ("knob", d, pad)

    def make():
        size = d + 2 * pad
        core = Image.new("L", (size, size), 0)
        core.paste(_circle_mask(d), (pad, pad))
        shadow = Image.new("L", (size, size), 0)
        shadow.paste(_circle_mask(d), (pad, pad + 1))
        shadow = shadow.filter(ImageFilter.GaussianBlur(pad * .8)).point(lambda v: v * 70 // 255)
        color = Image.composite(Image.new("RGB", (size, size), (255, 255, 255)),
                                Image.new("RGB", (size, size), (20, 28, 40)), core)
        return ImageTk.PhotoImage(_rgba(color, _max(core, shadow)))
    return _remember(key, make), pad


def logo(d: int):
    """从 Windows 图标载入同一品牌图案，供标题栏和窗口使用。"""
    key = ("logo", d)

    def make():
        g = max(2, d // 5)
        icon_path = Path(__file__).resolve().parents[2] / "assets" / "auto_reply.ico"
        with Image.open(icon_path) as icon:
            return icon.convert("RGBA").resize((d + 2 * g, d + 2 * g), Image.LANCZOS)
    return _remember(key, make)


def logo_photo(d: int, bg: str | None = None):
    """logo 贴到指定底色上（画布贴图）；bg 为 None 时保留透明（窗口图标）。"""
    key = ("logo_tk", d, bg)

    def make():
        img = logo(d)
        if bg:
            base = Image.new("RGB", img.size, T.rgb(bg))
            base.paste(img, (0, 0), img)
            img = base
        return ImageTk.PhotoImage(img)
    return _remember(key, make)


# ---------- Windows 11 窗口外观 ----------
def _dwm(win, attr: int, value: int) -> None:
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:  # 旧系统或非 Windows 忽略
        pass


def _colorref(color: str) -> int:
    r, g, b = T.rgb(color)
    return r | (g << 8) | (b << 16)


def round_corners(win) -> None:
    win.update_idletasks()
    _dwm(win, 33, 2)


def set_caption(win, color: str, text_color: str = T.TEXT) -> None:
    win.update_idletasks()
    _dwm(win, 35, _colorref(color))
    _dwm(win, 36, _colorref(text_color))
    _dwm(win, 34, _colorref(color))  # 边框同色，窗口更干净
