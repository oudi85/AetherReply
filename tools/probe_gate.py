"""Probe the Qt accessibility gate; activation is explicit and sends nothing."""

import argparse
import ctypes
import re
import struct
import time
from ctypes import wintypes
from pathlib import Path

import psutil


PATTERN = re.compile(rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})\x00\x0f\x84", re.DOTALL)


def sections(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise RuntimeError("invalid PE")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    start = pe + 24 + optional_size
    result = []
    for i in range(count):
        off = start + 40 * i
        name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        virtual_size, virtual_rva, raw_size, raw_off = struct.unpack_from("<IIII", data, off + 8)
        flags = struct.unpack_from("<I", data, off + 36)[0]
        result.append((name, flags, virtual_rva, max(virtual_size, raw_size), raw_off, raw_size))
    return result


def section_for_rva(all_sections, rva):
    for name, flags, virtual_rva, size, raw_off, _ in all_sections:
        if virtual_rva <= rva < virtual_rva + size:
            return name, flags, raw_off + (rva - virtual_rva)
    raise RuntimeError("RVA outside sections")


def rva_from_offset(all_sections, offset):
    for _, _, virtual_rva, _, raw_off, raw_size in all_sections:
        if raw_off <= offset < raw_off + raw_size:
            return virtual_rva + offset - raw_off
    return None


def gate_candidates(data, all_sections):
    """Find writable, initialized screen-reader flags referenced by the Qt gate."""
    matches = set()
    for match in PATTERN.finditer(data):
        cmp_rva = rva_from_offset(all_sections, match.start("disp") - 2)
        if cmp_rva is None:
            continue
        disp = struct.unpack("<i", match.group("disp"))[0]
        rva = cmp_rva + 7 + disp
        try:
            sec, flags, offset = section_for_rva(all_sections, rva)
        except RuntimeError:
            continue
        if sec == ".data" and flags & 0x80000000 and offset < len(data) and data[offset] == 1:
            matches.add(rva)
    return matches


def discover_gate(data):
    all_sections = sections(data)
    candidates = gate_candidates(data, all_sections)
    if len(candidates) != 1:
        raise RuntimeError(f"辅助功能 gate 特征不唯一（候选 {len(candidates)} 个）")
    return candidates.pop()


def module_location(proc):
    modules = [item for item in proc.memory_maps(grouped=False)
               if Path(item.path).name.lower() == "weixin.dll"]
    paths = {Path(item.path).resolve() for item in modules}
    if len(paths) != 1:
        raise RuntimeError("Weixin.dll 映射路径不唯一")
    path = paths.pop()
    return path, min(int(item.addr, 16) for item in modules)


def process_byte(pid, addr):
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise ctypes.WinError()
    try:
        result = ctypes.c_ubyte()
        read = ctypes.c_size_t()
        ok = kernel.ReadProcessMemory(handle, addr, ctypes.byref(result), 1,
                                      ctypes.byref(read))
        if not ok or read.value != 1:
            raise ctypes.WinError()
        return result.value
    finally:
        kernel.CloseHandle(handle)


def write_process_byte(pid, addr, value):
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_size_t)]
    kernel.WriteProcessMemory.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
    if not handle:
        raise ctypes.WinError()
    try:
        result = ctypes.c_ubyte(value)
        written = ctypes.c_size_t()
        ok = kernel.WriteProcessMemory(handle, addr, ctypes.byref(result), 1,
                                       ctypes.byref(written))
        if not ok or written.value != 1:
            raise ctypes.WinError()
    finally:
        kernel.CloseHandle(handle)


def main_window(pid):
    user = ctypes.windll.user32
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    candidates = []

    def callback(hwnd, _):
        window_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, 256)
            if name.value == "Qt51514QWindowIcon":
                rect = wintypes.RECT()
                user.GetWindowRect(hwnd, ctypes.byref(rect))
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
                candidates.append((area, hwnd))
        return True

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
    user.EnumWindows.argtypes = [type(cb), wintypes.LPARAM]
    user.EnumWindows(cb, 0)
    return max(candidates)[1] if candidates else None


def materialized(hwnd):
    import uiautomation as auto
    try:
        root = auto.ControlFromHandle(hwnd)
        if (root.ClassName or "").startswith("mmui::"):
            return True
        return any((child.ClassName or "").startswith("mmui::")
                   for child in root.GetChildren())
    except Exception:
        return False


def probe_anchors(hwnd):
    import uiautomation as auto
    found = {"main_window": False, "search_box": False, "chat_input": False}
    pending = [(auto.ControlFromHandle(hwnd), 0)]
    visited = 0
    while pending and visited < 1500:
        control, depth = pending.pop()
        visited += 1
        try:
            cls = control.ClassName or ""
            aid = (control.AutomationId or "").split(".")[-1]
            found["main_window"] |= cls == "mmui::MainWindow"
            found["search_box"] |= cls == "mmui::XValidatorTextEdit"
            found["chat_input"] |= aid == "chat_input_field"
            if all(found.values()):
                break
            if depth < 25:
                pending.extend((child, depth + 1) for child in control.GetChildren())
        except Exception:
            continue
    print(f"UIA 锚点: {found}，检查控件 {visited} 个")


def activation_probe(pid, address, original):
    hwnd = main_window(pid)
    if hwnd is None:
        print("未找到当前交互桌面上的微信主窗口；请在微信主窗口可见时运行。")
        return
    if materialized(hwnd):
        print("mmui 控件树已出现，无需激活。")
        probe_anchors(hwnd)
        return
    if original not in (0, 1):
        raise RuntimeError("unexpected gate byte; activation aborted")
    wrote = False
    try:
        if original == 0:
            write_process_byte(pid, address, 1)
            wrote = True
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if materialized(hwnd):
                print("mmui 控件树已出现；无 OCR 发送路径具备控件基础。")
                probe_anchors(hwnd)
                return
            time.sleep(0.2)
        print("mmui 控件树仍未出现。")
    finally:
        if wrote and not materialized(hwnd):
            write_process_byte(pid, address, original)
            print("已恢复原始 gate 字节。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true",
                        help="write the verified one-byte gate and check UIA; rollback on failure")
    args = parser.parse_args()
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() != "weixin.exe":
            continue
        try:
            dll_path, base = module_location(proc)
        except (psutil.AccessDenied, psutil.NoSuchProcess, RuntimeError):
            continue
        data = dll_path.read_bytes()
        rva = discover_gate(data)
        live = process_byte(proc.pid, base + rva)
        print(f"Weixin.dll path={dll_path} size={len(data)}")
        print(f"discovered gate RVA=0x{rva:x}")
        print(f"live_gate_byte={live}")
        if args.activate:
            activation_probe(proc.pid, base + rva, live)
        return
    print("matching Weixin.dll not found")


if __name__ == "__main__":
    main()
