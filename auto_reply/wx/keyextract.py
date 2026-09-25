"""从运行中的 Weixin.exe 进程内存提取数据库密钥。

微信 4.0.x：WCDB 在内存里缓存 x'<64位hex密钥><32位hex盐>' 的 ASCII 串，正则即得。
微信 4.1+：ASCII 缓存消失，按成本递增依次尝试：
  1) ASCII x'<96hex>'
  2) UTF-16LE 形式的同一模式
  3) 搜索每库独有的 16 字节 salt 原始字节 —— per-db 配置结构体里 salt 与
     派生密钥几乎必然同处 512 字节内，命中后只验证附近窗口（极快）
  4) 兜底：私有内存 8 字节对齐暴力扫描，单块 AES 校验 page1 头（多进程）
所有命中都经 page1 HMAC 验证后才采纳。
"""

import ctypes
import ctypes.wintypes as wt
import json
import multiprocessing as mp
import re
import struct
import time
from pathlib import Path

kernel32 = ctypes.windll.kernel32

PROCESS_READ = 0x0010 | 0x0400  # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
READABLE_PROTECT = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
HMAC_SZ = 64

_HEX_KEY_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
_HEX_KEY_WIDE_RE = re.compile(rb"x\x00'\x00(?:[0-9a-fA-F]\x00){96}'\x00")
_ZERO32 = b"\x00" * KEY_SZ
_HEADER = b"SQLite format 3\x00"
_CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
_CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("_pad2", wt.DWORD), ("Type", wt.DWORD),
    ]


