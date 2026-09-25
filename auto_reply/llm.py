"""OpenAI 兼容 chat/completions 客户端（纯标准库实现）。"""

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

_SSL_CTX = ssl.create_default_context()


class LLMError(RuntimeError):
    pass


def chat(cfg: dict, messages: list, *, temperature: float = 0.7,
         max_tokens: int = 2048, retries: int = 2) -> str:
    base = cfg["llm"]["base_url"].rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": cfg["llm"]["model"],
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if cfg["llm"].get("thinking"):
        effort = cfg["llm"].get("reasoning_effort", "high")
        if effort not in ("low", "high", "max"):
            raise ValueError("reasoning_effort 只能是 low、high 或 max")
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort
        payload["max_tokens"] = max(max_tokens, 4096)
    else:
        host = urllib.parse.urlparse(base).hostname or ""
        if host == "deepseek.com" or host.endswith(".deepseek.com"):
            # DeepSeek defaults to high-effort thinking when omitted.
            payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = temperature
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['llm']['api_key']}",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise LLMError(f"LLM 请求失败（{url}）: {last_err}")


_JSON_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def chat_json(cfg: dict, messages: list, **kw) -> dict | list:
    """要求模型返回 JSON 并解析；容忍 markdown 代码块包裹。"""
    text = chat(cfg, messages, **kw)
    m = _JSON_RE.search(text)
    raw = m.group(1) if m else text
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
    if start > 0:
        raw = raw[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 返回的不是合法 JSON: {e}\n---\n{text[:500]}")


def redact(text: str) -> str:
    """发样本给 LLM 前的最低限度脱敏。"""
    text = re.sub(r"1[3-9]\d{9}", "<手机号>", text)
    text = re.sub(r"\d{17}[\dXx]", "<身份证号>", text)
    text = re.sub(r"\b\d{16,19}\b", "<卡号>", text)
    return text
