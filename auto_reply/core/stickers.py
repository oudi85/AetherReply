"""Local favorite-sticker inventory, tagging and conservative retrieval."""

import base64
import copy
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from .. import llm
from .. import config as cfgmod
from ..wx import decrypt, keyextract, paths

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def scan(cfg: dict, con) -> dict:
    """Read favorite MD5s and captions; never send or download anything."""
    account = paths.find_account(paths.find_data_dir(cfg))
    db = account / "db_storage" / "emoticon" / "emoticon.db"
    if not db.is_file():
        raise FileNotFoundError("未找到微信收藏表情库 emoticon.db")
    with db.open("rb") as source:
        page1 = source.read(keyextract.PAGE_SZ)
    key_file = cfgmod.data_dir(cfg) / "keys.json"
    keys = keyextract.load_keys(key_file) if key_file.exists() else {}
    key_hex = keys.get(db.name)
    if not key_hex or not keyextract.hmac_ok(bytes.fromhex(key_hex), page1):
        found = keyextract.extract_keys_many(paths.list_wechat_pids(), {db.name: page1})
        key_hex = found.get(db.name)
        if not key_hex:
            raise RuntimeError("无法读取收藏表情库密钥，请保持 PC 微信登录")
        keys[db.name] = key_hex
        keyextract.save_keys(keys, key_file)
    with tempfile.TemporaryDirectory(prefix="auto_reply_stickers_") as temp:
        plain = Path(temp) / "emoticon.sqlite"
        decrypt.decrypt_db(db, plain, bytes.fromhex(key_hex))
        source = sqlite3.connect(plain)
        try:
            tables = {row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"kFavEmoticonOrderTable", "kNonStoreEmoticonTable"}
            if not required <= tables:
                raise RuntimeError("收藏表情库结构尚未适配")
            rows = source.execute(
                "SELECT f.md5,n.caption,n.cdn_url,n.tp_url,n.extern_url "
                "FROM kFavEmoticonOrderTable f "
                "LEFT JOIN kNonStoreEmoticonTable n ON n.md5=f.md5 "
                "ORDER BY f.rowid").fetchall()
        finally:
            source.close()
    now = int(time.time())
    records = []
    for order, (md5, caption, cdn, tp, external) in enumerate(rows):
        if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
            continue
        url = next((item for item in (cdn, tp, external)
                    if isinstance(item, str) and item.startswith(("https://", "http://"))), "")
        records.append((md5.lower(), str(caption or ""), url, order, now))
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE stickers SET favorite_order=-1")
        con.executemany(
            "INSERT INTO stickers(md5,caption,source_url,favorite_order,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(md5) DO UPDATE SET caption=excluded.caption,"
            "source_url=excluded.source_url,favorite_order=excluded.favorite_order,"
            "updated_at=excluded.updated_at", records)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {"favorites": len(rows), "indexed": len(records)}


def set_tags(con, md5: str, tags: list[str]) -> None:
    """Store unreviewed vision/legacy hints; these never authorize sending."""
    clean = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))[:12]
    if not clean or any(len(tag) > 30 for tag in clean):
        raise ValueError("请提供 1-12 个不超过 30 字的标签")
    cur = con.execute("UPDATE stickers SET tags_json=?,updated_at=? WHERE md5=?",
                      (json.dumps(clean, ensure_ascii=False), int(time.time()), md5.lower()))
    if cur.rowcount != 1:
        raise ValueError("收藏表情索引中没有这个 MD5")
    con.commit()


def save_category(con, name: str, guidance: str = "") -> None:
    name, guidance = name.strip(), guidance.strip()
    if (not 1 <= len(name) <= 24 or len(guidance) > 120 or
            any(ord(char) < 32 for char in name + guidance)):
        raise ValueError("分类名需为 1-24 字，分类说明最多 120 字")
    con.execute(
        "INSERT INTO sticker_categories(name,guidance,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET guidance=excluded.guidance,"
        "updated_at=excluded.updated_at", (name, guidance, int(time.time())))
    con.commit()


def delete_category(con, name: str) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM sticker_assignments WHERE category=?", (name,))
        con.execute("DELETE FROM sticker_categories WHERE name=?", (name,))
        con.commit()
    except Exception:
        con.rollback()
        raise


