"""Inspect the UIA-only route to File Transfer Assistant; never send a message."""

import argparse
import ctypes
import sqlite3
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_gate


TARGET = "文件传输助手"


def find(root, predicate, max_depth=25, max_nodes=3000):
    pending = [(root, 0)]
    visited = 0
    while pending and visited < max_nodes:
        ctrl, depth = pending.pop()
        visited += 1
        try:
            if predicate(ctrl):
                return ctrl
            if depth < max_depth:
                pending.extend((child, depth + 1) for child in ctrl.GetChildren())
        except Exception:
            continue
    return None


def aid_hit(ctrl, short):
    aid = ctrl.AutomationId or ""
    return aid == short or aid.endswith("." + short)


def wait_chat_input(win, label, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            box = find(win, lambda c: aid_hit(c, "chat_input_field"))
            if box is not None and box.Name == label:
                return box
        except Exception:
            pass
        time.sleep(0.2)
    return None


def click_control(ctrl, hwnd):
    """Click the UIA rectangle, temporarily removing Qt render transparency."""
    return click_rect(ctrl.BoundingRectangle, hwnd)


def click_rect(rect, hwnd):
    """Click a rectangle captured before a UIA provider can invalidate it."""
    if rect.width() <= 0 or rect.height() <= 0:
        raise RuntimeError("search result has empty rectangle")
    x = (rect.left + rect.right) // 2
    y = (rect.top + rect.bottom) // 2
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.EnumChildWindows.argtypes = [wintypes.HWND,
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = wintypes.LONG
    user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user.SetWindowLongW.restype = wintypes.LONG
    user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    descendants = []

    def collect(child, _):
        descendants.append(child)
        return True

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(collect)
    user.EnumChildWindows(hwnd, cb, 0)
    saved = []
    try:
        for child in descendants:
            style = user.GetWindowLongW(child, -20) & 0xFFFFFFFF
            if style & 0x20:
                user.SetWindowLongW(child, -20, style & ~0x20)
                saved.append((child, style))
        user.SetCursorPos(x, y)
        time.sleep(0.1)
        user.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user.mouse_event(0x0004, 0, 0, 0, 0)
    finally:
        for child, style in saved:
            user.SetWindowLongW(child, -20, style)


def draft_probe(input_box, hwnd, auto):
    vp = input_box.GetValuePattern()
    if vp is None or vp.IsReadOnly:
        print("输入框不支持可写 ValuePattern；草稿测试跳过")
        return
    if vp.Value:
        print("输入框已有草稿；为避免覆盖，草稿测试跳过")
        return
    input_box.SetFocus()
    try:
        focused = auto.GetFocusedControl()
        focus_ok = aid_hit(focused, "chat_input_field")
    except Exception:
        focus_ok = False
    if not focus_ok:
        click_control(input_box, hwnd)
        time.sleep(0.2)
        try:
            focus_ok = aid_hit(auto.GetFocusedControl(), "chat_input_field")
        except Exception:
            focus_ok = False
    print(f"聊天输入框获得焦点: {focus_ok}")
    if not focus_ok:
        return
    probe_text = "UIA draft test 123"
    try:
        input_box.SendKeys(probe_text, waitTime=0.03)
        time.sleep(0.2)
        print(f"草稿文字读回一致: {vp.Value == probe_text}")
    finally:
        input_box.SendKeys("{Ctrl}a{Delete}", waitTime=0.03)
        time.sleep(0.2)
        print(f"草稿已清空: {not vp.Value}")


def send_self_test(input_box, hwnd, auto):
    """Send exactly once to the verified self-chat, then check the DB mirror."""
    vp = input_box.GetValuePattern()
    if vp is None or vp.IsReadOnly or vp.Value:
        print("输入框不可读写或已有草稿；发送测试取消")
        return
    input_box.SetFocus()
    try:
        focused = aid_hit(auto.GetFocusedControl(), "chat_input_field")
    except Exception:
        focused = False
    if not focused:
        click_control(input_box, hwnd)
        time.sleep(0.2)
        try:
            focused = aid_hit(auto.GetFocusedControl(), "chat_input_field")
        except Exception:
            focused = False
    if not focused or input_box.Name != TARGET:
        print("焦点或会话身份核对失败；发送测试取消")
        return
    project = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project))
    from auto_reply import config
    from auto_reply.wx import sync
    cfg = config.load_config()
    mirror = config.data_dir(cfg) / "auto_reply.db"
    with sqlite3.connect(f"file:{mirror.as_posix()}?mode=ro", uri=True) as con:
        baseline = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    test_text = f"UIA self-test {int(time.time())}"
    vp.SetValue(test_text)
    time.sleep(0.3)
    actual = vp.Value or ""
    if actual != test_text:
        vp.SetValue("")
        print(f"输入内容读回不一致（预期 {len(test_text)} 字符，实际 {len(actual)} 字符，"
              f"读回 {actual!r}）；已清空草稿，发送测试取消")
        return
    input_box.SetFocus()
    try:
        focused = aid_hit(auto.GetFocusedControl(), "chat_input_field")
    except Exception:
        focused = False
    if not focused or input_box.Name != TARGET or vp.Value != test_text:
        vp.SetValue("")
        print("写入后焦点、会话或文字复核失败；已清空草稿，发送测试取消")
        return
    input_box.SendKeys("{Enter}", waitTime=0.05)
    print("已向文件传输助手按一次回车；正在核对本地消息库")
    confirmed = False
    for _ in range(5):
        time.sleep(1)
        try:
            sync.sync_once(cfg, verbose=False)
            with sqlite3.connect(f"file:{mirror.as_posix()}?mode=ro", uri=True) as con:
                confirmed = con.execute(
                    "SELECT 1 FROM messages WHERE id>? AND talker='filehelper' "
                    "AND content=? LIMIT 1",
                    (baseline, test_text)).fetchone() is not None
            if confirmed:
                break
        except Exception as exc:
            print(f"本地同步暂未完成: {type(exc).__name__}")
    if confirmed:
        print("已在本地消息库确认发送成功；不会重试发送")
    else:
        try:
            remaining = vp.Value or ""
        except Exception:
            remaining = ""
        if remaining.startswith(test_text):
            vp.SetValue("")
            print("回车没有发送，已清空测试草稿；不会重试")
        else:
            print("尚未从本地消息库确认，发送结果不确定；不会重试")


