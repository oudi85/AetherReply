"""Keep the model key encrypted for the current Windows user via DPAPI."""

from pathlib import Path


def save_key(path: Path, key: str) -> None:
    import win32crypt

    if not key.strip():
        raise ValueError("模型密钥为空")
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = win32crypt.CryptProtectData(
        key.strip().encode("utf-8"), None, None, None, None, 0)
    path.write_bytes(encrypted)


def load_key(path: Path) -> str:
    import win32crypt

    return win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1].decode("utf-8")
