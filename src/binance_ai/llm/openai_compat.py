from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from binance_ai.config import Settings


class OpenAICompatibleChatClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoint = self._build_chat_endpoint(settings.llm_base_url)

    @staticmethod
    def _build_chat_endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def chat(self, messages: List[Dict[str, str]]) -> str:
        payload = json.dumps(
            {
                "model": self.settings.llm_model,
                "messages": messages,
                "temperature": 0.2,
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Connection error: {exc}") from exc

        decoded: Dict[str, Any] = json.loads(body)
        choices = decoded.get("choices") or []
        if not choices:
            raise RuntimeError(f"Missing choices in LLM response: {decoded}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "\n".join(text for text in texts if text)
        raise RuntimeError(f"Unsupported LLM response format: {decoded}")

