"""共用一个定时器的补间动画；同一 key 的新动画会替换旧动画。"""

import time
import tkinter as tk

_root = None
_jobs: dict = {}
_ticking = False


def init(root) -> None:
    global _root
    _root = root


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2


def animate(key, ms: int, step, done=None, ease=ease_out) -> None:
    global _ticking
    _jobs[key] = (time.perf_counter(), max(ms, 1) / 1000, step, done, ease)
    if not _ticking and _root is not None:
        _ticking = True
        _root.after(0, _tick)


def cancel(key) -> None:
    _jobs.pop(key, None)


def _tick() -> None:
    global _ticking
    now = time.perf_counter()
    for key, (start, dur, step, done, ease) in list(_jobs.items()):
        t = min(1.0, (now - start) / dur)
        try:
            step(ease(t))
            if t >= 1:
                if _jobs.get(key, (None,))[0] == start:
                    del _jobs[key]
                if done:
                    done()
        except tk.TclError:  # 控件已销毁
            _jobs.pop(key, None)
    if _jobs:
        _root.after(15, _tick)
    else:
        _ticking = False
