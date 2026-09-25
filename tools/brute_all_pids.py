"""对所有 Weixin.exe 进程做暴力密钥扫描（诊断工具，主进程之外的小进程）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.wx import keyextract, paths
from auto_reply import config as cfgmod

import time


def main():
    cfg = cfgmod.load_config()
    data_root = paths.find_data_dir(cfg)
    account = paths.find_account(data_root)
    dbs = [d for d in paths.collect_dbs(account)
           if d.name.startswith("message") or d.name in ("contact.db", "session.db")]
    page1s = {}
    for d in dbs:
        with open(d, "rb") as fh:
            page1s[d.name] = fh.read(4096)

    pids = paths.list_wechat_pids()
    done_pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    targets = [p for p in pids if p != done_pid]
    print(f"扫描进程: {targets}")

    for pid in targets:
        print(f"\n=== PID {pid} ===", flush=True)
        t0 = time.time()

        def progress(tag, info, _pid=pid):
            if tag == "stage-done" or (tag == "brute" and "块" in str(info) and
                                       (str(info).split("/")[0].isdigit() and
                                        int(str(info).split("/")[0]) % 50 == 0)):
                print(f"  [{_pid}:{tag}] {info}", flush=True)

        try:
            found = keyextract.extract_keys(pid, page1s, progress=progress)
            print(f"\n  结果: {len(found)} 把 ({time.time()-t0:.0f}s) -> {list(found)[:3]}")
            if found:
                from pathlib import Path
                out = cfgmod.data_dir(cfg) / "keys.json"
                merged = {}
                if out.exists():
                    merged = keyextract.load_keys(out)
                merged.update(found)
                keyextract.save_keys(merged, out)
                print(f"  [+] 已合并保存到 {out}，共 {len(merged)} 把")
                break
        except Exception as e:
            print(f"  失败: {e}")


if __name__ == "__main__":
    main()