def main():
    import psutil
    import uiautomation as auto

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true",
                        help="invoke the unique File Transfer Assistant result; no text input or send")
    parser.add_argument("--click-only", action="store_true",
                        help="open File Transfer Assistant by clicking a fresh UIA result rectangle")
    parser.add_argument("--draft", action="store_true",
                        help="open File Transfer Assistant, write and clear a test draft; never press Enter")
    parser.add_argument("--send-self-test", action="store_true",
                        help="send one unique test message only to File Transfer Assistant")
    args = parser.parse_args()
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() != "weixin.exe":
            continue
        hwnd = probe_gate.main_window(proc.pid)
        if hwnd is None:
            continue
        if not probe_gate.materialized(hwnd):
            print("mmui 控件树未激活；先运行 probe_gate.py --activate。")
            return
        win = auto.ControlFromHandle(hwnd)
        user = probe_gate.ctypes.windll.user32
        user.SetForegroundWindow.argtypes = [probe_gate.wintypes.HWND]
        user.GetForegroundWindow.restype = probe_gate.wintypes.HWND
        try:
            win.SetActive()
        except Exception:
            pass
        user.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        print(f"微信主窗口已置前: {user.GetForegroundWindow() == hwnd}")
        search = find(win, lambda c: c.ClassName == "mmui::XValidatorTextEdit"
                      and c.ControlTypeName == "EditControl" and c.Name == "搜索")
        if search is None:
            print("未找到搜索框")
            return
        pattern = search.GetValuePattern()
        if pattern is None or pattern.IsReadOnly:
            print("搜索框不支持 UIA ValuePattern.SetValue")
            return
        try:
            search.SetFocus()
        except Exception:
            pass
        pattern.SetValue(TARGET)
        try:
            time.sleep(0.8)
            try:
                actual = pattern.Value or ""
                print(f"搜索框写入后内容长度: {len(actual)}；与关键词一致: {actual == TARGET}")
            except Exception:
                print("搜索框 ValuePattern 读回失败")
            deadline = time.monotonic() + 5
            results = []
            list_source = "none"
            child_aids = []
            while time.monotonic() < deadline:
                lst = find(win, lambda c: aid_hit(c, "search_list"), max_nodes=1500)
                if lst is not None:
                    list_source = "main_window"
                if lst is None:
                    lst = auto.ListControl(searchDepth=0xFFFFFFFF, AutomationId="search_list")
                    if lst.Exists(0.3, 0.1):
                        list_source = "global_exact"
                    else:
                        lst = None
                if lst is None:
                    lst = find(auto.GetRootControl(), lambda c: aid_hit(c, "search_list"),
                           max_depth=22, max_nodes=4000)
                    if lst is not None:
                        list_source = "desktop_tree"
                if lst is not None:
                    children = lst.GetChildren()
                    child_aids = [(child.AutomationId or "").rsplit(".", 1)[-1]
                                  for child in children[:30]]
                    results = [child for child in children
                               if (child.AutomationId or "").startswith("search_item_")]
                    if results:
                        break
                time.sleep(0.2)
            print(f"搜索列表定位来源: {list_source}")
            if list_source != "none":
                print(f"搜索列表子控件标识（最多 30 个）: {child_aids}")
            exact = [r for r in results if (r.Name or "").split("\n", 1)[0].strip() == TARGET]
            print(f"搜索结果 {len(results)} 个；名称完全匹配“文件传输助手” {len(exact)} 个")
            if not results:
                aids = []
                pending = [(win, 0)]
                while pending and len(aids) < 30:
                    ctrl, depth = pending.pop()
                    try:
                        aid = ctrl.AutomationId or ""
                        if "search" in aid.lower():
                            aids.append(aid.rsplit(".", 1)[-1])
                        if depth < 20:
                            pending.extend((child, depth + 1) for child in ctrl.GetChildren())
                    except Exception:
                        continue
                print(f"主窗口中与搜索相关的控件标识: {aids}")
            if len(exact) == 1:
                item = exact[0]
                result_rect = item.BoundingRectangle
                try:
                    invoke = item.GetInvokePattern()
                    invokable = invoke is not None
                except Exception:
                    invoke = None
                    invokable = False
                print(f"唯一结果支持 InvokePattern: {invokable}")
                if args.open or args.click_only or args.draft or args.send_self_test:
                    click_rect(result_rect, hwnd)
                    opened_input = wait_chat_input(win, TARGET, timeout=4.0)
                    if args.click_only:
                        print(f"直接点击后目标名称匹配: {opened_input is not None}")
                    chat_name = opened_input.Name if opened_input is not None else ""
                    print(f"打开后输入框可定位: {opened_input is not None}；目标名称匹配: {chat_name == TARGET}")
                    if chat_name == TARGET and args.draft:
                        draft_probe(opened_input, hwnd, auto)
                    if chat_name == TARGET and args.send_self_test:
                        send_self_test(opened_input, hwnd, auto)
            input_box = find(win, lambda c: aid_hit(c, "chat_input_field"))
            print(f"当前聊天输入框仍可定位: {input_box is not None}")
        finally:
            try:
                pattern.SetValue("")
            except Exception:
                pass
        return
    print("当前交互桌面未找到微信主窗口")


if __name__ == "__main__":
    main()
