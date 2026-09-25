"""同步层：提取密钥 → 解密 → 规范化入镜像库。

微信 4.x 的 message/contact/session 库表结构随小版本变动，这里先内省
（把每个解密库的表结构落盘到 data/schema_*.txt），再按已知列名候选
组合规范化；识别不了的库会跳过并提示看 schema 文件。
"""

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .. import config as cfgmod
from ..core import store
from . import decrypt, keyextract, paths

# message 表列名候选（微信 4.x 已知变体）
COL = {
    "is_sender": ["IsSender"],
    "type": ["Type"],
    "sub_type": ["SubType"],
    "content": ["StrContent", "MessageContent", "message_content", "Content"],
    "create_time": ["CreateTime", "CreateTimeStamp", "create_time"],
    "sequence": ["Sequence", "CreateSequence"],
    "talker": ["Talker", "UsrName", "StrTalker"],
    "talker_id": ["TalkerId"],
    "rowid_pk": ["localId", "local_id", "MsgId"],
    "nickname": ["NickName", "nick_name"],
    "remark": ["Remark", "remark"],
    "alias": ["Alias", "alias"],
    "username": ["UserName", "username", "user_name"],
}


def _pick(columns: list[str], key: str) -> str | None:
    low = {c.lower(): c for c in columns}
    for cand in COL[key]:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def _introspect(db: Path, out_dir: Path) -> list[tuple[str, list[str]]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        info = []
        for t in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{t}')")]
            info.append((t, cols))
        (out_dir / f"schema_{db.stem}.txt").write_text(
            "\n".join(f"{t}: {', '.join(c)}" for t, c in info), encoding="utf-8")
        return info
    finally:
        con.close()


def _find_message_table(tables: list[tuple[str, list[str]]]) -> tuple[str, dict] | None:
    """在解密库中找消息表，返回 (表名, 列映射)。支持单表和按会话分表两种布局。"""
    # 布局一：单张总表（Msg / ChatMsg ...），含 IsSender + 内容 + 时间
    for name, cols in tables:
        if name.startswith("sqlite_"):
            continue
        mapping = {
            "is_sender": _pick(cols, "is_sender"),
            "type": _pick(cols, "type"),
            "content": _pick(cols, "content"),
            "create_time": _pick(cols, "create_time"),
        }
        if all(mapping.values()) and not name.lower().endswith("_fts"):
            mapping["talker"] = _pick(cols, "talker")
            mapping["talker_id"] = _pick(cols, "talker_id")
            mapping["rowid"] = _pick(cols, "rowid_pk") or "rowid"
            return name, mapping
    # 布局二：每会话一张 Chat_xxx 表（列相同，talker 从 Name2Id 反查）
    chat_tables = [(n, c) for n, c in tables
                   if n.startswith("Chat_") and all([
                       _pick(c, "is_sender"), _pick(c, "type"),
                       _pick(c, "content"), _pick(c, "create_time")])]
    if chat_tables:
        # 列映射取自第一张；talker 由调用方按表名批量解析
        cols = chat_tables[0][1]
        mapping = {
            "is_sender": _pick(cols, "is_sender"),
            "type": _pick(cols, "type"),
            "content": _pick(cols, "content"),
            "create_time": _pick(cols, "create_time"),
            "rowid": _pick(cols, "rowid_pk") or "rowid",
        }
        return "__CHAT_TABLES__", mapping
    return None


def _name2id(decrypted: Path) -> dict[int, str]:
    """读 Name2Id 表：TalkerId/表名后缀 -> wxid。"""
    out = {}
    try:
        con = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
        rows = con.execute("SELECT * FROM Name2Id").fetchall()
        cols = [d[1] for d in con.execute("PRAGMA table_info(Name2Id)")]
        con.close()
        idc = next((i for i, c in enumerate(cols) if c.lower() == "id"), 0)
        namec = next((i for i, c in enumerate(cols) if "usrname" in c.lower()), 1)
        for r in rows:
            try:
                out[int(r[idc])] = str(r[namec])
            except (ValueError, TypeError, IndexError):
                continue
    except sqlite3.Error:
        pass
    return out


def _decode_text(value: bytes | str | None) -> str:
    """还原 4.x 文本消息；无法解码时留空，不把二进制送给模型。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard
            value = zstandard.ZstdDecompressor().decompress(
                value, max_output_size=2_000_000)
        except Exception:
            return ""
    try:
        return value.decode("utf-8").strip("\x00 \t\r\n")
    except UnicodeDecodeError:
        return ""


def _talker_index(con) -> dict[str, str]:
    return {hashlib.md5(row[0].encode("utf-8")).hexdigest(): row[0]
            for row in con.execute("SELECT wxid FROM contacts") if row[0]}


def _import_messages_4x(con, decrypted: Path, src_name: str,
                        tables: list[tuple[str, list[str]]], self_wxid: str) -> int:
    """导入微信 4.x 的 Msg_<会话MD5> 分表。"""
    chat_tables = [(name, cols) for name, cols in tables
                   if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", name)]
    talkers = _talker_index(con)
    labels = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情包]",
              48: "[位置]", 49: "[文件或链接]", 10000: "[系统消息]"}
    imported = 0
    rcon = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
    try:
        self_row = rcon.execute(
            "SELECT rowid FROM Name2Id WHERE user_name=?", (self_wxid,)).fetchone()
        if self_row is None:
            raise ValueError(f"{src_name}: Name2Id 中找不到当前账号")
        self_sender_id = self_row[0]
        for table, cols in chat_tables:
            if not {"local_id", "local_type", "real_sender_id", "create_time",
                    "message_content"}.issubset(set(cols)):
                continue
            talker = talkers.get(table[4:].lower(), table[4:].lower())
            state_name = f"{src_name}:{table}"
            state = store.get_sync_state(con, state_name)
            since = state["max_rowid"] if state else 0
            compressed = "compress_content" if "compress_content" in cols else "NULL"
            query = (f'SELECT local_id, local_type, real_sender_id, create_time, '
                     f'message_content, {compressed} FROM "{table}" '
                     'WHERE local_id > ? ORDER BY local_id')
            batch = []
            max_id = since
            for local_id, raw_type, sender_id, created, body, alt in rcon.execute(query, (since,)):
                max_id = max(max_id, int(local_id))
                kind = raw_type & 0xFF if raw_type > 0xFFFF else raw_type
                text = _decode_text(body) if kind == 1 else labels.get(kind, f"[类型{kind}]")
                if kind == 1 and not text:
                    text = _decode_text(alt)
                batch.append((talker, int(sender_id == self_sender_id), kind, text,
                              int(created), src_name, table, int(local_id)))
                if len(batch) >= 2000:
                    _write_4x(con, batch)
                    imported += len(batch)
                    batch = []
            if batch:
                _write_4x(con, batch)
                imported += len(batch)
            if max_id != since:
                store.upsert_sync_state(con, state_name, 0, 0, max_id)
    finally:
        rcon.close()
    return imported


def _repair_sender_flags_4x(con, decrypted: Path, src_name: str,
                            self_wxid: str) -> None:
    """Upgrade mirrors made with the old, incorrect constant sender ID."""
    marker = f"{src_name}:sender-v2"
    if store.get_sync_state(con, marker):
        return
    rcon = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
    try:
        self_row = rcon.execute(
            "SELECT rowid FROM Name2Id WHERE user_name=?", (self_wxid,)).fetchone()
        if self_row is None:
            raise ValueError(f"{src_name}: Name2Id 中找不到当前账号")
        self_sender_id = self_row[0]
        tables = [row[0] for row in rcon.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", row[0])]
        if not tables:
            return
        with con:
            con.execute(
                "UPDATE messages SET is_sender=0 WHERE src_db=? AND src_table LIKE 'Msg_%'",
                (src_name,))
            for table in tables:
                ids = rcon.execute(
                    f'SELECT local_id FROM "{table}" WHERE real_sender_id=?',
                    (self_sender_id,))
                con.executemany(
                    "UPDATE messages SET is_sender=1 WHERE src_db=? "
                    "AND src_table=? AND src_rowid=?",
                    ((src_name, table, row[0]) for row in ids))
            con.execute(
                "DELETE FROM incoming_events WHERE message_id IN "
                "(SELECT id FROM messages WHERE src_db=? AND is_sender=1)",
                (src_name,))
            con.execute(
                "INSERT INTO sync_state(name,mtime,size,max_rowid,synced_at) "
                "VALUES(?,0,0,0,0)", (marker,))
    finally:
        rcon.close()


def _write_4x(con, batch):
    con.executemany(
        "INSERT OR IGNORE INTO messages "
        "(talker,is_sender,msg_type,content,create_time,src_db,src_table,src_rowid) "
        "VALUES (?,?,?,?,?,?,?,?)", batch)
    con.commit()


def _import_messages(con, decrypted: Path, src_name: str, since_rowid: int,
                     self_wxid: str) -> int:
    """把解密库的消息导入镜像，返回导入条数。"""
    tables = _introspect(decrypted, decrypted.parent)
    if any(re.fullmatch(r"Msg_[0-9a-fA-F]{32}", name) for name, _ in tables):
        return _import_messages_4x(con, decrypted, src_name, tables, self_wxid)
    found = _find_message_table(tables)
    if not found:
        print(f"  [!] {src_name}: 未识别出消息表，表结构见 data/schema_{Path(src_name).stem}.txt")
        return -1
    table, m = found

    rows_imported = 0
    n2i = _name2id(decrypted) if m.get("talker_id") or table == "__CHAT_TABLES__" else {}

    rcon = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
    try:
        targets = ([(table, None)] if table != "__CHAT_TABLES__"
                   else [(n, n.removeprefix("Chat_")) for n, _ in tables if n.startswith("Chat_")])
        for tname, chat_key in targets:
            # talker 解析：单表布局按 Talker 列或 TalkerId 列；分表布局按表名后缀
            if m.get("talker"):
                sel = (f"SELECT rowid, {m['is_sender']}, {m['type']}, {m['content']}, "
                       f"{m['create_time']}, {m['talker']} FROM \"{tname}\" WHERE rowid > ?")
                talker_from_row = True
            elif m.get("talker_id"):
                sel = (f"SELECT rowid, {m['is_sender']}, {m['type']}, {m['content']}, "
                       f"{m['create_time']}, {m['talker_id']} FROM \"{tname}\" WHERE rowid > ?")
                talker_from_row = True
            else:
                sel = (f"SELECT rowid, {m['is_sender']}, {m['type']}, {m['content']}, "
                       f"{m['create_time']} FROM \"{tname}\" WHERE rowid > ?")
                talker_from_row = False

            fixed_talker = n2i.get(int(chat_key), "") if chat_key is not None else ""
            if talker_from_row is False and not fixed_talker:
                continue

            batch = []
            for r in rcon.execute(sel, (since_rowid,)):
                rowid = r[0]
                talker = (str(r[5]) if talker_from_row else fixed_talker)
                if talker and talker.isdigit():        # 还是数字 id → Name2Id 反查
                    talker = n2i.get(int(talker), talker)
                # 群聊里 TalkerId 语义不同（群内发送者），上层用不到，忽略非空群成员
                batch.append((talker, r[1], r[2], r[3] or "", r[4], src_name, rowid))
                if len(batch) >= 5000:
                    _write(con, batch); rows_imported += len(batch); batch = []
            if batch:
                _write(con, batch); rows_imported += len(batch)
    finally:
        rcon.close()
    return rows_imported


def _write(con, batch):
    con.executemany(
        "INSERT OR REPLACE INTO messages"
        "(talker, is_sender, msg_type, content, create_time, src_db, src_rowid) "
        "VALUES(?,?,?,?,?,?,?)", batch)
    con.commit()


def _import_contacts(con, decrypted: Path, src_name: str) -> int:
    tables = _introspect(decrypted, decrypted.parent)
    for name, cols in sorted(tables, key=lambda item: item[0].lower() != "contact"):
        un = _pick(cols, "username")
        if not un:
            continue
        nick, remark, alias = (_pick(cols, "nickname") or "''",
                               _pick(cols, "remark") or "''",
                               _pick(cols, "alias") or "''")
        rcon = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
        try:
            rows = rcon.execute(
                f"SELECT {un}, {nick}, {remark}, {alias} FROM \"{name}\"").fetchall()
        finally:
            rcon.close()
        con.executemany(
            "INSERT INTO contacts(wxid, nickname, remark, alias) VALUES(?,?,?,?) "
            "ON CONFLICT(wxid) DO UPDATE SET nickname=excluded.nickname, "
            "remark=excluded.remark, alias=excluded.alias",
            [(r[0], r[1] or "", r[2] or "", r[3] or "") for r in rows])
        con.commit()
        return len(rows)
    return 0


def _import_sessions(con, decrypted: Path) -> int:
    rcon = sqlite3.connect(f"file:{decrypted}?mode=ro", uri=True)
    try:
        rows = rcon.execute("SELECT username FROM SessionTable WHERE username IS NOT NULL")
        names = [(row[0],) for row in rows if row[0]]
    except sqlite3.Error:
        return 0
    finally:
        rcon.close()
    con.executemany("INSERT OR IGNORE INTO contacts(wxid) VALUES(?)", names)
    con.commit()
    return len(names)


def sync_once(cfg: dict, verbose: bool = True) -> dict:
    """一轮同步：密钥→解密→导入。返回统计 dict。"""
    data_dir = cfgmod.data_dir(cfg)
    dec_dir = data_dir / "decrypted"
    mirror = store.connect(data_dir / "auto_reply.db")

    data_root = paths.find_data_dir(cfg)
    account = paths.find_account(data_root)
    self_wxid = re.sub(r"_[0-9a-fA-F]{4}$", "", account.name)
    if not self_wxid.startswith("wxid_") or self_wxid == account.name:
        raise ValueError("无法从微信账号目录确认当前账号 wxid")
    if verbose:
        print(f"[i] 账号目录: {account.name}")
    dbs = paths.collect_dbs(account, cfg["wechat"].get("dbs") or None)

    # 1) 需要解密且源文件有变化的库
    changed = []
    observed = {}
    for db in dbs:
        st = store.get_sync_state(mirror, db.name)
        wal_st = store.get_sync_state(mirror, db.name + ":wal")
        mtime, size = db.stat().st_mtime_ns, db.stat().st_size
        wal_rev = decrypt.wal_revision(db)
        observed[db.name] = (mtime, size, wal_rev)
        if (st is None or st["mtime"] != mtime or st["size"] != size
                or wal_st is None or
                (wal_st["mtime"], wal_st["size"], wal_st["max_rowid"]) != wal_rev
                or not (dec_dir / f"{db.stem}.sqlite").exists()):
            changed.append(db)

    keys_path = data_dir / "keys.json"
    keys = keyextract.load_keys(keys_path) if keys_path.exists() else {}
    ready = {db.name for db in dbs if db not in changed}

    if changed:
        pids = paths.list_wechat_pids()
        missing = []
        for db in changed:
            with open(db, "rb") as fh:
                page1 = fh.read(keyextract.PAGE_SZ)
            key = keys.get(db.name)
            try:
                valid = bool(key) and keyextract.hmac_ok(bytes.fromhex(key), page1)
            except ValueError:
                valid = False
            if not valid:
                keys.pop(db.name, None)
                missing.append(db.name)
        if pids and missing:
            if verbose:
                print(f"[i] 微信运行中({len(pids)} 个进程)，内存提取缺失密钥: {missing}")
            page1s = {}
            for d in dbs:
                if d.name in missing:
                    with open(d, "rb") as fh:
                        page1s[d.name] = fh.read(keyextract.PAGE_SZ)
            got = keyextract.extract_keys_many(pids, page1s)
            if got:
                keys.update(got)
                keyextract.save_keys(keys, keys_path)
                if verbose:
                    print(f"[+] 新提取 {len(got)} 把密钥")

        for db in changed:
            key = keys.get(db.name)
            if not key:
                if verbose:
                    print(f"[!] {db.name}: 没有密钥（微信需处于已登录运行状态），跳过")
                continue
            out = dec_dir / f"{db.stem}.sqlite"
            success = False
            for attempt in range(3):
                before = (db.stat().st_mtime_ns, db.stat().st_size,
                          decrypt.wal_revision(db))
                try:
                    pages = decrypt.decrypt_db(db, out, bytes.fromhex(key))
                    after = (db.stat().st_mtime_ns, db.stat().st_size,
                             decrypt.wal_revision(db))
                    if before != after:
                        continue
                    if before[2][2] > 0:
                        check = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
                        try:
                            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                raise ValueError("WAL 快照完整性检查失败")
                        finally:
                            check.close()
                    observed[db.name] = before
                    success = True
                    break
                except (ValueError, sqlite3.DatabaseError):
                    continue
            if not success:
                if verbose:
                    print(f"[!] {db.name}: 数据库正在变化或 WAL 快照校验失败，下轮重试")
                continue
            ready.add(db.name)
            if verbose:
                print(f"[+] 解密 {db.name} -> {out.name} ({pages} 页)")

    # 2) 联系人与会话先入库，才能把 Msg_<MD5> 分表还原为会话 ID。
    # stale：本轮有变化却没能更新到最新的库；非空时不算一次成功同步。
    stats = {"dbs": 0, "messages": 0, "contacts": 0,
             "stale": [f"{db.name}（{'缺少密钥' if not keys.get(db.name) else '解密未完成'}）"
                       for db in changed if db.name not in ready]}
    for db in dbs:
        if db.name not in ready:
            continue
        dec = dec_dir / f"{db.stem}.sqlite"
        if not dec.exists():
            continue
        if db.name == "contact.db" and db in changed:
            stats["contacts"] += _import_contacts(mirror, dec, db.name)
        elif db.name == "session.db" and db in changed:
            _import_sessions(mirror, dec)

    for db in dbs:
        if db.name not in ready:
            continue
        dec = dec_dir / f"{db.stem}.sqlite"
        if not dec.exists():
            continue
        st = store.get_sync_state(mirror, db.name)
        since = st["max_rowid"] if st else 0
        if re.fullmatch(r"message_\d+\.db", db.name):
            n = _import_messages(mirror, dec, db.name, since, self_wxid)
            if n < 0:
                stats["stale"].append(f"{db.name}（消息表无法识别）")
                continue
            _repair_sender_flags_4x(mirror, dec, db.name, self_wxid)
            if verbose and n:
                print(f"[+] {db.name}: 导入 {n} 条新消息")
            stats["messages"] += max(n, 0)
        mtime, size, wal_rev = observed[db.name]
        row = mirror.execute(
            "SELECT COALESCE(MAX(src_rowid),0) FROM messages WHERE src_db=?",
            (db.name,)).fetchone()
        store.upsert_sync_state(mirror, db.name, mtime, size, row[0])
        store.upsert_sync_state(mirror, db.name + ":wal", *wal_rev)
        stats["dbs"] += 1

    mirror.close()
    return stats


def resolve_talker(con, name: str) -> str | None:
    """把用户输入的昵称/备注/wxid 解析成 talker（wxid）。"""
    if row := con.execute("SELECT wxid FROM contacts WHERE wxid=?", (name,)).fetchone():
        return row["wxid"]
    row = con.execute(
        "SELECT wxid FROM contacts WHERE remark=? OR nickname=? OR alias=? "
        "ORDER BY length(remark)>0 DESC LIMIT 1", (name, name, name)).fetchone()
    if row:
        return row["wxid"]
    row = con.execute(
        "SELECT DISTINCT talker FROM messages WHERE talker LIKE ? LIMIT 2",
        (f"%{name}%",)).fetchone()
    return row["talker"] if row else None