def _read_mem(handle, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(handle, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def _enum_regions(handle):
    regions, addr, mbi = [], 0, _MBI()
    while addr < 0x7FFFFFFFFFFF:
        if not kernel32.VirtualQueryEx(handle, ctypes.c_uint64(addr),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE_PROTECT
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regions


# ----------------------------- 验证原语 -----------------------------

def hmac_ok(enc_key: bytes, page1: bytes) -> bool:
    """page1 HMAC 验证：这把密钥是否属于该库（最终裁决）。"""
    import hashlib
    import hmac as hmac_mod
    if len(page1) < PAGE_SZ or len(enc_key) != KEY_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + 16]
    stored = page1[PAGE_SZ - HMAC_SZ:]
    h = hmac_mod.new(mac_key, data, hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == stored


def page1_material(page1: bytes) -> tuple[bytes, bytes, bytes]:
    """(salt, iv, c1)：c1 解密后应为 SQLite 头 16 字节。"""
    return (page1[:SALT_SZ],
            page1[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16],
            page1[SALT_SZ: SALT_SZ + 16])


# ----------------------------- 四层策略 -----------------------------

def _iter_mem(handle):
    for base, size in _enum_regions(handle):
        data = _read_mem(handle, base, size)
        if data:
            yield data


def _find_bytes(handle, needle: bytes):
    """在可读区域按块查找，返回远程地址，不保留整份进程内存。"""
    overlap = len(needle) - 1
    for base, size in _enum_regions(handle):
        pos = 0
        while pos < size:
            data = _read_mem(handle, base + pos, min(8 * 1024 * 1024, size - pos))
            if not data:
                break
            start = 0
            while (hit := data.find(needle, start)) >= 0:
                yield base + pos + hit
                start = hit + 1
            if pos + len(data) >= size:
                break
            pos += max(1, len(data) - overlap)


def _strategy_config_cipher(handle, page1s, already):
    """微信 4.1.x：沿 WCDB Config.Cipher 指针找到异或编码的逐库密钥。"""
    found = {}
    anchors = list(_find_bytes(handle, _CONFIG_CIPHER_NAME))
    for anchor in anchors:
        pair = struct.pack("<QQ", anchor, len(_CONFIG_CIPHER_NAME))
        for address in _find_bytes(handle, pair):
            node = _read_mem(handle, address - 0x10, 0x50)
            if not node or len(node) < 0x40:
                continue
            config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
            if not 0x10000 <= config_ptr < 0x800000000000:
                continue
            obj = _read_mem(handle, config_ptr + 0x88, 0x28)
            if not obj or len(obj) < 0x18:
                continue
            data_ptr, data_len = struct.unpack_from("<QQ", obj, 0x8)
            if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                continue
            blob = _read_mem(handle, data_ptr, data_len)
            if not blob or len(blob) != data_len:
                continue
            decoded = bytes(v ^ _CONFIG_XOR_MASK[i % len(_CONFIG_XOR_MASK)]
                            for i, v in enumerate(blob))
            for match in _HEX_KEY_RE.finditer(decoded):
                run = match.group(1)
                starts = dict.fromkeys([0, *range(0, len(run) - 63, 32), len(run) - 64])
                for start in starts:
                    if start < 0 or start + 64 > len(run):
                        continue
                    candidate = bytes.fromhex(run[start:start + 64].decode("ascii"))
                    for name, page1 in page1s.items():
                        if name not in already and name not in found and hmac_ok(candidate, page1):
                            found[name] = candidate.hex()
            if len(found) + len(already) == len(page1s):
                return found
    return found


def _strategy_ascii(handle, page1s, already):
    """4.0.x：ASCII x'<96hex>'。"""
    found = {}
    by_salt = {}
    for name, p1 in page1s.items():
        by_salt.setdefault(p1[:SALT_SZ].hex(), []).append(name)
    for data in _iter_mem(handle):
        for m in _HEX_KEY_RE.finditer(data):
            h = m.group(1).decode()
            if len(h) == 96:
                key_hex, salt_hex = h[:64], h[64:]
                for name in by_salt.get(salt_hex, ()):
                    if name not in already and name not in found:
                        if hmac_ok(bytes.fromhex(key_hex), page1s[name]):
                            found[name] = key_hex
    return found


def _strategy_utf16(handle, page1s, already):
    """同一模式转成 UTF-16LE 的情况。"""
    found = {}
    by_salt = {p1[:SALT_SZ].hex(): n for n, p1 in page1s.items()}
    for data in _iter_mem(handle):
        for m in _HEX_KEY_WIDE_RE.finditer(data):
            h = m.group(0).replace(b"\x00", b"").decode("ascii")[2:-1]
            salt_hex = h[64:]
            name = by_salt.get(salt_hex)
            if name and name not in already and name not in found:
                key = bytes.fromhex(h[:64])
                if hmac_ok(key, page1s[name]):
                    found[name] = key.hex()
    return found


def _strategy_salt_proximity(handle, page1s, already, progress=None):
    """找 salt 原始字节，验证其前后 ±512 字节内的 8 字节对齐窗口。"""
    found = {}
    pend = {n: page1_material(p1) for n, p1 in page1s.items() if n not in already}
    for data in _iter_mem(handle):
        for name, (salt, iv, c1) in list(pend.items()):
            if name in found:
                continue
            pos = data.find(salt)
            while pos != -1 and name not in found:
                lo, hi = max(0, pos - 512), min(len(data) - KEY_SZ, pos + 512)
                for off in range(lo, hi, 8):
                    cand = data[off: off + KEY_SZ]
                    if cand == _ZERO32 or _try_key(cand, iv, c1):
                        if cand != _ZERO32 and hmac_ok(cand, page1s[name]):
                            found[name] = cand.hex()
                            break
                pos = data.find(salt, pos + 1)
            if name in found and progress:
                progress("salt-adjacent", name)
    return found


def _try_key(cand: bytes, iv: bytes, c1: bytes) -> bool:
    from Crypto.Cipher import AES
    blk = AES.new(cand, AES.MODE_ECB).decrypt(c1)
    plain = bytes(a ^ b for a, b in zip(blk, iv))
    # 页 1 的前 16 字节是明文 salt，解密首块对应 SQLite header 的 16..31。
    return (plain[:2] == b"\x10\x00" and plain[2:4] in (b"\x01\x01", b"\x02\x02")
            and plain[4:8] == b"\x50\x40\x20\x20")


def _strategy_brute(handle, page1s, already, progress=None):
    """8 字节对齐全内存暴力，单块 AES 快筛 + HMAC 确认，多进程并行。"""
    pend = {n: page1_material(p1) for n, p1 in page1s.items() if n not in already}
    if not pend:
        return {}
    targets = [(name, iv, c1) for name, (salt, iv, c1) in pend.items()]

    blocks = []
    for data in _iter_mem(handle):
        for i in range(0, max(1, len(data) - KEY_SZ), 4 * 1024 * 1024):
            blocks.append(data[i: i + 4 * 1024 * 1024])
    if not blocks:
        return {}

    ctx = mp.get_context("spawn")
    n_cpu = min(12, mp.cpu_count() or 4)
    found, done = {}, 0
    with ctx.Pool(n_cpu) as pool:
        for hits in pool.imap_unordered(
                _brute_worker, ((b, targets) for b in blocks), chunksize=1):
            done += 1
            if progress and done % 10 == 0:
                progress("brute", f"{done}/{len(blocks)} 块")
            for key, name in hits:
                if name not in found and hmac_ok(key, page1s[name]):
                    found[name] = key.hex()
    return found


def _brute_worker(args):
    data, targets = args
    from Crypto.Cipher import AES
    out = []
    name0, iv0, c10 = targets[0]
    for off in range(0, len(data) - KEY_SZ, 8):
        cand = data[off: off + KEY_SZ]
        if cand == _ZERO32:
            continue
        blk = AES.new(cand, AES.MODE_ECB).decrypt(c10)
        if not _try_key(cand, iv0, c10):
            continue
        # 快筛命中：对其它库也试一遍
        for name, iv, c1 in targets:
            if _try_key(cand, iv, c1):
                out.append((cand, name))
    return out


# ----------------------------- 入口 -----------------------------

def extract_keys(pid: int, db_page1s: dict[str, bytes], progress=None,
                 brute: bool = False) -> dict[str, str]:
    """扫描进程内存提取密钥，返回 {db名: hex密钥}。全部命中即提前返回。"""
    handle = kernel32.OpenProcess(PROCESS_READ, False, pid)
    if not handle:
        raise PermissionError(f"OpenProcess({pid}) 失败，权限不足或进程已退出")
    try:
        total = len(db_page1s)
        found = {}
        strategies = [_strategy_config_cipher, _strategy_ascii, _strategy_utf16,
                      _strategy_salt_proximity]
        if brute:
            strategies.append(_strategy_brute)
        for strategy in strategies:
            if progress:
                progress("stage", strategy.__name__)
            if strategy in (_strategy_salt_proximity, _strategy_brute):
                found.update(strategy(handle, db_page1s, found, progress))
            else:
                found.update(strategy(handle, db_page1s, found))
            if progress:
                progress("stage-done", f"{strategy.__name__}: {len(found)}/{total}")
            if len(found) == total:
                break
        return found
    finally:
        kernel32.CloseHandle(handle)


def extract_keys_many(pids: list[int], db_page1s: dict[str, bytes],
                      progress=None) -> dict[str, str]:
    """先逐个进程做定向扫描，避免在错误进程上启动全内存暴力。"""
    found = {}
    for pid in pids:
        pending = {name: page for name, page in db_page1s.items() if name not in found}
        if not pending:
            break
        if progress:
            progress("pid", str(pid))
        try:
            found.update(extract_keys(pid, pending, progress=progress))
        except PermissionError:
            continue
    return found


def save_keys(keys: dict[str, str], path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "keys": keys},
        ensure_ascii=False, indent=2), encoding="utf-8")


def load_keys(path) -> dict[str, str]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["keys"]
