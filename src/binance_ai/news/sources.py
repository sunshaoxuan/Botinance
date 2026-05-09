from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import List
from urllib.request import Request, urlopen

from binance_ai.models import NewsItem


def _http_get_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 Codex BinanceAI/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_coindesk_rss(limit: int = 12) -> List[NewsItem]:
    xml_text = _http_get_text("https://www.coindesk.com/arc/outboundfeeds/rss")
    root = ET.fromstring(xml_text)
    items: List[NewsItem] = []
    for node in root.findall("./channel/item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        published_ms = _parse_pub_date_ms(pub_date)
        if title and link:
            items.append(
                NewsItem(
                    source="CoinDesk",
                    title=title,
                    url=link,
                    published_at_ms=published_ms,
                    category="media",
                )
            )
    return items


def fetch_cointelegraph_rss(limit: int = 12) -> List[NewsItem]:
    xml_text = _http_get_text("https://cointelegraph.com/rss")
    root = ET.fromstring(xml_text)
    items: List[NewsItem] = []
    for node in root.findall("./channel/item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        published_ms = _parse_pub_date_ms(pub_date)
        if title and link:
            items.append(
                NewsItem(
                    source="Cointelegraph",
                    title=title,
                    url=link,
                    published_at_ms=published_ms,
                    category="media",
                )
            )
    return items


def fetch_binance_announcements(limit_per_catalog: int = 6) -> List[NewsItem]:
    catalogs = [
        ("Binance Listing", 48),
        ("Binance Delisting", 161),
    ]
    items: List[NewsItem] = []
    for source_name, catalog_id in catalogs:
        url = (
            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
            f"?type=1&catalogId={catalog_id}&pageNo=1&pageSize={limit_per_catalog}"
        )
        raw = _http_get_text(url)
        payload = json.loads(raw)
        catalogs_payload = (((payload.get("data") or {}).get("catalogs")) or [])
        for catalog in catalogs_payload:
            for article in catalog.get("articles", []):
                code = article.get("code", "")
                title = str(article.get("title", "")).strip()
                published_ms = int(article.get("releaseDate") or 0)
                if title and code:
                    items.append(
                        NewsItem(
                            source=source_name,
                            title=title,
                            url=f"https://www.binance.com/en/support/announcement/{code}",
                            published_at_ms=published_ms,
                            category="official",
                        )
                    )
    return items


def _parse_pub_date_ms(value: str) -> int:
    if not value:
        return int(time.time() * 1000)
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)
