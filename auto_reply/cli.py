"""CLI 入口：python -m auto_reply <command>"""

import argparse
import json
import sys
import time

from . import config as cfgmod
from .core import control, decision, events, persona, reply_plan, stats, stickers, store, suggest, voice
from .wx import paths, sync


def cmd_detect(cfg):
    running, pid = paths.is_wechat_running()
    print(f"微信进程: {'运行中 PID=' + str(pid) if running else '未运行'}")
    data_root = paths.find_data_dir(cfg)
    print(f"数据目录: {data_root}")
    account = paths.find_account(data_root)
    print(f"账号目录: {account.name}")
    dbs = paths.collect_dbs(account, cfg["wechat"].get("dbs") or None)
    print(f"数据库共 {len(dbs)} 个:")
    for d in dbs:
        print(f"  {d.relative_to(account.parent)}  ({d.stat().st_size // 1024} KB)")
    mirror = cfgmod.data_dir(cfg) / "auto_reply.db"
    if mirror.exists():
        con = store.connect(mirror)
        n = con.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        t = con.execute("SELECT COUNT(DISTINCT talker) c FROM messages").fetchone()["c"]
        print(f"本地镜像: {n} 条消息 / {t} 个会话")


def cmd_keys(cfg):
    running, _ = paths.is_wechat_running()
    if not running:
        sys.exit("[x] 微信未运行，无法从内存提取密钥。请先登录微信。")
    data_root = paths.find_data_dir(cfg)
    account = paths.find_account(data_root)
    dbs = paths.collect_dbs(account, cfg["wechat"].get("dbs") or None)
    # 只提取同步层真正会用的库：message_* / contact / session
    dbs = [d for d in dbs
           if d.name.startswith("message") or d.name in ("contact.db", "session.db")]
    page1s = {}
    for d in dbs:
        with open(d, "rb") as fh:
            page1s[d.name] = fh.read(4096)
    t0 = time.time()

    def progress(tag, info):
        print(f"\r  [{tag}] {info}                      ", end="", flush=True)

    found = _extract(cfg, paths.list_wechat_pids(), page1s, progress)
    print(f"\n[+] 提取到 {len(found)}/{len(page1s)} 把密钥（{time.time()-t0:.1f}s）")
    missing = set(page1s) - set(found)
    if missing:
        print(f"[!] 未找到: {', '.join(sorted(missing))}")
    out = cfgmod.data_dir(cfg) / "keys.json"
    from .wx import keyextract
    existing = keyextract.load_keys(out) if out.exists() else {}
    _save(cfg, {**existing, **found}, out)
    print(f"[+] 已保存 {out}")


def cmd_sync(cfg):
    result = sync.sync_once(cfg)
    print(f"[✓] 同步完成: {result['dbs']} 个库, "
          f"{result['messages']} 条新消息, {result['contacts']} 个联系人")


def cmd_contacts(cfg, limit=30, show_id=False):
    con = _mirror(cfg)
    rows = con.execute(
        "SELECT m.talker, c.remark, c.nickname, COUNT(*) n, "
        "MAX(m.create_time) last_t FROM messages m LEFT JOIN contacts c "
        "ON c.wxid = m.talker GROUP BY m.talker ORDER BY n DESC LIMIT ?",
        (limit,)).fetchall()
    from datetime import datetime
    for r in rows:
        label = r["remark"] or r["nickname"] or r["talker"]
        print(f"{label:<24} {r['n']:>6} 条  最近 "
              f"{datetime.fromtimestamp(r['last_t']).strftime('%Y-%m-%d')}"
              + (f"  {r['talker']}" if show_id else ""))


def cmd_stats(cfg, name, days):
    con = _mirror(cfg)
    talker = sync.resolve_talker(con, name)
    if not talker:
        sys.exit(f"[x] 找不到联系人: {name}（先跑 contacts 看列表）")
    s = stats.stats_for(con, talker, limit_days=days)
    print(stats.render_stats(s))


def cmd_persona(cfg, name, force, show_json):
    cfgmod.require_llm(cfg)
    con = _mirror(cfg)
    talker = sync.resolve_talker(con, name)
    if not talker:
        sys.exit(f"[x] 找不到联系人: {name}")
    print(f"[i] 正在分析 {talker} ...")
    card = persona.build(cfg, con, talker, force=force)
    if show_json:
        print(json.dumps(card, ensure_ascii=False, indent=2))
    else:
        print(persona.render_card(card))


