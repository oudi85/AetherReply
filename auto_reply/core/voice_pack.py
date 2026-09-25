"""Password-encrypted, account-bound portable owner-style data.

The pack contains compact profiles and a few redacted same-contact examples,
never the message mirror, database keys, API credentials, or other chats' text.
"""

import hashlib
import json
import os
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt

from .. import llm
from . import voice

FORMAT = "auto-reply-voice-pack-v1"


def account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def _key(password: str, salt: bytes) -> bytes:
    if len(password) < 10:
        raise ValueError("迁移口令至少需要 10 个字符")
    return scrypt(password.encode("utf-8"), salt, 32, N=2**15, r=8, p=1)


def export_pack(con, account_id: str, password: str, destination: Path) -> dict:
    voice.ensure_index(con)
    profiles = {scope: json.loads(raw) for scope, raw in con.execute(
        "SELECT scope,profile_json FROM portable_voice_profiles")}
    profiles.update({scope: json.loads(raw) for scope, raw in con.execute(
        "SELECT scope,profile_json FROM voice_profiles WHERE scope='global' OR scope LIKE 'talker:%'")
        if json.loads(raw).get("sample_count")})
    examples = []
    for talker, incoming, reply, created in con.execute(
            "SELECT talker,incoming,reply,created_at FROM voice_examples "
            "ORDER BY created_at DESC LIMIT 10000"):
        if not voice._usable(reply, example=True) or not voice._usable(incoming):
            continue
        examples.append({"talker": talker, "incoming": llm.redact(incoming),
                         "reply": llm.redact(reply), "created_at": created})
    for talker, incoming, reply, created in con.execute(
            "SELECT talker,incoming,reply,created_at FROM portable_voice_examples "
            "ORDER BY created_at DESC LIMIT 10000"):
        if voice._usable(reply, example=True) and voice._usable(incoming):
            item = {"talker": talker, "incoming": llm.redact(incoming),
                    "reply": llm.redact(reply), "created_at": created}
            if item not in examples:
                examples.append(item)
    payload = {"format": FORMAT, "account": account_fingerprint(account_id),
               "profiles": profiles, "examples": examples}
    salt, nonce = os.urandom(16), os.urandom(12)
    cipher = AES.new(_key(password, salt), AES.MODE_GCM, nonce=nonce)
    data, tag = cipher.encrypt_and_digest(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    wrapper = {"format": FORMAT, "salt": salt.hex(), "nonce": nonce.hex(),
               "tag": tag.hex(), "data": data.hex()}
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"迁移文件已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as out:
        json.dump(wrapper, out)
    return {"profiles": len(profiles), "examples": len(examples)}


def import_pack(con, account_id: str, password: str, source: Path) -> dict:
    wrapper = json.loads(Path(source).read_text(encoding="utf-8"))
    if wrapper.get("format") != FORMAT:
        raise ValueError("不支持的风格包格式")
    salt, nonce, tag = (bytes.fromhex(wrapper[key]) for key in ("salt", "nonce", "tag"))
    cipher = AES.new(_key(password, salt), AES.MODE_GCM, nonce=nonce)
    payload = json.loads(cipher.decrypt_and_verify(bytes.fromhex(wrapper["data"]), tag))
    if payload.get("format") != FORMAT or payload.get("account") != account_fingerprint(account_id):
        raise ValueError("风格包不属于当前微信账号")
    profiles = payload.get("profiles", {})
    examples = payload.get("examples", [])
    if not isinstance(profiles, dict) or not isinstance(examples, list):
        raise ValueError("风格包内容无效")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM portable_voice_profiles")
        con.execute("DELETE FROM portable_voice_examples")
        con.executemany("INSERT INTO portable_voice_profiles(scope,profile_json) VALUES(?,?)",
                        [(scope, json.dumps(profile, ensure_ascii=False))
                         for scope, profile in profiles.items()
                         if scope == "global" or scope.startswith("talker:")])
        con.executemany("INSERT INTO portable_voice_examples(talker,incoming,reply,created_at) "
                        "VALUES(?,?,?,?)",
                        [(item["talker"], item["incoming"], item["reply"], item["created_at"])
                         for item in examples])
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {"profiles": len(profiles), "examples": len(examples)}
