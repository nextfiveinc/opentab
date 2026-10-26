"""OpenRouter client: one key, many models. Structured output, retried once, cached by content hash."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from engine.http import post_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SAVE_EVERY = 20


class LLMError(Exception):
    pass


class OpenRouter:
    def __init__(self, model: str, cache_path: Path | None = None):
        self.model = model
        self.cache_path = cache_path
        self._cache: dict[str, Any] = {}
        self._unsaved = 0
        if cache_path and cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._cache = {}
        import atexit

        atexit.register(self.flush)  # a crash mid-run keeps what was paid for

    @property
    def available(self) -> bool:
        return bool(os.environ.get("OPENROUTER_API_KEY"))

    def _cache_key(self, system: str, user: str, schema: dict | None = None) -> str:
        # The schema participates in the key: a changed output contract must not
        # hit a stale cached response.
        fingerprint = json.dumps(schema, sort_keys=True) if schema else ""
        return hashlib.sha1(f"{self.model}|{system}|{user}|{fingerprint}".encode()).hexdigest()

    def complete(self, system: str, user: str, schema: dict | None = None,
                 temperature: float = 0.1) -> Any:
        """Returns parsed JSON (if a schema is given) or text. One retry on failure.
        Results are cached by content hash, including structured (schema) calls —
        classification is the expensive path and must not be re-billed on repeats."""
        import httpx

        key = self._cache_key(system, user, schema)
        if key in self._cache:
            return self._cache[key]

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "classification", "strict": True, "schema": schema},
            }

        last_error = ""
        for attempt in range(2):
            try:
                with httpx.Client() as client:
                    data = post_json(
                        OPENROUTER_URL, client, body,
                        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                    )
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content) if schema else content
                self._cache[key] = parsed
                self._unsaved += 1
                if self._unsaved >= SAVE_EVERY:
                    self.flush()
                return parsed
            except (RuntimeError, KeyError, TypeError, ValueError, httpx.HTTPError) as e:
                # httpx errors (timeouts, DNS, resets) used to escape and abort the
                # whole run; callers fall back to rules on LLMError.
                last_error = str(e)
        raise LLMError(f"OpenRouter failed after retry: {last_error[:300]}")

    def flush(self) -> None:
        """Write the cache. Called every SAVE_EVERY new results and at exit: the
        whole file used to be rewritten after every single call."""
        if not self._unsaved:
            return
        self._unsaved = 0
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
