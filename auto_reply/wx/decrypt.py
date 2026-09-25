"""SQLCipher 4（微信 4.x WCDB）纯 Python 页级解密。

页布局（page_size=4096, reserve=80）:
  page 1 : [salt 16B][ciphertext 4000B][IV 16B][HMAC-SHA512 64B]
  page n : [ciphertext 4016B][IV 16B][HMAC-SHA512 64B]
解密后的页拼起来就是标准 SQLite 文件；page1 头部补回 "SQLite format 3\\x00"。
"""

import hmac as hmac_mod
import hashlib
import os
import struct
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b"SQLite format 3\x00"


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(
            page[SALT_SZ: PAGE_SZ - RESERVE_SZ])
        return bytearray(SQLITE_HDR + plain + b"\x00" * RESERVE_SZ)
    plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(
        page[: PAGE_SZ - RESERVE_SZ])
    return plain + b"\x00" * RESERVE_SZ


def page_hmac_ok(enc_key: bytes, page1: bytes) -> bool:
    """密钥-文件配对验证：HMAC 匹配才继续解整库。"""
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored = page1[PAGE_SZ - HMAC_SZ:]
    h = hmac_mod.new(mac_key, data, hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == stored


def _wal_paths(src: Path) -> tuple[Path, Path]:
    return Path(str(src) + "-wal"), Path(str(src) + "-shm")


def _wal_index_frame(shm: bytes, wal: bytes) -> int | None:
    """读取 SQLite WAL 索引的已提交帧数；索引不可信时由 WAL 校验恢复。"""
    if (len(shm) < 96 or len(wal) < 32 or shm[:48] != shm[48:96]
            or shm[12] != 1 or shm[32:40] != wal[16:24]):
        return None
    return struct.unpack_from("<I", shm, 16)[0]


def wal_revision(src: Path) -> tuple[int, int, int]:
    """同步层使用的 WAL 变化标记，不读取消息内容。"""
    wal_path, shm_path = _wal_paths(src)
    if not wal_path.exists():
        return (0, 0, 0)
    st = wal_path.stat()
    with wal_path.open("rb") as fh:
        header = fh.read(32)
    shm = shm_path.read_bytes()[:96] if shm_path.exists() else b""
    mx = _wal_index_frame(shm, header)
    return (st.st_mtime_ns, st.st_size, mx if mx is not None else -1)


def _wal_checksum(data: bytes, state: tuple[int, int], endian: str) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL 校验数据长度无效")
    s0, s1 = state
    for x0, x1 in struct.iter_unpack(endian + "II", data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _wal_page_hmac_ok(mac_key: bytes, page: bytes, pgno: int) -> bool:
    start = SALT_SZ if pgno == 1 else 0
    digest = hmac_mod.new(mac_key, page[start:PAGE_SZ-HMAC_SZ]
                          + struct.pack("<I", pgno), hashlib.sha512).digest()
    return hmac_mod.compare_digest(digest, page[PAGE_SZ-HMAC_SZ:])


def committed_wal_pages(wal: bytes, shm: bytes, enc_key: bytes,
                        db_salt: bytes) -> tuple[dict[int, bytes], int]:
    """只返回 SQLite 校验通过且处于已提交事务中的加密页。"""
    if len(wal) < 32:
        return {}, 0
    magic, version, page_size = struct.unpack_from(">III", wal)
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_SZ:
        raise ValueError("不支持的 WAL 格式")
    endian = "<" if magic == 0x377F0682 else ">"
    checksum = _wal_checksum(wal[:24], (0, 0), endian)
    if checksum != struct.unpack_from(">II", wal, 24):
        raise ValueError("WAL 文件头校验失败")
    max_frame = _wal_index_frame(shm, wal)
    if max_frame == 0:
        return {}, 0
    mac_key = derive_mac_key(enc_key, db_salt)
    committed, pending = {}, {}
    commit_size = 0
    available = (len(wal)-32) // (24+PAGE_SZ)
    limit = min(available, max_frame) if max_frame is not None else available
    verified = 0
    for i in range(limit):
        start = 32 + i * (24+PAGE_SZ)
        frame = wal[start:start+24+PAGE_SZ]
        pgno, dbsize = struct.unpack_from(">II", frame)
        if pgno == 0 or frame[8:16] != wal[16:24]:
            break
        checksum = _wal_checksum(frame[:8] + frame[24:], checksum, endian)
        if checksum != struct.unpack_from(">II", frame, 16):
            break
        page = frame[24:]
        if pgno == 1 and page[:SALT_SZ] != db_salt:
            raise ValueError("WAL 首页盐与主库不一致")
        if not _wal_page_hmac_ok(mac_key, page, pgno):
            raise ValueError("WAL 页面 HMAC 校验失败")
        verified += 1
        pending[pgno] = page
        if dbsize:
            committed.update(pending)
            pending.clear()
            commit_size = dbsize
    if max_frame is not None and verified < max_frame:
        raise ValueError("WAL 索引指向不完整的帧")
    return committed, commit_size


def decrypt_db(src: Path, dst: Path, enc_key: bytes) -> int:
    """解密主库及已提交 WAL 到 dst，返回主库页数。"""
    data = src.read_bytes()
    if len(data) < PAGE_SZ:
        raise ValueError(f"{src.name}: 文件太小，不是有效数据库")
    if not page_hmac_ok(enc_key, data[:PAGE_SZ]):
        raise ValueError(f"{src.name}: HMAC 验证失败，密钥不匹配")

    wal_path, shm_path = _wal_paths(src)
    wal = wal_path.read_bytes() if wal_path.exists() else b""
    shm = shm_path.read_bytes()[:96] if shm_path.exists() else b""
    overlay, commit_size = committed_wal_pages(wal, shm, enc_key, data[:SALT_SZ])

    out = bytearray()
    for pgno in range(len(data) // PAGE_SZ):
        page = overlay.pop(pgno + 1, None)
        if page is None:
            page = data[pgno * PAGE_SZ: (pgno + 1) * PAGE_SZ]
        out += decrypt_page(enc_key, page, pgno + 1)
    for pgno in range(len(data) // PAGE_SZ + 1, commit_size + 1):
        page = overlay.pop(pgno, None)
        if page is None:
            raise ValueError(f"WAL 提交后缺少新增页 {pgno}")
        out += decrypt_page(enc_key, page, pgno)
    if commit_size:
        del out[commit_size * PAGE_SZ:]
    tail = len(data) % PAGE_SZ
    if tail:  # 尾部残页按 0 补齐（正常不应出现）
        out += data[-tail:] + b"\x00" * (PAGE_SZ - tail)

    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".tmp")
    temp.write_bytes(bytes(out))
    os.replace(temp, dst)
    return len(data) // PAGE_SZ
