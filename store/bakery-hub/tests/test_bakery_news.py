import datetime as dt
from email.utils import format_datetime
from unittest.mock import Mock

import bakery_news


def feed(*items):
    return ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()


def article(title, published, link="https://www.maff.go.jp/j/press/example.html"):
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<pubDate>{format_datetime(published)}</pubDate></item>")


def test_news_filters_old_unrelated_and_external_links():
    now = dt.datetime.now(dt.timezone.utc)
    rows = bakery_news._parse(feed(
        article("小麦価格の情報", now - dt.timedelta(hours=2)),
        article("小麦価格の古い情報", now - dt.timedelta(days=15)),
        article("無関係な情報", now - dt.timedelta(hours=1)),
        article("食品安全に関する情報", now, "https://evil.example/news"),
    ), now)
    assert [row["title"] for row in rows] == ["小麦価格の情報"]
    assert rows[0]["source"] == "農林水産省"


def test_news_cache_limits_requests_and_fails_closed(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    raw = feed(article("バター価格の情報", now))
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            assert size == bakery_news.MAX_BYTES + 1
            return raw

    def open_feed(request, timeout):
        calls.append(request.full_url)
        assert timeout == 4
        return Response()

    monkeypatch.setattr(bakery_news, "urlopen", open_feed)
    bakery_news._cache.update(until=dt.datetime.min.replace(tzinfo=dt.timezone.utc), items=[])
    assert len(bakery_news.get_news()) == 1
    assert len(bakery_news.get_news()) == 1
    assert calls == [bakery_news.FEED_URL]
    bakery_news._cache["until"] = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    monkeypatch.setattr(bakery_news, "urlopen", Mock(side_effect=OSError("unavailable")))
    assert bakery_news.get_news() == []


def test_home_news_failure_does_not_block_page(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(bakery_news, "urlopen", Mock(side_effect=AssertionError("home must not request RSS")))
    assert authenticated_client.get("/home").status_code == 200
    assert "BakeryNews" in authenticated_client.get("/home").get_data(as_text=True)
