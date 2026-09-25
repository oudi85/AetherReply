"""浅色主题：配色、字体、图标字形与按 DPI 缩放的尺寸。"""

import tkinter.font as tkfont

BG = "#F3F4F6"
SURFACE = "#FFFFFF"
SURFACE_2 = "#F6F7F9"
SURFACE_3 = "#ECEEF1"
HOVER = "#F5F6F8"
SELECTED = "#ECEFF3"
BORDER = "#E4E7EB"
DIVIDER = "#EEF0F3"
TEXT = "#1A1D21"
TEXT_2 = "#464C55"
MUTED = "#858C97"
SUBTLE = "#B2B8C1"
CHAT_BG = "#F5F6F8"
BUBBLE_IN = "#FFFFFF"
BUBBLE_OUT = "#95EC69"
GREEN, GREEN_DEEP, GREEN_SOFT = "#07C160", "#06AD56", "#E6F8EE"
INDIGO, INDIGO_SOFT, VIOLET = "#5B5FEF", "#EFEFFE", "#A26CF9"
AMBER, AMBER_SOFT = "#F59E0B", "#FEF4E3"
RED, RED_SOFT = "#F04438", "#FEEDEB"
BLUE, BLUE_SOFT = "#2F7BF6", "#EAF2FF"

LEVEL = {"ok": GREEN, "warn": AMBER, "bad": RED}
# 状态：前景色、浅底色、标签
STATUS = {
    "sent": (GREEN, GREEN_SOFT, "已发送"),
    "drafted": (INDIGO, INDIGO_SOFT, "已生成"),
    "review": (RED, RED_SOFT, "待核对"),
    "retrying": (AMBER, AMBER_SOFT, "重试中"),
    "waiting": (BLUE, BLUE_SOFT, "待处理"),
    "sending": (BLUE, BLUE_SOFT, "发送中"),
    "paused": (AMBER, AMBER_SOFT, "已暂停"),
    "skipped": (MUTED, SURFACE_3, "已跳过"),
    "done": (MUTED, SURFACE_3, "已完成"),
}
AVATARS = [("#7BE3B4", "#10B981"), ("#9CC4FF", "#3B82F6"), ("#C9B8FF", "#8B5CF6"),
           ("#FFB3A7", "#F2574A"), ("#FFD27A", "#F59E0B"), ("#7FE0EE", "#06B6D4"),
           ("#FFB0D6", "#EC4899"), ("#B7C4D6", "#64748B")]

FONT = "Microsoft YaHei UI"
NUM = "Segoe UI Variable Display"
ICON = "Segoe Fluent Icons"
MONO = "Cascadia Mono"


class I:  # Segoe Fluent Icons / MDL2 字形
    SEARCH = chr(0xE721)
    REFRESH = chr(0xE72C)
    ADD = chr(0xE710)
    DELETE = chr(0xE74D)
    LOG = chr(0xE8A5)
    COPY = chr(0xE8C8)
    CHECK = chr(0xE73E)
    CLOSE = chr(0xE711)
    PLAY = chr(0xE768)
    PAUSE = chr(0xE769)
    CHAT = chr(0xE8BD)
    WARN = chr(0xE7BA)
    FOLDER = chr(0xE838)
    DOWN = chr(0xE70D)
    UP = chr(0xE70E)
    BOT = chr(0xE99A)
    PEOPLE = chr(0xE716)


_factor = 1.0
_fonts: dict = {}


def init(root) -> None:
    global _factor, ICON
    _fonts.clear()  # Font 对象绑定在创建它的 Tk 实例上
    _factor = root.winfo_fpixels("1i") / 96
    families = set(tkfont.families(root))
    if ICON not in families:
        ICON = "Segoe MDL2 Assets"
    for name, fallback in (("NUM", "Segoe UI"), ("MONO", "Consolas")):
        if globals()[name] not in families:
            globals()[name] = fallback


def S(n: float) -> int:
    """96 DPI 下的像素值换算到当前屏幕。"""
    return int(round(n * _factor))


def font(px: float, weight: str = "normal", family: str | None = None) -> tkfont.Font:
    key = (px, weight, family or FONT)
    if key not in _fonts:
        _fonts[key] = tkfont.Font(family=family or FONT, size=-S(px), weight=weight)
    return _fonts[key]


def icon(px: float) -> tkfont.Font:
    return font(px, family=ICON)


def rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def blend(a: str, b: str, t: float, steps: int = 12) -> str:
    """颜色插值；t 量化到有限档位，圆角贴图缓存不会无限增长。"""
    t = round(max(0.0, min(1.0, t)) * steps) / steps
    ca, cb = rgb(a), rgb(b)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def elide(text: str, fnt: tkfont.Font, width: int) -> str:
    text = " ".join((text or "").split())
    if fnt.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fnt.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def initial(name: str) -> str:
    for ch in (name or "").replace("示例·", ""):
        if ch.isalnum():
            return ch.upper()
    return "?"