def cmd_suggest(cfg, name, note):
    cfgmod.require_llm(cfg)
    con = _mirror(cfg)
    talker = sync.resolve_talker(con, name)
    if not talker:
        sys.exit(f"[x] 找不到联系人: {name}")
    print("[i] 生成建议 ...")
    result = suggest.suggest(cfg, con, talker, note=note)
    if result["candidates"]:
        selected = decision.select_candidate(cfg, con, talker,
                                             result["candidates"],
                                             str(result.get("analysis", "")))
        inc = suggest.latest_incoming(con, talker)
        result["candidates"][selected.index] = suggest.resolve_selected(
            cfg, con, result["candidates"][selected.index],
            inc["content"] if inc else "")
    print(suggest.render(result))


def cmd_voice_rebuild(cfg):
    """Refresh only the local owner-style index; never call the model or sender."""
    con = _mirror(cfg, create=False)
    if con is None:
        sys.exit("[x] 本地消息镜像不存在，请先运行 sync")
    try:
        result = voice.rebuild(con)
        print("[+] 本人表达习惯已更新："
              f"抽样 {result['own_texts_sampled']} 条本人文字，"
              f"{result['contact_profiles']} 个有足够样本的联系人，"
              f"{result['reply_examples']} 条历史回复情境。")
    finally:
        con.close()


def cmd_voice_pack(cfg, action, path):
    """Transfer style between machines without exporting raw chat history."""
    import getpass
    from pathlib import Path
    from .core import voice_pack
    import re
    account = re.sub(r"_[0-9a-fA-F]{4}$", "",
                     paths.find_account(paths.find_data_dir(cfg)).name)
    con = _mirror(cfg, create=(action == "import"))
    if con is None:
        sys.exit("[x] 本地消息镜像不存在，请先运行 sync")
    try:
        password = getpass.getpass("风格包口令：")
        if action == "export":
            result = voice_pack.export_pack(con, account, password, Path(path))
        else:
            result = voice_pack.import_pack(con, account, password, Path(path))
        print(f"[+] {action} 完成：{result['profiles']} 份画像，"
              f"{result['examples']} 条脱敏示例")
    finally:
        con.close()


def cmd_stickers(cfg, args):
    con = _mirror(cfg)
    try:
        if args.action == "scan":
            print(stickers.scan(cfg, con))
        elif args.action == "tag":
            stickers.set_tags(con, args.md5, args.tags)
            print("[+] 视觉参考标签已保存；只有人工分类会进入自动发送候选")
        elif args.action == "category-add":
            stickers.save_category(con, args.md5 or "", " ".join(args.tags))
            print("[+] 分类已保存")
        elif args.action == "category-delete":
            stickers.delete_category(con, args.md5 or "")
            print("[+] 分类及其表情关联已删除")
        elif args.action == "categories":
            for row in con.execute("SELECT c.name,c.guidance,COUNT(a.md5) AS n "
                                   "FROM sticker_categories c LEFT JOIN sticker_assignments a "
                                   "ON a.category=c.name GROUP BY c.name ORDER BY c.name"):
                print(f"{row['name']} ({row['n']} 张)  {row['guidance']}")
        elif args.action == "assign":
            if len(args.tags) < 2:
                raise ValueError("用法：stickers assign <md5> <分类名> <具体用途说明>")
            stickers.assign(con, args.md5, args.tags[0], " ".join(args.tags[1:]))
            print("[+] 人工分类已保存")
        elif args.action == "unassign":
            if not args.tags:
                raise ValueError("用法：stickers unassign <md5> <分类名>")
            stickers.unassign(con, args.md5, args.tags[0])
            print("[+] 人工分类已移除")
        elif args.action == "labeler":
            from .sticker_labeler import open_labeler
            open_labeler(cfg, con)
        elif args.action == "download":
            print(f"[+] 已保存：{stickers.download(cfg, con, args.md5)}")
        elif args.action == "auto-tag":
            print(f"[+] 标签：{stickers.auto_tag(cfg, con, args.md5)}")
        elif args.action == "auto-tag-batch":
            done = failed = 0
            rows = con.execute("SELECT md5,asset_path,tags_json FROM stickers "
                               "WHERE favorite_order>=0 ORDER BY favorite_order DESC "
                               "LIMIT ?", (args.limit,)).fetchall()
            for row in rows:
                if json.loads(row["tags_json"]):
                    continue
                try:
                    if not row["asset_path"]:
                        stickers.download(cfg, con, row["md5"])
                    stickers.auto_tag(cfg, con, row["md5"])
                    done += 1
                    print(f"[+] 已标记 {done} 张", flush=True)
                except Exception as exc:
                    failed += 1
                    print(f"[!] 一张表情未标记：{type(exc).__name__}", flush=True)
            print(f"[+] 批量结束：新增 {done}，失败 {failed}")
        elif args.action == "calibrate":
            from .wx import sticker_send
            print(f"[+] 表情映射已校准：{sticker_send.calibrate(cfg, con)}")
        elif args.action == "recommend":
            for item in stickers.recommend(con, args.text):
                print(f"{item['md5']}  {','.join(item['tags'])}"
                      + ("  [图片已保存]" if item["asset_ready"] else ""))
        else:
            for row in con.execute("SELECT md5,caption,tags_json,asset_path FROM stickers "
                                   "WHERE favorite_order>=0 ORDER BY favorite_order DESC "
                                   "LIMIT ?", (args.limit,)):
                print(f"{row['md5']}  {row['caption'] or '(无描述)'}  "
                      f"{row['tags_json']}  {'[图片已保存]' if row['asset_path'] else ''} "
                      f"人工分类={stickers.annotations(con, row['md5'])}")
    finally:
        con.close()


