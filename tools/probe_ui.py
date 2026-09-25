"""只读探测微信顶层窗口和辅助功能控件类型，不读取聊天文字。"""

import ctypes
import sys
import tempfile
from collections import Counter
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_reply.wx.paths import list_wechat_pids


def main():
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.EnumChildWindows.argtypes = [
        wintypes.HWND,
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM]
    pids = set(list_wechat_pids())
    found = []
    total = [0]

    def callback(hwnd, _):
        total[0] += 1
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append((hwnd, bool(user32.IsWindowVisible(hwnd)), name.value,
                          (rect.right - rect.left, rect.bottom - rect.top)))
        return True

    enum_callback = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
    user32.EnumWindows.argtypes = [type(enum_callback), wintypes.LPARAM]
    user32.EnumWindows(enum_callback, 0)
    print(f"微信进程 {len(pids)} 个；可见顶层窗口 "
          f"{sum(visible for _, visible, _, _ in found)} 个；桌面窗口总数 {total[0]} 个")
    if total[0] == 0:
        print("当前执行环境不能访问交互桌面；请在已登录的 Windows 桌面终端运行本命令。")
        return

    try:
        import uiautomation as auto
    except ImportError:
        print("缺少 uiautomation，安装 requirements.txt 后重试。")
        return
    for hwnd, visible, class_name, size in found:
        if not visible:
            continue
        child_classes = Counter()
        child_windows = []

        def child_callback(child_hwnd, _):
            child_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_hwnd, child_name, 256)
            child_classes[child_name.value] += 1
            child_windows.append((child_hwnd, child_name.value))
            return True

        child_enum = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(child_callback)
        user32.EnumChildWindows(hwnd, child_enum, 0)
        counts = Counter()
        frontier = [auto.ControlFromHandle(hwnd)]
        for _ in range(5):
            next_level = []
            for control in frontier:
                try:
                    children = control.GetChildren()
                except Exception:
                    continue
                for child in children:
                    counts[child.ControlTypeName] += 1
                    next_level.append(child)
            frontier = next_level[:1000]
        print(f"窗口类型 {class_name}，尺寸 {size}，"
              f"Win32 子窗口类型 {dict(child_classes)}，UIA 控件类型 {dict(counts)}")
        _probe_msaa(hwnd)
        for child_hwnd, child_class in child_windows[:10]:
            child_counts = Counter()
            frontier = [auto.ControlFromHandle(child_hwnd)]
            for _ in range(6):
                next_level = []
                for control in frontier:
                    try:
                        descendants = control.GetChildren()
                    except Exception:
                        continue
                    for descendant in descendants:
                        child_counts[descendant.ControlTypeName] += 1
                        next_level.append(descendant)
                frontier = next_level[:1000]
            print(f"子窗口 {child_class}：UIA 控件类型 {dict(child_counts)}")
            _probe_msaa(child_hwnd)


def _probe_msaa(hwnd):
    """只读取 MSAA 角色和子节点数量，不读取 Name/Value/Description。"""
    try:
        import comtypes
        import comtypes.client
        from comtypes.automation import VARIANT

        comtypes.client.gen_dir = tempfile.mkdtemp(prefix="auto_reply_msaa_")
        msaa = comtypes.client.GetModule("oleacc.dll")
        acc = comtypes.POINTER(msaa.IAccessible)()
        iid = msaa.IAccessible._iid_
        oleacc = ctypes.WinDLL("oleacc")
        get_object = oleacc.AccessibleObjectFromWindow
        get_object.argtypes = [wintypes.HWND, wintypes.DWORD,
                               ctypes.POINTER(comtypes.GUID),
                               ctypes.POINTER(ctypes.POINTER(msaa.IAccessible))]
        get_object.restype = wintypes.LONG
        hr = get_object(hwnd, 0xFFFFFFFC, ctypes.byref(iid), ctypes.byref(acc))
        if hr != 0:
            print(f"MSAA: 未提供客户端对象（状态 {hr}）")
            return
        children = oleacc.AccessibleChildren
        children.argtypes = [ctypes.POINTER(msaa.IAccessible), wintypes.LONG,
                             wintypes.LONG, ctypes.POINTER(VARIANT),
                             ctypes.POINTER(wintypes.LONG)]
        children.restype = wintypes.LONG
        role_counts = Counter()
        variant_types = Counter()
        queue = [(acc, 0)]
        visited = 0
        while queue and visited < 1000:
            current, depth = queue.pop(0)
            visited += 1
            try:
                child_count = min(int(current.accChildCount), 200)
            except Exception as exc:
                role_counts[f"child_count_error:{type(exc).__name__}"] += 1
                continue
            if not child_count or depth >= 6:
                continue
            variants = (VARIANT * child_count)()
            obtained = wintypes.LONG()
            hr = children(current, 0, child_count, variants, ctypes.byref(obtained))
            if hr not in (0, 1):
                role_counts[f"enumeration_error:{hr}"] += 1
                continue
            for item in variants[:obtained.value]:
                variant_types[str(item.vt)] += 1
                try:
                    if item.vt == 3:  # VT_I4：同一 IAccessible 对象下的子 ID
                        role = current.accRole[int(item.value)]
                    elif item.vt == 9:  # VT_DISPATCH：独立子对象
                        child = item.value.QueryInterface(msaa.IAccessible)
                        role = child.accRole[0]
                        queue.append((child, depth + 1))
                    else:
                        continue
                    role_counts[str(role)] += 1
                except Exception as exc:
                    role_counts[f"role_error:{type(exc).__name__}"] += 1
        print(f"MSAA 遍历对象 {visited} 个，节点类型 {dict(variant_types)}，"
              f"角色统计 {dict(role_counts)}")
    except Exception as exc:
        print(f"MSAA 探测失败：{type(exc).__name__}")


if __name__ == "__main__":
    main()
