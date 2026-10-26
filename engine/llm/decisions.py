"""OpenRouter Decisions API client: decision models answer questions with probabilities.

Every decision model on OpenRouter speaks the same contract (POST /api/alpha/decisions,
question types `noul`, `choice`, `score`), so the model is pure configuration: put any id
from `list_decision_models()` (or `python -m engine models`) in the pack's decide.yml.
Nothing here depends on which model answers. Thresholds do: they belong to the model
build they were tuned on, which is why every answer records the exact build
(`response.model`) and why a swap should be followed by `engine eval`.

The chat-completions client (openrouter.py) cannot call these models: they are rejected
on /chat/completions. The endpoint is alpha, so responses are validated strictly and any
mismatch is an error, never a silent default.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=decisions"
SAVE_EVERY = 20
QUESTION_TYPES = ("noul", "choice", "score")


class DecisionError(Exception):
    pass


@dataclass
class Decision:
    """Normalised answers: noul -> float P(true); choice -> {"choice", "probabilities"};
    score -> {"level", "legend", "probabilities"}. `model` is the build that answered."""

    answers: dict[str, Any]
    model: str = ""
    cost: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def _normalise(qid: str, question: dict, answer: Any) -> Any:
    qtype = question["type"]
    if not isinstance(answer, dict) or answer.get("type") != qtype:
        raise DecisionError(f"answer {qid!r}: expected type {qtype!r}, got {answer!r:.120}")
    if qtype == "noul":
        p = answer.get("noul")
        if not isinstance(p, (int, float)):
            raise DecisionError(f"answer {qid!r}: missing noul probability")
        return float(p)
    if qtype == "choice":
        choice = answer.get("choice")
        if choice not in (question.get("criteria") or {}):
            raise DecisionError(f"answer {qid!r}: choice {choice!r} is not an offered option")
        return {"choice": choice, "probabilities": answer.get("probabilities") or {choice: 1.0}}
    # score: pick a level, never treat the expectation as a quantity (not calibrated)
    levels = question.get("criteria") or []
    probs = answer.get("probabilities") or {}
    if probs:
        level = int(max(probs, key=lambda k: probs[k]))
    elif isinstance(answer.get("score"), (int, float)):
        level = round(answer["score"])
    else:
        raise DecisionError(f"answer {qid!r}: missing score")
    return {"level": max(0, min(level, len(levels) - 1)), "legend": answer.get("legend"),
            "probabilities": probs}


def build_request(model: str, state: Any, questions: dict[str, dict]) -> dict:
    """Wire format. Questions are sent with only the keys the API defines."""
    body_q = {}
    for qid, q in questions.items():
        if q.get("type") not in QUESTION_TYPES:
            raise DecisionError(f"question {qid!r}: type must be one of {QUESTION_TYPES}")
        body_q[qid] = {k: q[k] for k in ("type", "instructions", "criteria") if k in q}
    return {"model": model, "state": state, "questions": body_q}


class DecisionClient:
    def __init__(self, model: str, cache_path: Path | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not model:
            raise DecisionError("no decision model configured (decide.yml: model)")
        self.model = model
        self.cache_path = cache_path
        self.timeout = timeout
        self._transport = transport  # tests inject a mock transport
        self._cache: dict[str, Any] = {}
        self._unsaved = 0
        self.spent = 0.0
        self.calls = 0
        if cache_path and cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._cache = {}
        import atexit

        atexit.register(self.flush)

    @property
    def available(self) -> bool:
        return bool(os.environ.get("OPENROUTER_API_KEY")) or self._transport is not None

    def decide(self, state: Any, questions: dict[str, dict]) -> Decision:
        body = build_request(self.model, state, questions)
        key = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        data = self._cache.get(key)
        if data is None:
            data = self._post(body)
            self._cache[key] = data
            self._unsaved += 1
            if self._unsaved >= SAVE_EVERY:
                self.flush()
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise DecisionError("response has no answers object")
        out = {}
        for qid, q in questions.items():
            if qid not in answers:
                raise DecisionError(f"answer {qid!r} missing from response")
            out[qid] = _normalise(qid, q, answers[qid])
        return Decision(out, model=data.get("model", self.model),
                        cost=float((data.get("usage") or {}).get("cost") or 0.0), raw=data)

    def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"}
        last = ""
        for _ in range(2):  # one retry on network errors, 429 and 5xx
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    r = client.post(DECISIONS_URL, json=body, headers=headers)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                continue
            if r.status_code >= 400:
                # 4xx is a contract problem (bad model id, not a decision model, bad
                # question) and will not fix itself on retry.
                raise DecisionError(f"HTTP {r.status_code} from decisions API: {r.text[:300]}")
            try:
                data = r.json()
            except ValueError as e:
                raise DecisionError(f"non-JSON response: {e}") from e
            self.calls += 1
            self.spent += float((data.get("usage") or {}).get("cost") or 0.0)
            return data
        raise DecisionError(f"decisions API failed after retry: {last}")

    def flush(self) -> None:
        if not self._unsaved or not self.cache_path:
            return
        self._unsaved = 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")


def list_decision_models(timeout: float = 30.0) -> list[dict]:
    """OpenRouter's live catalog of decision models (public; no key needed)."""
    r = httpx.get(CATALOG_URL, timeout=timeout)
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        pricing = m.get("pricing") or {}
        out.append({
            "id": m.get("id"),
            "build": m.get("canonical_slug"),
            "alias_of": (m.get("alias_target") or {}).get("slug"),
            "context": m.get("context_length"),
            "usd_per_m_input": round(float(pricing.get("prompt") or 0) * 1e6, 4),
            "usd_per_m_output": round(float(pricing.get("completion") or 0) * 1e6, 4),
        })
    return out