def cmd_watch(cfg):
    cfgmod.require_llm(cfg)
    interval = cfg["sync"]["poll_seconds"]
    print(f"[i] watch 模式：每 {interval}s 同步一次，发现新消息自动出建议。Ctrl+C 退出")
    while True:
        try:
            sync.sync_once(cfg, verbose=False)
            con = _mirror(cfg, create=False)
            if con is None:
                time.sleep(interval); continue
            try:
                events.enqueue_new(
                    con, max_age_seconds=cfg["sync"]["max_event_age_seconds"])
                for batch in events.ready_batches(
                        con, debounce_seconds=cfg["sync"]["debounce_seconds"]):
                    if _push_suggestion(cfg, con, batch["talker"]):
                        events.mark_done(con, batch["talker"], batch["newest_id"])
                    else:
                        events.defer(con, batch["talker"], batch["newest_id"])
            finally:
                con.close()
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[✓] 已退出")
            return


def cmd_watch_auto(cfg):
    """Generate one reply per eligible incoming batch and send via UIA only."""
    cfgmod.require_llm(cfg)
    options = cfg.get("auto_send", {})
    allowed = options.get("allow_talkers", [])
    if not isinstance(allowed, list):
        sys.exit("[x] auto_send.allow_talkers 必须是列表。")
    allowed = set(allowed)
    interval = cfg["sync"]["poll_seconds"]
    from .wx.uia_send import SendError, SendSession, display_name, send_once
    from .wx.sticker_send import send_once as send_sticker_once
    print(f"[i] 自动回复已启动；允许 {len(allowed)} 个一对一会话；"
          f"每 {interval}s 检查一次。Ctrl+C 退出")
    existing = _mirror(cfg, create=False)
    if existing is not None:
        try:
            stopped = existing.execute(
                "SELECT status,count(*) n FROM incoming_events "
                "WHERE status IN ('sending','uncertain') GROUP BY status").fetchall()
            if stopped:
                print("[!] 需人工核对的历史发送状态：" + ", ".join(
                    f"{row['status']}={row['n']}" for row in stopped))
        finally:
            existing.close()
    paused_seen = False
    while True:
        try:
            synced = sync.sync_once(cfg, verbose=False)
            con = _mirror(cfg)
            try:
                events.enqueue_new(
                    con, max_age_seconds=cfg["sync"]["max_event_age_seconds"])
                if synced["stale"]:
                    control.record_worker_issue(
                        con, "同步未完成：" + "、".join(synced["stale"]))
                else:
                    control.record_sync_ok(con)
                runtime_cfg = cfgmod.load_config()
                runtime_options = runtime_cfg.get("auto_send", {})
                current_allowed = set(runtime_options.get("allow_talkers", []))
                mode = control.current_mode(con)
                paused = (mode == "paused" or not current_allowed or
                          (mode == "auto" and not runtime_options.get("enabled")))
                if paused != paused_seen:
                    print("[i] 自动回复已暂停" if paused else "[i] 自动回复已恢复", flush=True)
                    paused_seen = paused
                batches = [] if paused else events.ready_batches(
                    con, debounce_seconds=cfg["sync"]["debounce_seconds"])
                for batch in batches:
                    talker, newest = batch["talker"], batch["newest_id"]
                    if talker not in current_allowed or talker == "filehelper" or talker.endswith("@chatroom"):
                        events.skip_pending(con, talker, newest)
                        continue
                    if mode == "auto":
                        try:
                            display_name(con, talker)
                        except SendError as exc:
                            events.skip_pending(con, talker, newest)
                            print(f"[!] 会话不满足自动发送条件，已跳过：{exc}")
                            continue
                    incoming = con.execute(
                        "SELECT create_time FROM messages WHERE id=? AND talker=? "
                        "AND is_sender=0", (newest, talker)).fetchone()
                    if incoming is None:
                        events.skip_pending(con, talker, newest)
                        continue
                    if incoming["create_time"] < int(time.time()) - cfg["sync"]["max_event_age_seconds"]:
                        events.skip_pending(con, talker, newest)
                        continue
                    already_replied = con.execute(
                        "SELECT 1 FROM messages WHERE talker=? AND is_sender=1 "
                        "AND create_time>=? LIMIT 1",
                        (talker, incoming["create_time"])).fetchone()
                    if already_replied:
                        events.skip_pending(con, talker, newest)
                        continue
                    try:
                        result = suggest.suggest(runtime_cfg, con, talker)
                        candidates = result.get("candidates") or []
                        selection = decision.select_candidate(
                            runtime_cfg, con, talker, candidates,
                            str(result.get("analysis", "")))
                        incoming_text = con.execute(
                            "SELECT content FROM messages WHERE id=?", (newest,)).fetchone()[0]
                        choice = suggest.resolve_selected(
                            runtime_cfg, con, candidates[selection.index], incoming_text)
                        candidates[selection.index] = choice
                        reply = choice.get("text", "").strip() if isinstance(choice, dict) else ""
                        if not reply or len(reply) > 1000 or "\n" in reply or "\r" in reply:
                            raise ValueError("模型未给出可发送的单行回复")
                        parts = choice.get("parts") or [{"type": "text", "text": reply}]
                        control.record_decision(
                            con, newest, selection.provider, selection.index,
                            candidates, str(result.get("analysis", "")))
                    except Exception as exc:
                        events.defer(con, talker, newest)
                        control.record_generation_failure(
                            con, newest, f"{type(exc).__name__}: {exc}")
                        print(f"[!] 回复生成失败，稍后重试：{type(exc).__name__}")
                        continue
                    latest_options = cfgmod.load_config().get("auto_send", {})
                    latest_mode = control.current_mode(con)
                    if (latest_mode == "paused" or
                            talker not in latest_options.get("allow_talkers", []) or
                            (latest_mode == "auto" and not latest_options.get("enabled"))):
                        control.set_decision_status(con, newest, "paused")
                        continue
                    if latest_mode == "draft":
                        events.mark_drafted(con, talker, newest)
                        print("[✓] 已生成候选；仅生成模式未发送", flush=True)
                        continue
                    events.mark_sending(con, talker, newest)
                    control.set_decision_status(con, newest, "sending")
                    try:
                        if runtime_cfg.get("reply_plan", {}).get("enabled"):
                            session = None

                            def active_session(active, target):
                                nonlocal session
                                if session is None:
                                    session = SendSession(active, target)
                                elif session.talker != target:
                                    raise SendError("连发目标已变化")
                                return session

                            def send_part(part_cfg, active, target, text):
                                return active_session(active, target).send_text(
                                    part_cfg, active, text)

                            def send_sticker(part_cfg, active, target, md5):
                                return send_sticker_once(
                                    cfgmod.load_config(), active, target, md5,
                                    session=active_session(active, target))

                            status = reply_plan.send(
                                con, runtime_cfg, cfgmod.load_config, send_part,
                                talker, newest, incoming["create_time"], parts,
                                sticker_sender=send_sticker)
                        else:
                            status = send_once(runtime_cfg, con, talker, reply)
                        if status == "preflight":
                            events.retry_preflight(con, talker, newest)
                            control.set_decision_status(con, newest, "preflight",
                                                        "发送前设置或会话状态已变化")
                            continue
                    except SendError as exc:
                        events.retry_preflight(con, talker, newest)
                        control.set_decision_status(con, newest, "preflight", str(exc))
                        print(f"[!] 发送前核对失败，稍后重试：{exc}")
                        continue
                    except Exception as exc:
                        status = "uncertain"
                        control.set_decision_status(con, newest, "uncertain", type(exc).__name__)
                        print(f"[!] 发送结果不确定，不会自动重发：{type(exc).__name__}")
                    events.mark_result(con, talker, newest,
                                       "sent" if status == "confirmed" else "uncertain")
                    control.set_decision_status(con, newest,
                                                "sent" if status == "confirmed" else "uncertain")
                    print("[✓] 已确认发送" if status == "confirmed"
                          else "[!] 回复未全部确认，已停止自动重试")
            finally:
                con.close()
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[✓] 已退出")
            return
        except Exception as exc:
            print(f"[!] 自动回复循环暂时无法运行：{type(exc).__name__}: {exc}", flush=True)
            try:
                issue_con = _mirror(cfg)
                try:
                    control.record_worker_issue(
                        issue_con, f"循环异常：{type(exc).__name__}: {exc}")
                finally:
                    issue_con.close()
            except Exception:
                pass
            time.sleep(max(interval, 30))


