from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from binance_ai.config import Settings


class OllamaChatClient:
    provider = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.llm_fallback_model
        self.endpoint = self._build_chat_endpoint(settings.llm_fallback_base_url)
        self.last_provider = self.provider
        self.last_model = self.model

    @staticmethod
    def _build_chat_endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/api/chat"):
            return base
        return f"{base}/api/chat"

    def chat(self, messages: List[Dict[str, str]]) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": self.settings.llm_fallback_num_predict,
                },
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.settings.llm_fallback_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Ollama connection error: {exc}") from exc

        decoded: Dict[str, Any] = json.loads(body)
        message = decoded.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        raise RuntimeError(f"Unsupported Ollama response format: {decoded}")


class FallbackChatClient:
    def __init__(self, primary: object | None, fallback: object | None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_provider = ""
        self.last_model = ""
        self.last_error = ""

    def chat(self, messages: List[Dict[str, str]]) -> str:
        if self.primary is not None:
            try:
                result = self.primary.chat(messages)
                self.last_provider = str(getattr(self.primary, "provider", "openai_compat"))
                self.last_model = str(getattr(self.primary, "model", ""))
                self.last_error = ""
                return result
            except Exception as exc:
                self.last_error = str(exc)
                if self.fallback is None:
                    raise

        if self.fallback is None:
            raise RuntimeError("No LLM client available.")
        result = self.fallback.chat(messages)
        self.last_provider = str(getattr(self.fallback, "provider", "fallback"))
        self.last_model = str(getattr(self.fallback, "model", ""))
        return result
