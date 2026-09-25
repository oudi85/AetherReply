"""配置加载：config.toml（项目根）> config.example.toml 默认值 > 环境变量。"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


DEFAULTS = {
    "wechat": {
        "data_dir": "",       # 空 = 自动探测
        "dbs": [],            # 空 = message/contact/session 全部
    },
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "api_key_file": "",
        "model": "glm-4-flash",
        "thinking": False,
        "reasoning_effort": "high",
        "redact_sensitive": True,
    },
    "sync": {
        "data_dir": "data",
        "poll_seconds": 5,
        "max_event_age_seconds": 300,
        "debounce_seconds": 2,
    },
    "auto_send": {
        "enabled": False,
        "allow_talkers": [],
    },
    "decision": {
        "provider": "default",  # 预留 jev；当前由 DeepSeek 生成，默认取第一条
    },
    "reply_plan": {
        "enabled": False,
        "max_parts": 3,
        "allow_stickers": False,
    },
    "stickers": {
        "vision_model": "",
        "vision_base_url": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = _deep_merge(DEFAULTS, {})
    path = PROJECT_ROOT / "config.toml"
    if not path.exists() and tomllib is not None:
        example = PROJECT_ROOT / "config.example.toml"
        if example.exists():
            with open(example, "rb") as f:
                cfg = _deep_merge(cfg, tomllib.load(f))
    if path.exists() and tomllib is not None:
        with open(path, "rb") as f:
            cfg = _deep_merge(cfg, tomllib.load(f))

    if env_key := os.environ.get("AUTO_REPLY_API_KEY"):
        cfg["llm"]["api_key"] = env_key
    elif not cfg["llm"]["api_key"] and cfg["llm"].get("api_key_file"):
        from .secret_store import load_key
        key_file = Path(cfg["llm"]["api_key_file"])
        if not key_file.is_absolute():
            key_file = PROJECT_ROOT / key_file
        cfg["llm"]["api_key"] = load_key(key_file)
    return cfg


def data_dir(cfg: dict) -> Path:
    p = Path(cfg["sync"]["data_dir"])
    return p if p.is_absolute() else PROJECT_ROOT / p


def require_llm(cfg: dict) -> None:
    if not cfg["llm"]["api_key"]:
        sys.exit(
            "[x] 未配置 LLM api_key。请在 config.toml [llm] 里填，"
            "或设置环境变量 AUTO_REPLY_API_KEY。"
        )