def _push_suggestion(cfg, con, talker) -> bool:
    label = con.execute(
        "SELECT COALESCE(NULLIF(remark,''),NULLIF(nickname,''),?) label "
        "FROM contacts WHERE wxid=?",
        (talker, talker)).fetchone()
    name = label["label"] if label else talker
    print(f"\n=== {name} 有新消息，自动分析 ===")
    try:
        print(suggest.render(suggest.suggest(cfg, con, talker)))
        return True
    except Exception as e:
        print(f"[!] 建议生成失败: {e}")
        return False


def cmd_events(cfg):
    """同步并仅记录新来讯事件，不调用模型或发送消息。"""
    sync.sync_once(cfg, verbose=False)
    con = _mirror(cfg)
    try:
        result = events.enqueue_new(
            con, max_age_seconds=cfg["sync"]["max_event_age_seconds"])
        pending = con.execute(
            "SELECT COUNT(*) FROM incoming_events WHERE status='pending'").fetchone()[0]
        print(f"事件队列: 新增 {result['queued']}，过期跳过 {result['skipped']}，待处理 {pending}")
    finally:
        con.close()


def cmd_toasts(cfg, limit):
    """查看最近的微信系统通知（验证通知通道是否可用）。"""
    from .wx import toast
    items = toast.wechat_toasts(limit=limit)
    if not items:
        print("[!] 没有微信 toast。请检查：微信 设置→消息通知 已开启，"
              "且微信窗口不在前台时来过消息。")
        return
    import datetime
    for it in items:
        t = datetime.datetime.fromtimestamp(it["arrival"] / 10**7 - 11644473600)
        print(f"{t:%m-%d %H:%M} [{it['title']}] {it['body']}")


