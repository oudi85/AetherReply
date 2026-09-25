"""合成数据自测：按 SQLCipher 4（微信 4.x）页布局加密一个假库，
验证 decrypt_db 能完整还原、keyextract 的 HMAC/AES 校验能配对密钥。
这证明解密实现本身正确——拿到真密钥即可解真库。
"""

import hashlib
import hmac as hmac_mod
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Crypto.Cipher import AES

from auto_reply.wx import decrypt, keyextract

PAGE_SZ = decrypt.PAGE_SZ
KEY_SZ = decrypt.KEY_SZ


def make_fake_db(key: bytes, salt: bytes, n_pages: int = 5) -> bytes:
    """按微信 4.x 页布局加密 n_pages 页假 SQLite 内容。"""
    plaintext_body = (b"SQLite format 3\x00" + b"\x10\x00\x01\x01\x50\x40\x20\x20" +
                      bytes(range(256)) * 20)
    out = bytearray()
    for pgno in range(1, n_pages + 1):
        if pgno == 1:
            plain = plaintext_body[decrypt.SALT_SZ: PAGE_SZ - decrypt.RESERVE_SZ]
        else:
            plain = bytes([(pgno * 7 + i) % 251 for i in
                           range(PAGE_SZ - decrypt.RESERVE_SZ)])
        iv = bytes((pgno * 31 + i) % 256 for i in range(16))
        enc = AES.new(key, AES.MODE_CBC, iv).encrypt(plain)
        page = bytearray()
        if pgno == 1:
            page += salt
        page += enc + iv
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=KEY_SZ)
        h = hmac_mod.new(mac_key, enc + iv, hashlib.sha512)
        h.update(struct.pack("<I", pgno))
        page += h.digest()
        assert len(page) == PAGE_SZ, f"page {pgno} size {len(page)}"
        out += page
    return bytes(out)


def main():
    key = bytes(range(32))
    salt = bytes(range(16, 32))
    db = make_fake_db(key, salt)

    # 1) keyextract 层的验证原语
    page1 = db[:PAGE_SZ]
    assert keyextract.hmac_ok(key, page1), "正确密钥应通过 HMAC 验证"
    assert not keyextract.hmac_ok(b"\x00" * 32, page1), "错误密钥不应通过"
    mat_salt, mat_iv, mat_c1 = keyextract.page1_material(page1)
    assert keyextract._try_key(key, mat_iv, mat_c1), "AES 单块快筛应命中"
    print("[+] keyextract 验证原语 OK")

    # 2) 暴力 worker 能在一块内存里找到该密钥（8 字节对齐位置——与真实堆布局一致）
    blob = b"\x00" * 128 + key + b"\xff" * 100
    hits = keyextract._brute_worker((blob, [("fake.db", mat_iv, mat_c1)]))
    assert hits and hits[0][0] == key, f"暴力扫描应找到密钥, got {len(hits)} hits"
    print("[+] 暴力扫描 worker OK")

    # 3) decrypt_db 完整还原
    with tempfile.TemporaryDirectory(prefix="auto_reply_crypto_") as tmp:
        src = Path(tmp) / "enc.db"
        dst = Path(tmp) / "dec.db"
        src.write_bytes(db)
        pages = decrypt.decrypt_db(src, dst, key)
        assert pages == 5
        dec = dst.read_bytes()
        assert dec[:24] == (b"SQLite format 3\x00" +
                            b"\x10\x00\x01\x01\x50\x40\x20\x20")
        # 逐页正文比对：第2页明文应为 (2*7+i)%251
        p2 = dec[PAGE_SZ: 2 * PAGE_SZ]
        expect = bytes([(14 + i) % 251 for i in range(PAGE_SZ - decrypt.RESERVE_SZ)])
        assert p2[:len(expect)] == expect, "第2页明文不匹配"
    print("[+] decrypt_db 往返还原 OK")

    print("[✓] 全部自测通过：解密管线正确，密钥到位即可解真库")


if __name__ == "__main__":
    main()
