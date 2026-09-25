"""WAL 提交边界、旧帧和页级校验的回归测试。"""

import hashlib
import hmac
import struct
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from test_decrypt import make_fake_db
from auto_reply.wx import decrypt


def _encrypt_page(plain: bytes, key: bytes, salt: bytes, pgno: int) -> bytes:
    iv = bytes((pgno * 19 + i + 3) % 256 for i in range(16))
    start = 16 if pgno == 1 else 0
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(plain[start:4016])
    page = (salt if pgno == 1 else b"") + enc + iv
    mac_key = decrypt.derive_mac_key(key, salt)
    digest = hmac.new(mac_key, enc + iv + struct.pack("<I", pgno),
                      hashlib.sha512).digest()
    return page + digest


def _wal(frames: list[tuple[int, int, bytes]]) -> bytes:
    salt = bytes(range(32, 40))
    header = struct.pack(">IIII", 0x377F0682, 3007000, 4096, 0) + salt
    checksum = decrypt._wal_checksum(header, (0, 0), "<")
    out = bytearray(header + struct.pack(">II", *checksum))
    for pgno, dbsize, page in frames:
        first = struct.pack(">II", pgno, dbsize)
        checksum = decrypt._wal_checksum(first + page, checksum, "<")
        out += first + salt + struct.pack(">II", *checksum) + page
    return bytes(out)


def _shm(wal: bytes, mx_frame: int) -> bytes:
    header = bytearray(48)
    header[12] = 1
    struct.pack_into("<I", header, 16, mx_frame)
    header[32:40] = wal[16:24]
    return bytes(header * 2)


def main():
    key, salt = bytes(range(32)), bytes(range(16, 32))
    original = make_fake_db(key, salt, 3)
    first = bytearray(decrypt.decrypt_page(key, original[:4096], 1))
    first[60] ^= 0x55
    changed = _encrypt_page(first, key, salt, 1)
    second = bytearray(decrypt.decrypt_page(key, original[4096:8192], 2))
    second[60] ^= 0x77
    uncommitted = _encrypt_page(second, key, salt, 2)
    wal = _wal([(1, 3, changed), (2, 0, uncommitted)])

    pages, size = decrypt.committed_wal_pages(wal, b"", key, salt)
    assert size == 3 and set(pages) == {1}
    assert decrypt.committed_wal_pages(wal, _shm(wal, 0), key, salt) == ({}, 0)
    pages, size = decrypt.committed_wal_pages(wal, _shm(wal, 1), key, salt)
    assert size == 3 and set(pages) == {1}

    corrupted = bytearray(wal)
    corrupted[32 + 24 + 100] ^= 1
    try:
        decrypt.committed_wal_pages(corrupted, _shm(wal, 1), key, salt)
    except ValueError:
        pass
    else:
        raise AssertionError("损坏的已提交帧不能被接受")

    with tempfile.TemporaryDirectory(prefix="auto_reply_wal_") as tmp:
        src, dst = Path(tmp) / "enc.db", Path(tmp) / "dec.db"
        src.write_bytes(original)
        Path(str(src) + "-wal").write_bytes(wal)
        Path(str(src) + "-shm").write_bytes(_shm(wal, 1))
        decrypt.decrypt_db(src, dst, key)
        result = dst.read_bytes()
        assert result[60] == first[60]
        assert result[4096+60] == second[60] ^ 0x77
        assert len(result) == len(original)
    print("[+] WAL 已提交帧、未提交帧、旧帧和校验测试通过")


if __name__ == "__main__":
    main()
