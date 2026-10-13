"""農林水産省の公開RSSから、パン店に関係する見出しとリンクだけを取得する。"""

import datetime as dt
import threading
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FEED_URL = "https://www.maff.go.jp/j/press/rss.xml"
SOURCE = "農林水産省"
CACHE_SECONDS = 1800
FAILURE_SECONDS = 300
MAX_AGE_DAYS = 14
MAX_BYTES = 256 * 1024
KEYWORDS = (
    "小麦", "麦粉", "バター", "乳製品", "パン", "製パン", "菓子", "食品産業",
    "食品表示", "食品衛生", "食中毒", "食品安全", "原材料", "砂糖", "価格", "食料品",
)

_lock = threading.Lock()
_cache = {"until": dt.datetime.min.replace(tzinfo=dt.timezone.utc), "items": []}


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
        except (TypeError, ValueError):
            return None
    if date.tzinfo is None:
        return None
    return date.astimezone(dt.timezone.utc)


def _parse(raw, now):
    root = ET.fromstring(raw)
    results = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        title = _value(item, "title")[:160]
        link = _value(item, "link")
        published = _published(_value(item, "pubDate") or _value(item, "date"))
        parsed = urlparse(link)
        if (not title or not any(word in title for word in KEYWORDS)
                or parsed.scheme != "https" or parsed.hostname not in ("maff.go.jp", "www.maff.go.jp")
                or parsed.username or parsed.password or not published
                or not dt.timedelta(0) <= now - published <= dt.timedelta(days=MAX_AGE_DAYS)):
            continue
        results.append({"title": title, "url": link, "published": published.isoformat(), "source": SOURCE})
    return sorted(results, key=lambda item: item["published"], reverse=True)[:15]


def get_news(limit=3):
    """短時間キャッシュする。外部通信が失敗した場合は空一覧を返す。"""
    now = dt.datetime.now(dt.timezone.utc)
    with _lock:
        if now >= _cache["until"]:
            try:
                request = Request(FEED_URL, headers={"User-Agent": "BakeryHub/1.0 (news reader; https://github.com/tosane932/tosane-works)"})
                with urlopen(request, timeout=4) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("RSS response too large")
                _cache["items"] = _parse(raw, now)
                _cache["until"] = now + dt.timedelta(seconds=CACHE_SECONDS)
            except (OSError, ValueError, ET.ParseError):
                _cache["items"] = []
                _cache["until"] = now + dt.timedelta(seconds=FAILURE_SECONDS)
        # キャッシュ中にも記事の経過日数を確認し、古い見出しを最新扱いしない。
        fresh = [item for item in _cache["items"] if
                 _published(item["published"]) and
                 now - _published(item["published"]) <= dt.timedelta(days=MAX_AGE_DAYS)]
        return fresh[:limit]