def assign(con, md5: str, category: str, description: str) -> None:
    """A human assignment is the only approval for automatic sticker use."""
    md5, category, description = md5.lower(), category.strip(), description.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", md5):
        raise ValueError("表情 ID 无效")
    if not 1 <= len(description) <= 120 or any(ord(char) < 32 for char in description):
        raise ValueError("请写 1-120 字的具体用途，供 DS 区分同类表情")
    if not con.execute("SELECT 1 FROM stickers WHERE md5=?", (md5,)).fetchone():
        raise ValueError("收藏表情索引中没有这个 MD5")
    if not con.execute("SELECT 1 FROM sticker_categories WHERE name=?", (category,)).fetchone():
        raise ValueError("请先创建分类")
    con.execute("INSERT INTO sticker_assignments(md5,category,description,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(md5,category) DO UPDATE SET "
                "description=excluded.description,updated_at=excluded.updated_at",
                (md5, category, description, int(time.time())))
    con.commit()


def unassign(con, md5: str, category: str) -> None:
    con.execute("DELETE FROM sticker_assignments WHERE md5=? AND category=?",
                (md5.lower(), category.strip()))
    con.commit()


def annotations(con, md5: str) -> list[dict]:
    return [dict(row) for row in con.execute(
        "SELECT category,description FROM sticker_assignments WHERE md5=? "
        "ORDER BY category", (md5.lower(),))]


def _calibrated(cfg: dict, con) -> list[str]:
    from ..wx import sticker_send
    return sticker_send.calibrated_favorites(cfg, con)


def available_categories(cfg: dict, con) -> dict[str, str]:
    """Only manually classified, calibrated favorites reach the first DS call."""
    favorites = set(_calibrated(cfg, con))
    if not favorites:
        return {}
    result = {}
    for row in con.execute(
            "SELECT c.name,c.guidance,a.md5 FROM sticker_categories c "
            "JOIN sticker_assignments a ON a.category=c.name "
            "JOIN stickers s ON s.md5=a.md5 WHERE s.favorite_order>=0 "
            "ORDER BY c.name"):
        if row["md5"] in favorites:
            result[row["name"]] = row["guidance"]
    return result


def options_in_category(cfg: dict, con, category: str) -> list[dict]:
    favorites = _calibrated(cfg, con)
    if not favorites:
        return []
    rows = {row["md5"]: row["description"] for row in con.execute(
        "SELECT a.md5,a.description FROM sticker_assignments a "
        "JOIN stickers s ON s.md5=a.md5 "
        "WHERE a.category=? AND s.favorite_order>=0", (category,))}
    return [{"md5": md5, "description": rows[md5]}
            for md5 in favorites if md5 in rows]


def choose_in_category(cfg: dict, con, category: str, incoming: str,
                       reply_text: str = "") -> str | None:
    """One small DS call chooses an approved sticker or abstains."""
    options = options_in_category(cfg, con, category)
    if not options:
        return None
    names = "\n".join(f"- {item['md5']}: {item['description']}" for item in options)
    selection_cfg = copy.deepcopy(cfg)
    selection_cfg["llm"]["thinking"] = False
    result = llm.chat_json(selection_cfg, [{"role": "system", "content":
        "你为一条微信回复挑选表情。只能从给定分类中的人工标注表情选择一张；"
        "语境不合适、用途说明不足或难以确定时返回 null。"
        "不要凭 ID 猜图片内容。严格输出 JSON：{\"md5\":null} "
        "或 {\"md5\":\"给定的32位ID\"}。"},
        {"role": "user", "content": f"分类：{category}\n对方消息：{llm.redact(incoming[:500])}\n"
         f"拟回复文字：{llm.redact(reply_text[:300])}\n可选表情：\n{names}"}],
        temperature=0, max_tokens=150)
    md5 = result.get("md5") if isinstance(result, dict) else None
    if md5 is None:
        return None
    if not isinstance(md5, str) or md5.lower() not in {x["md5"] for x in options}:
        raise ValueError("DS 选择了分类之外的表情")
    return md5.lower()


def recommend(con, context: str, limit: int = 5) -> list[dict]:
    """Only tagged images can be recommended; never guess an unknown sticker."""
    query = re.sub(r"\s+", "", context.lower())
    scored = []
    for row in con.execute("SELECT md5,caption,tags_json,asset_path FROM stickers "
                           "WHERE favorite_order>=0"):
        tags = json.loads(row["tags_json"])
        score = sum(len(tag) for tag in tags if tag.lower() in query)
        if score:
            scored.append((score, {"md5": row["md5"], "caption": row["caption"],
                                   "tags": tags, "asset_ready": bool(row["asset_path"])}))
    return [item for _, item in sorted(scored, key=lambda pair: -pair[0])[:limit]]


