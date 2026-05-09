import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from binance_ai.models import NewsItem
from binance_ai.news.service import NewsService


class NewsServiceTests(unittest.TestCase):
    def test_collect_uses_cache_before_refresh_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = NewsService(cache_path=Path(tmpdir) / "news_cache.json", refresh_seconds=120)
            item = NewsItem(
                source="CoinDesk",
                title="XRP rises",
                url="https://example.com/xrp",
                published_at_ms=1,
                category="media",
                matched_keywords=["XRP"],
            )
            with patch("binance_ai.news.service.fetch_binance_announcements", return_value=[]), \
                 patch("binance_ai.news.service.fetch_coindesk_rss", return_value=[item]), \
                 patch("binance_ai.news.service.fetch_cointelegraph_rss", return_value=[]):
                first = service.collect_for_symbols(["XRPJPY"], "JPY")
                second = service.collect_for_symbols(["XRPJPY"], "JPY")

            self.assertEqual(first.refresh_status, "REFRESHED")
            self.assertEqual(second.refresh_status, "CACHED")
            self.assertEqual(len(second.items), 1)

    def test_collect_refreshes_again_after_cache_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = NewsService(cache_path=Path(tmpdir) / "news_cache.json", refresh_seconds=10)
            item = NewsItem(
                source="CoinDesk",
                title="XRP rises again",
                url="https://example.com/xrp2",
                published_at_ms=1,
                category="media",
                matched_keywords=["XRP"],
            )
            with patch("binance_ai.news.service.fetch_binance_announcements", return_value=[]), \
                 patch("binance_ai.news.service.fetch_coindesk_rss", return_value=[item]), \
                 patch("binance_ai.news.service.fetch_cointelegraph_rss", return_value=[]), \
                 patch("binance_ai.news.service.time.time", side_effect=[1000, 1012]):
                first = service.collect_for_symbols(["XRPJPY"], "JPY")
                second = service.collect_for_symbols(["XRPJPY"], "JPY")

            self.assertEqual(first.refresh_status, "REFRESHED")
            self.assertEqual(second.refresh_status, "REFRESHED")


if __name__ == "__main__":
    unittest.main()
