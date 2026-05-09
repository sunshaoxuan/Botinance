from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from binance_ai.models import NewsCollectionResult, NewsItem
from binance_ai.news.sources import (
    fetch_binance_announcements,
    fetch_coindesk_rss,
    fetch_cointelegraph_rss,
)


class NewsService:
    def __init__(self, cache_path: Path, refresh_seconds: int) -> None:
        self.cache_path = cache_path
        self.refresh_seconds = max(10, refresh_seconds)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def collect_for_symbols(self, symbols: Sequence[str], quote_asset: str) -> NewsCollectionResult:
        now_ms = int(time.time() * 1000)
        cached = self._read_cache()
        if cached is not None:
            age_seconds = max(0, (now_ms - cached["last_updated_ms"]) // 1000)
            if age_seconds < self.refresh_seconds:
                return NewsCollectionResult(
                    items=self._deserialize_items(cached["items"]),
                    refresh_status="CACHED",
                    last_updated_ms=int(cached["last_updated_ms"]),
                    next_refresh_ms=int(cached["last_updated_ms"]) + self.refresh_seconds * 1000,
                )

        candidates: List[NewsItem] = []
        for fetcher in (fetch_binance_announcements, fetch_coindesk_rss, fetch_cointelegraph_rss):
            try:
                candidates.extend(fetcher())
            except Exception:
                continue

        filtered = self._filter_relevant(candidates, symbols=symbols, quote_asset=quote_asset)
        seen = set()
        deduped: List[NewsItem] = []
        for item in filtered:
            key = (item.source, item.title)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        deduped.sort(key=lambda item: item.published_at_ms, reverse=True)
        final_items = deduped[:12]
        self._write_cache(final_items, now_ms)
        return NewsCollectionResult(
            items=final_items,
            refresh_status="REFRESHED",
            last_updated_ms=now_ms,
            next_refresh_ms=now_ms + self.refresh_seconds * 1000,
        )

    def _filter_relevant(
        self,
        items: Iterable[NewsItem],
        symbols: Sequence[str],
        quote_asset: str,
    ) -> List[NewsItem]:
        keywords = self._keywords(symbols, quote_asset)
        filtered: List[NewsItem] = []
        for item in items:
            text = f"{item.title} {item.url}".upper()
            matches = [keyword for keyword in keywords if keyword in text]
            if item.category == "official" or matches:
                filtered.append(
                    NewsItem(
                        source=item.source,
                        title=item.title,
                        url=item.url,
                        published_at_ms=item.published_at_ms,
                        category=item.category,
                        matched_keywords=matches,
                    )
                )
        return filtered

    @staticmethod
    def _keywords(symbols: Sequence[str], quote_asset: str) -> List[str]:
        keys = {"BINANCE", quote_asset.upper()}
        for symbol in symbols:
            normalized = symbol.upper()
            keys.add(normalized)
            if normalized.endswith(quote_asset.upper()):
                base = normalized[: -len(quote_asset)]
                if base:
                    keys.add(base)
        return sorted(keys)

    def _read_cache(self) -> Optional[Dict[str, object]]:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, items: List[NewsItem], updated_ms: int) -> None:
        payload = {
            "last_updated_ms": updated_ms,
            "items": [asdict(item) for item in items],
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _deserialize_items(raw_items: List[Dict[str, object]]) -> List[NewsItem]:
        return [
            NewsItem(
                source=str(item.get("source", "")),
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                published_at_ms=int(item.get("published_at_ms", 0)),
                category=str(item.get("category", "")),
                matched_keywords=[str(value) for value in item.get("matched_keywords", [])],
            )
            for item in raw_items
        ]
