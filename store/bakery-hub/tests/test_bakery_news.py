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
        article("小麦価格の古い情報", now - dt.timedelta(days=31)),
        article("無関係な情報", now - dt.timedelta(hours=1)),
        article("食品安全に関する情報", now, "https://evil.example/news"),
    ), now)
    assert [row["title"] for row in rows] == ["小麦価格の情報"]
    assert rows[0]["source"] == "農林水産省"


def test_rdf_date_and_https_source_validation():
    now = dt.datetime.now(dt.timezone.utc)
    raw = ("<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
           "xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<item><title>食品表示の更新</title>"
           "<link>https://www.maff.go.jp/j/press/example.html</link>"
           f"<dc:date>{now.isoformat()}</dc:date></item>"
           "<item><title>パンに関する偽記事</title>"
           "<link>https://www.maff.go.jp.evil.example/</link>"
           f"<dc:date>{now.isoformat()}</dc:date></item></rdf:RDF>").encode()
    assert [row["title"] for row in bakery_news._parse(raw, now)] == ["食品表示の更新"]


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

    Response.geturl = lambda self: bakery_news.FEED_URL
    monkeypatch.setattr(bakery_news, "FEEDS", bakery_news.FEEDS[:1])
    monkeypatch.setattr(bakery_news, "_open_feed", open_feed)
    bakery_news._cache[bakery_news.FEED_URL].update(until=dt.datetime.min.replace(tzinfo=dt.timezone.utc), items=[])
    assert len(bakery_news.get_news()) == 1
    assert len(bakery_news.get_news()) == 1
    assert calls == [bakery_news.FEED_URL]
    bakery_news._cache[bakery_news.FEED_URL]["until"] = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    monkeypatch.setattr(bakery_news, "_open_feed", Mock(side_effect=OSError("unavailable")))
    assert bakery_news.get_news() == []


def test_multiple_sources_atom_dates_dedup_and_isolated_failure(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    atom = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>ベーグル新商品</title>"
            "<link href='https://shokuhin.net/story/'/><published>" + now.isoformat() +
            "</published></entry></feed>").encode()
    rss = feed(article("パンの新店舗オープン", now, "https://www.ssnp.co.jp/story/"))
    class Response:
        def __init__(self, url, body): self.url, self.body = url, body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def geturl(self): return self.url
        def read(self, size): return self.body[:size]
    calls = []
    def open_feed(request, timeout):
        calls.append(request.full_url)
        if request.full_url == bakery_news.FEED_URL:
            raise OSError("MAFF temporarily unavailable")
        return Response(request.full_url, atom if 'shokuhin.net' in request.full_url else rss)
    monkeypatch.setattr(bakery_news, "_open_feed", open_feed)
    for cached in bakery_news._cache.values():
        cached.update(until=dt.datetime.min.replace(tzinfo=dt.timezone.utc), items=[])
    rows = bakery_news.get_news(15)
    assert {row['source'] for row in rows} == {'食品新聞', '食品産業新聞社'}
    assert {row['topic'] for row in rows} == {'人気・トレンド', '新店舗・地域'}
    assert len(calls) == len(bakery_news.FEEDS)
    assert bakery_news.get_news(15) == rows
    assert len(calls) == len(bakery_news.FEEDS)
    assert bakery_news._parse(atom.replace(b'shokuhin.net', b'shokuhin.net.evil.example'), now,
                              '食品新聞', ('shokuhin.net',)) == []


def test_home_news_failure_does_not_block_page(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(bakery_news, "_open_feed", Mock(side_effect=AssertionError("home must not request RSS")))
    assert authenticated_client.get("/home").status_code == 200
    assert "BakeryNews" in authenticated_client.get("/home").get_data(as_text=True)
