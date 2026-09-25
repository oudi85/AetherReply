"""定位微信 4.x 的数据目录与账号（注册表 + 文件系统探测）。"""

import os
import re
from pathlib import Path

import psutil


def _registry_install_path() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Tencent\Weixin") as key:
            val, _ = winreg.QueryValueEx(key, "InstallPath")
            return Path(val)
    except OSError:
        return None


def find_data_dir(cfg: dict) -> Path:
    """返回 xwechat_files 根目录（含 wxid_* 子目录的那个）。"""
    configured = cfg["wechat"].get("data_dir", "")
    if configured:
        p = Path(configured)
        if p.is_dir() and any((d / "db_storage").is_dir() for d in p.iterdir() if d.is_dir()):
            return p
        raise FileNotFoundError(f"配置的 data_dir 下没有微信 4.x 数据库: {p}")

    candidates = []
    # 常见位置：微信通常装在自定义盘（注册表 InstallPath 的同级或文档目录）
    install = _registry_install_path()
    if install is not None:
        candidates.append(install.parent / "xwechat_files")
    docs = Path(os.environ.get("USERPROFILE", "")) / "Documents"
    candidates += [docs / "xwechat_files", docs / "WeChat Files"]
    for drive in "CDEF":
        candidates += [
            Path(f"{drive}:/xwechat_files"),
            Path(f"{drive}:/微信/xwechat_files"),
            Path(f"{drive}:/WeChat/xwechat_files"),
        ]
    for p in candidates:
        if p.is_dir() and any((d / "db_storage").is_dir() for d in p.iterdir() if d.is_dir()):
            return p
    raise FileNotFoundError(
        "未找到 xwechat_files 数据目录。请在 config.toml [wechat] data_dir 里手动指定"
        "（即包含 wxid_xxx 子目录的目录）。"
    )


def find_account(data_dir: Path) -> Path:
    """数据目录下选最近使用的账号目录。"""
    accts = [d for d in data_dir.iterdir() if d.is_dir()
             and (d / "db_storage").is_dir()]
    if not accts:
        raise FileNotFoundError(f"{data_dir} 下没有账号目录")
    return max(accts, key=lambda d: d.stat().st_mtime)


def list_wechat_pids() -> list[int]:
    """列出所有 Weixin.exe 进程；密钥配置可能只在其中一个。

    用 psutil 而非 powershell/tasklist：后台以 pythonw 运行时，启动控制台程序
    会弹出黑窗。
    """
    pids = set()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == "weixin.exe":
                pids.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(pids)


def is_wechat_running() -> tuple[bool, int]:
    pids = list_wechat_pids()
    return (bool(pids), pids[0] if pids else 0)


def collect_dbs(account_dir: Path, only: list[str] | None = None) -> list[Path]:
    """db_storage 下的全部 .db（排除 -wal/-shm/material 等）。"""
    out = []
    db_storage = account_dir / "db_storage"
    if not db_storage.is_dir():
        raise FileNotFoundError(f"没有 db_storage: {account_dir}")
    for f in sorted(db_storage.rglob("*.db")):
        name = f.name
        if name.endswith(("-wal", "-shm")) or ".material" in name:
            continue
        if name.endswith(("_fts.db", ".kvdb")):
            continue
        if only and name not in only:
            continue
        if not only and not (re.fullmatch(r"message_\d+\.db", name)
                             or name in {"contact.db", "session.db"}):
            continue
        out.append(f)
    return out
