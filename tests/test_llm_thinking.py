"""The DeepSeek thinking toggle and effort reach the API payload."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_reply.llm import chat


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}]}'


def main():
    cfg = {"llm": {"base_url": "https://api.deepseek.com",
                   "api_key": "test-only", "model": "deepseek-flash",
                   "thinking": True, "reasoning_effort": "high"}}
    captured = []

    def fake_open(request, **_):
        captured.append(json.loads(request.data))
        return Response()

    with patch("urllib.request.urlopen", fake_open):
        for effort in ("low", "high", "max"):
            cfg["llm"]["reasoning_effort"] = effort
            assert chat(cfg, [{"role": "user", "content": "test"}], retries=0) == "ok"
    assert [p["reasoning_effort"] for p in captured] == ["low", "high", "max"]
    assert all(p["thinking"] == {"type": "enabled"} for p in captured)
    assert all("temperature" not in p and p["max_tokens"] >= 4096 for p in captured)
    cfg["llm"]["thinking"] = False
    with patch("urllib.request.urlopen", fake_open):
        assert chat(cfg, [{"role": "user", "content": "test"}], retries=0) == "ok"
    assert captured[-1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured[-1]
    assert captured[-1]["temperature"] == 0.7
    print("[+] DeepSeek 思考模式与强度参数测试通过")


if __name__ == "__main__":
    main()
