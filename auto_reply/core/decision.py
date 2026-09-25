"""Candidate selection boundary; a Jev ranker can be plugged in here later."""

from dataclasses import dataclass

from . import voice


@dataclass(frozen=True)
class Selection:
    index: int
    provider: str


class DecisionUnavailable(RuntimeError):
    pass


def select_candidate(cfg: dict, con, talker: str, candidates: list[dict],
                     analysis: str = "") -> Selection:
    """Return an index after validating that there is a usable candidate.

    The Jev adapter will receive the same conversation and candidate list here.
    It must return a valid index; if unavailable, sending stops before UIA.
    """
    if not candidates:
        raise ValueError("模型没有返回候选回复")
    provider = cfg.get("decision", {}).get("provider", "default")
    if provider == "default":
        ranked = voice.best_candidate(con, talker, candidates)
        if ranked is not None:
            return Selection(ranked, provider)
        for index, candidate in enumerate(candidates):
            if isinstance(candidate, dict) and isinstance(candidate.get("text"), str):
                if candidate["text"].strip():
                    return Selection(index, provider)
        raise ValueError("模型没有返回可用的文字回复")
    if provider == "jev":
        raise DecisionUnavailable("Jev 接口已预留，尚未配置可用服务")
    raise ValueError(f"未知的判断来源: {provider}")
