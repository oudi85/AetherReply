"""Update the small set of config.toml fields exposed by the desktop UI."""

import json
import os
import re
from pathlib import Path

from auto_reply.config import PROJECT_ROOT, load_config


CONFIG_PATH = PROJECT_ROOT / "config.toml"


def _replace_value(section: str, key: str, value: str) -> None:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    block = re.search(rf"(?ms)^\[{re.escape(section)}\]\s*\n(.*?)(?=^\[|\Z)", raw)
    if block is None:
        raise ValueError(f"配置中没有 [{section}]")
    body = block.group(1)
    line = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    replacement = f"{key} = {value}"
    if line.search(body):
        body = line.sub(lambda _: replacement, body, count=1)
    else:
        body = replacement + "\n" + body
    updated = raw[:block.start(1)] + body + raw[block.end(1):]
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def set_reasoning_effort(value: str) -> None:
    if value not in ("low", "high", "max"):
        raise ValueError("思考强度只能是 low、high 或 max")
    _replace_value("llm", "reasoning_effort", json.dumps(value))


def set_thinking(enabled: bool) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("深度思考开关必须是布尔值")
    _replace_value("llm", "thinking", "true" if enabled else "false")


def set_allowlist(talkers: list[str]) -> None:
    clean = list(dict.fromkeys(t.strip() for t in talkers if t.strip()))
    if len(clean) > 30 or any("\n" in t or "\r" in t for t in clean):
        raise ValueError("联系人列表无效")
    _replace_value("auto_send", "allow_talkers",
                   json.dumps(clean, ensure_ascii=False))


def current_allowlist() -> list[str]:
    return list(load_config()["auto_send"].get("allow_talkers", []))
