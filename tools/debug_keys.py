"""诊断：扫描所有 Weixin.exe 进程，统计 x'<hex>' 匹配分布 + salt 命中情况。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.wx import keyextract, paths
from auto_reply import config as cfgmod

cfg = cfgmod.load_config()
data_root = paths.find_data_dir(cfg)
account = paths.find_account(data_root)
dbs = paths.collect_dbs(account)

salts = {}
for d in dbs:
    with open(d, "rb") as fh:
        salts[d.name] = fh.read(16).hex()

print(f"db salts: {len(set(salts.values()))} 个唯一值")

# 所有 Weixin.exe PID
pids = paths.list_wechat_pids()
print(f"所有进程: {pids}")

for pid in pids:
    try:
        h = keyextract.kernel32.OpenProcess(keyextract.PROCESS_READ, False, pid)
        if not h:
            print(f"PID {pid}: OpenProcess 失败")
            continue
        regions = keyextract._enum_regions(h)
        total_hits = {}
        salt_hits = {}
        len_dist = {}
        for i, (base, size) in enumerate(regions):
            data = keyextract._read_mem(h, base, size)
            if not data:
                continue
            for m in keyextract._HEX_KEY_RE.finditer(data):
                hxs = m.group(1).decode()
                L = len(hxs)
                len_dist[L] = len_dist.get(L, 0) + 1
                if L == 96:
                    s = hxs[64:]
                    if s in salts.values():
                        salt_hits[s] = salt_hits.get(s, 0) + 1
        keyextract.kernel32.CloseHandle(h)
        print(f"\nPID {pid}: {len(regions)} 区域, hex串长度分布 {len_dist}")
        print(f"  96hex 中 salt 命中: {len(salt_hits)} 个")
        for s, c in list(salt_hits.items())[:5]:
            names = [n for n, v in salts.items() if v == s]
            print(f"    salt {s[:12]}... -> {names} (出现{c}次)")
    except Exception as e:
        print(f"PID {pid}: 错误 {e}")