def available_for_reply(cfg: dict, con) -> dict[str, str]:
    favorites = _calibrated(cfg, con)
    result = {}
    for md5 in favorites:
        descriptions = [row["description"] for row in annotations(con, md5)]
        if descriptions:
            result[md5] = "、".join(descriptions[:3])
    return result


def _extension(data: bytes) -> str:
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("下载内容不是受支持的表情图片")


def download(cfg: dict, con, md5: str) -> Path:
    row = con.execute("SELECT source_url FROM stickers WHERE md5=?", (md5.lower(),)).fetchone()
    if row is None or not row[0]:
        raise ValueError("此收藏表情没有可用的原图地址")
    url = row[0]
    parsed = urllib.parse.urlparse(url)
    allowed = (".qq.com", ".qpic.cn", ".wechat.com")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or \
            not any(parsed.hostname.endswith(suffix) for suffix in allowed):
        raise ValueError("表情地址不属于已识别的微信素材域名")
    # Old WeChat records often store http URLs; request the same CDN via HTTPS.
    urls = [url.replace("http://", "https://", 1)
            if parsed.scheme == "http" else url]
    last_error = None
    for candidate in urls:
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                final = urllib.parse.urlparse(response.url)
                final_host = final.hostname or ""
                if final.scheme != "https" or not any(
                        final_host.endswith(suffix) for suffix in allowed):
                    raise ValueError("表情下载跳转到了非微信素材域名")
                data = response.read(MAX_IMAGE_BYTES + 1)
            if hashlib.md5(data).hexdigest() != md5.lower():
                raise ValueError("表情素材 MD5 与收藏记录不一致")
            break
        except (OSError, ValueError) as exc:
            last_error = exc
    else:
        raise ValueError(f"无法验证表情原图：{type(last_error).__name__}") from last_error
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("表情图片超过 8 MB")
    ext = _extension(data)
    dest = cfgmod.data_dir(cfg) / "stickers" / f"{md5.lower()}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(ext + ".tmp")
    temp.write_bytes(data)
    temp.replace(dest)
    con.execute("UPDATE stickers SET asset_path=?,updated_at=? WHERE md5=?",
                (str(dest), int(time.time()), md5.lower()))
    con.commit()
    return dest


def auto_tag(cfg: dict, con, md5: str) -> list[str]:
    """Explicit opt-in: one local sticker image is sent to a configured vision model."""
    model = cfg.get("stickers", {}).get("vision_model", "")
    if not model:
        raise ValueError("请先配置 [stickers].vision_model（须支持图片输入）")
    row = con.execute("SELECT asset_path FROM stickers WHERE md5=?",
                      (md5.lower(),)).fetchone()
    if row is None or not row[0] or not Path(row[0]).is_file():
        raise ValueError("请先下载这张收藏表情的图片")
    with Image.open(row[0]) as source:
        if getattr(source, "n_frames", 1) > 1:
            count = source.n_frames
            positions = sorted({0, count // 2, count - 1})
            image = Image.new("RGB", (256 * len(positions), 256), "white")
            for column, frame in enumerate(positions):
                source.seek(frame)
                still = source.convert("RGB")
                still.thumbnail((256, 256))
                image.paste(still, (256 * column, 0))
        else:
            image = source.convert("RGB")
            image.thumbnail((512, 512))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    vision_cfg = copy.deepcopy(cfg)
    vision_cfg["llm"]["model"] = model
    vision_cfg["llm"]["thinking"] = False
    if cfg.get("stickers", {}).get("vision_base_url"):
        vision_cfg["llm"]["base_url"] = cfg["stickers"]["vision_base_url"]
    result = llm.chat_json(vision_cfg, [{"role": "user", "content": [
        {"type": "text", "text": "请只根据这张微信表情图，给出 3-6 个简短中文标签。"
         "标签写它表达的情绪、动作和适用场景；不确定时用中性描述。"
         "严格输出 JSON：{\"tags\":[\"开心\",\"庆祝\"]}"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}], temperature=0.2, max_tokens=600)
    tags = result.get("tags") if isinstance(result, dict) else None
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("视觉模型没有返回有效标签")
    set_tags(con, md5, tags)
    return tags