def cmd_watch_toast(cfg):
    """监听微信系统通知，来消息即出建议（不依赖数据库密钥）。"""
    cfgmod.require_llm(cfg)
    from .wx import toast
    interval = cfg["sync"]["poll_seconds"]
    seen = toast.latest_watermark()
    print(f"[i] 监听微信 toast（每 {interval}s），Ctrl+C 退出")
    con = _mirror(cfg, create=False)  # 有人格库就带上，没有也能裸建议
    while True:
        try:
            for it in toast.wechat_toasts(since_arrival=seen):
                seen = max(seen, it["arrival"])
                if not it["body"]:
                    continue
                print(f"\n=== [{it['title']}] {it['body']}")
                talker = None
                if con is not None:
                    try:
                        talker = sync.resolve_talker(con, it["title"])
                    except Exception:
                        talker = None
                if talker:
                    try:
                        print(suggest.render(
                            suggest.suggest(cfg, con, talker, note=it["body"])))
                        continue
                    except Exception as e:
                        print(f"[!] 数据库建议失败({e})，退化为无上下文建议")
                _bare_suggest(cfg, it)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[✓] 已退出")
            return


def _bare_suggest(cfg, toast_item):
    """没有本地聊天库时的无上下文建议。"""
    from . import llm
    msgs = [
        {"role": "system", "content":
            "你是聊天军师。用户收到一条微信消息（来自系统通知，可能被截断）。"
            "给出3条简短口语化回复候选：接话/推进/幽默各一条，并一句话点破对方意图。"
            "输出 JSON: {\"analysis\":str, \"candidates\":[{\"style\":str,\"text\":str}]}"},
        {"role": "user", "content": f"来自 [{toast_item['title']}] 的消息：{toast_item['body']}"},
    ]
    try:
        r = llm.chat_json(cfg, msgs, temperature=0.8, max_tokens=800)
        print(f"意图: {r.get('analysis', '')}")
        for i, c in enumerate(r.get("candidates", []), 1):
            print(f"{i}. ({c.get('style', '')}) {c.get('text', '')}")
    except Exception as e:
        print(f"[!] 建议失败: {e}")


