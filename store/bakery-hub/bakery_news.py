"""公開RSS/Atomの見出し、日付、記事へのリンクを集約する。"""
import datetime as dt
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

FEED_URL = "https://www.maff.go.jp/j/press/rss.xml"
SOURCE = "農林水産省"
FEEDS = (
    (FEED_URL, SOURCE, ("maff.go.jp", "www.maff.go.jp")),
    ("https://shokuhin.net/category/kakou/seifun/%E3%83%91%E3%83%B3/feed/", "食品新聞", ("shokuhin.net", "www.shokuhin.net")),
    ("https://www.ssnp.co.jp/feed/", "食品産業新聞社", ("ssnp.co.jp", "www.ssnp.co.jp")),
    ("https://www.ssnp.co.jp/rice/feed/", "食品産業新聞社", ("ssnp.co.jp", "www.ssnp.co.jp")),
)
CACHE_SECONDS, FAILURE_SECONDS, MAX_AGE_DAYS, MAX_BYTES = 1800, 300, 30, 256 * 1024
TOPICS = (
    ("新店舗・地域", ("新店", "開店", "オープン", "出店", "広島", "パンフェス", "パンのフェス")),
    ("消費者動向", ("消費者", "購買", "調査", "アンケート", "朝食", "食習慣", "需要")),
    ("材料・価格", ("小麦", "麦粉", "バター", "乳製品", "砂糖", "原材料", "製粉", "価格", "物流費")),
    ("業界全般", ("食品産業", "食品表示", "食品衛生", "食中毒", "食品安全", "商品開発", "製パン技術", "店舗運営")),
    ("人気・トレンド", ("パン", "ベーカリー", "ベーグル", "クロワッサン", "ブレッド", "製パン", "食パン")),
)
BAKERY_WORDS = ("パン", "ベーカリー", "ベーグル", "クロワッサン", "ブレッド", "製パン")
MATERIAL_WORDS = ("小麦", "麦粉", "バター", "乳製品", "砂糖", "製粉")
MARKET_WORDS = ("価格", "高騰", "需給", "供給", "値上", "調達", "輸入")
_lock = threading.Lock()
_cache = {url: {"until": dt.datetime.min.replace(tzinfo=dt.timezone.utc), "items": []} for url, _, _ in FEEDS}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _open_feed(request, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _value(item, name):
    for child in item:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


def _published(value):
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None
    return date.astimezone(dt.timezone.utc) if date.tzinfo else None


def _parse(raw, now, source=SOURCE, allowed_hosts=("maff.go.jp", "www.maff.go.jp")):
    results = []
    for item in ET.fromstring(raw).iter():
        if item.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = _value(item, "title")[:160]
        topic = next((name for name, words in TOPICS if any(word in title for word in words)), None)
        if source != SOURCE and source != "食品新聞" and not (
                any(word in title for word in BAKERY_WORDS)
                or (any(word in title for word in MATERIAL_WORDS) and any(word in title for word in MARKET_WORDS))):
            continue
        link = _value(item, "link")
        if not link:
            link = next((child.get("href", "") for child in item
                         if child.tag.rsplit("}", 1)[-1] == "link" and child.get("rel", "alternate") == "alternate"), "")
        published = _published(_value(item, "pubDate") or _value(item, "published")
                               or _value(item, "date") or _value(item, "updated"))
        parsed = urlparse(link)
        if (not topic or parsed.scheme != "https" or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not published or not dt.timedelta(0) <= now - published <= dt.timedelta(days=MAX_AGE_DAYS)):
            continue
        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
        results.append({"title": title, "url": clean_url, "published": published.isoformat(),
                        "source": source, "topic": topic})
    return sorted(results, key=lambda row: row["published"], reverse=True)[:40]


def _retrieve(feed, now):
    url, source, hosts = feed
    try:
        request = Request(url, headers={"User-Agent": "BakeryHub/1.0 (news reader; https://github.com/tosane932/tosane-works)"})
        with _open_feed(request, timeout=4) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in hosts:
                raise ValueError("Unexpected feed redirect")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("RSS response too large")
        return _parse(raw, now, source, hosts), CACHE_SECONDS
    except (OSError, ValueError, ET.ParseError):
        return [], FAILURE_SECONDS


def get_news(limit=3):
    """情報源別に失敗を隔離し、期限切れと重複を再確認する。"""
    now = dt.datetime.now(dt.timezone.utc)
    with _lock:
        combined = []
        expired = [feed for feed in FEEDS if now >= _cache.setdefault(
            feed[0], {"until": dt.datetime.min.replace(tzinfo=dt.timezone.utc), "items": []})["until"]]
        if expired:
            # A slow information source must not delay the others serially.
            with ThreadPoolExecutor(max_workers=len(expired)) as pool:
                refreshed = list(pool.map(lambda feed: _retrieve(feed, now), expired))
            for (url, _, _), (items, seconds) in zip(expired, refreshed):
                _cache[url].update(items=items, until=now + dt.timedelta(seconds=seconds))
        for url, source, hosts in FEEDS:
            cached = _cache[url]
            combined.extend(row for row in cached["items"] if
                            (published := _published(row["published"])) and
                            dt.timedelta(0) <= now - published <= dt.timedelta(days=MAX_AGE_DAYS))
        seen, results = set(), []
        for row in sorted(combined, key=lambda item: item["published"], reverse=True):
            if row["url"] not in seen:
                seen.add(row["url"])
                results.append(row)
        return results[:limit]
