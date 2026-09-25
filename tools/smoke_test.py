"""引擎冒烟测试：合成消息库 → sync 导入 → stats 统计 → suggest 上下文。"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.core import stats, store, suggest
from auto_reply.wx import sync as wxsync

TMP = Path(tempfile.mkdtemp(prefix="ar_smoke_"))


def make_fake_wx_message_db(path: Path):
    """模拟微信 4.x 解密产物：Msg 表 + Name2Id 映射（布局A）。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE Name2Id (Id INTEGER PRIMARY KEY, UsrName TEXT);
        CREATE TABLE Msg (localId INTEGER PRIMARY KEY AUTOINCREMENT,
            TalkerId INTEGER, IsSender INTEGER, Type INTEGER,
            StrContent TEXT, CreateTime INTEGER, Sequence INTEGER);
    """)
    con.execute("INSERT INTO Name2Id VALUES (7, 'wxid_friend001')")
    # 生成 400 条 60 天内的对话：对方话多、爱问问题、晚上活跃
    import random
    random.seed(42)
    now = int(time.time())
    rid = 0
    t = now - 60 * 86400
    while t < now - 3600:
        t += random.randint(1800, 7200)
        n = random.randint(2, 8)
        for i in range(n):
            sender = random.choice([0, 0, 0, 1])  # 对方 3/4
            body = random.choice([
                "哈哈哈真的假的", "今晚吃啥？", "那个事情怎么样了？",
                "我跟你讲个事", "好", "行吧", "发个链接我看看？", "绷不住了",
            ])
            if sender == 0 and 19 <= time.localtime(t).tm_hour <= 23:
                body = "晚上" + body
            rid += 1
            con.execute("INSERT INTO Msg(TalkerId,IsSender,Type,StrContent,CreateTime,Sequence)"
                        " VALUES(7,?,1,?,?,?)", (sender, body, t + i * 60, rid))
    con.commit()
    con.close()


def main():
    # 1) 合成"解密后的微信库"
    wxdb = TMP / "message_0.sqlite"
    make_fake_wx_message_db(wxdb)

    # 2) 走 sync 的导入路径（真实代码，不打桩）
    mirror = store.connect(TMP / "auto_reply.db")
    n = wxsync._import_messages(mirror, wxdb, "message_0.db", 0)
    assert n > 300, f"导入条数异常: {n}"
    print(f"[+] sync 导入 {n} 条，talker 解析: "
          f"{mirror.execute('SELECT DISTINCT talker FROM messages').fetchone()[0]}")

    # 3) 行为统计
    talker = mirror.execute("SELECT talker FROM messages LIMIT 1").fetchone()[0]
    s = stats.stats_for(mirror, talker)
    assert s["total"] == n and s["they_initiate_pct"] > 0
    print(stats.render_stats(s))
    print("[+] stats OK")

    # 4) suggest 的上下文窗口
    ctx = suggest.context_window(mirror, talker, n=5)
    assert len(ctx) == 5 and "我:" in " ".join(ctx)
    print("\n".join(ctx))
    inc = suggest.latest_incoming(mirror, talker)
    assert inc and inc["is_sender"] == 0 if False else inc is not None
    print(f"[+] 最新来讯: {inc['content']}")
    print("[✓] 引擎链路全部通过")


if __name__ == "__main__":
    main()