# --- helpers（便于测试时打桩） ---
def _extract(cfg, pids, page1s, progress):
    from .wx import keyextract
    return keyextract.extract_keys_many(pids, page1s, progress=progress)


def _save(cfg, found, out):
    from .wx import keyextract
    keyextract.save_keys(found, out)


def _mirror(cfg, create=True):
    p = cfgmod.data_dir(cfg) / "auto_reply.db"
    if not create and not p.exists():
        return None
    return store.connect(p)


def main():
    ap = argparse.ArgumentParser(prog="auto_reply",
                                 description="微信本地数据 → 人格模型 → 回复建议")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="探测微信/账号/数据库")
    sub.add_parser("keys", help="从运行中的微信内存提取数据库密钥")
    sub.add_parser("sync", help="解密并同步到本地镜像库")
    p = sub.add_parser("contacts", help="按消息量列出会话")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--show-id", action="store_true", help="同时显示配置允许名单所需的 wxid")
    p = sub.add_parser("stats", help="某人的行为统计")
    p.add_argument("name")
    p.add_argument("--days", type=int, default=None)
    p = sub.add_parser("persona", help="生成/更新人格卡片")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="忽略增量阈值强制重建")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p = sub.add_parser("suggest", help="给最新消息生成回复建议")
    p.add_argument("name")
    p.add_argument("--note", default="", help="补充说明，如'他在问我借钱'")
    sub.add_parser("voice-rebuild", help="从本人历史文字重建本地表达习惯，不调用模型或发送")
    p = sub.add_parser("voice-pack", help="用口令加密导出或导入本人表达习惯")
    p.add_argument("action", choices=("export", "import"))
    p.add_argument("path", help="风格包文件路径")
    p = sub.add_parser("stickers", help="扫描和人工分类本机收藏表情")
    p.add_argument("action", choices=("scan", "list", "tag", "download", "auto-tag",
                                      "auto-tag-batch", "recommend", "calibrate",
                                      "category-add", "category-delete", "categories",
                                      "assign", "unassign", "labeler"))
    p.add_argument("md5", nargs="?")
    p.add_argument("tags", nargs="*")
    p.add_argument("--text", default="")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("watch", help="轮询同步+新消息自动建议")
    sub.add_parser("watch-auto", help="仅对显式允许的一对一会话自动发送，无 OCR")
    sub.add_parser("events", help="同步并检查新消息事件队列，不调用模型")
    sub.add_parser("toasts", help="查看最近的微信系统通知")
    sub.add_parser("watch-toast", help="监听系统通知实时出建议（无需密钥）")

    args = ap.parse_args()
    cfg = cfgmod.load_config()
    if args.cmd == "detect":
        cmd_detect(cfg)
    elif args.cmd == "keys":
        cmd_keys(cfg)
    elif args.cmd == "sync":
        cmd_sync(cfg)
    elif args.cmd == "contacts":
        cmd_contacts(cfg, args.limit, args.show_id)
    elif args.cmd == "stats":
        cmd_stats(cfg, args.name, args.days)
    elif args.cmd == "persona":
        cmd_persona(cfg, args.name, args.force, args.json)
    elif args.cmd == "suggest":
        cmd_suggest(cfg, args.name, args.note)
    elif args.cmd == "voice-rebuild":
        cmd_voice_rebuild(cfg)
    elif args.cmd == "voice-pack":
        cmd_voice_pack(cfg, args.action, args.path)
    elif args.cmd == "stickers":
        cmd_stickers(cfg, args)
    elif args.cmd == "watch":
        cmd_watch(cfg)
    elif args.cmd == "watch-auto":
        cmd_watch_auto(cfg)
    elif args.cmd == "events":
        cmd_events(cfg)
    elif args.cmd == "toasts":
        cmd_toasts(cfg, 20)
    elif args.cmd == "watch-toast":
        cmd_watch_toast(cfg)


if __name__ == "__main__":
    main()
