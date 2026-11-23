"""第一层：15 家官方监控名单的公开源适配器。

官方名单只决定这里抓谁；名单外交易所一律不建官方条目。
每个源标注层级：api > rss_json > html > blocked（预期不可直读，只靠云端搜索兜底）。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin

from . import http

CST = timezone(timedelta(hours=8))


@dataclass
class Item:
    exchange: str
    title: str
    url: str
    published_at: str = ""
    summary: str = ""
    body: str = ""
    signal_type: str = "官方公告"
    source_name: str = ""
    source_layer: str = "api"
    extra: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange, "title": self.title, "url": self.url,
            "published_at": self.published_at, "summary": self.summary,
            "signal_type": self.signal_type, "source_name": self.source_name,
            "source_layer": self.source_layer, "extra": self.extra,
        }


@dataclass
class OfficialSource:
    name: str
    exchange: str
    layer: str
    url: str
    kind: str
    note: str = ""
    headers: Optional[Dict[str, str]] = None


# 官方监控名单 15 家。Gate 在 GitHub 出口实测 403，保留条目但标 blocked，
# 按纪律记失败并交由云端搜索兜底，不把它伪装成抓到的空列表。
SOURCES: List[OfficialSource] = [
    OfficialSource("binance_api", "Binance", "api",
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20",
        "binance", "公告页 202 空内容，API 已实测可用"),
    OfficialSource("okx_api", "OKX", "api",
        "https://www.okx.com/api/v5/support/announcements", "okx", "pTime 毫秒，annType 区分充提"),
    OfficialSource("bybit_api", "Bybit", "api",
        "https://api.bybit.com/v5/announcements/index?locale=zh-TW&limit=20", "bybit",
        "首次 403、再次 200，不稳定源"),
    OfficialSource("bitget_html", "Bitget", "html",
        "https://www.bitget.com/support/sections/12508313443483", "html", "列表页实测 200"),
    OfficialSource("gate_blocked", "Gate", "blocked",
        "https://www.gate.com/announcements", "blocked", "GitHub 出口 403，只靠云端搜索兜底"),
    OfficialSource("coinbase_status", "Coinbase", "rss_json",
        "https://status.coinbase.com/history.rss", "rss", "状态页 RSS 实测 200"),
    OfficialSource("coinbase_status_json", "Coinbase", "rss_json",
        "https://status.coinbase.com/api/v2/summary.json", "coinbase_status", "逐组件状态"),
    OfficialSource("weex_html", "WEEX", "html",
        "https://www.weex.com/zh-CN/product-launch", "html", "产品公告页实测 200"),
    OfficialSource("yubit_sitemap", "YUBIT", "rss_json",
        "https://yubit.gitbook.io/yubit/cn/sitemap-pages.xml", "yubit", "sitemap 筛公告节点"),
    OfficialSource("deepcoin_html", "Deepcoin", "html",
        "https://www.deepcoin.com/ps/zh/announcements", "html", "可达但列表需渲染，可能 low_quality"),
    OfficialSource("easicoin_zendesk", "EasiCoin", "api",
        "https://easicoin.zendesk.com/api/v2/help_center/zh-cn/articles.json?per_page=5", "zendesk", ""),
    OfficialSource("bitbaby_html", "Bitbaby", "html",
        "https://www.bitbaby.com/en-us/announcement", "html", "日期只到天"),
    OfficialSource("hotcoin_html", "Hotcoin", "html",
        "https://www.hotcoin.com/en_US/announcement", "html", "可达但地区限制可能导致内容不全"),
    OfficialSource("bittap_zendesk", "BitTap", "api",
        "https://support.bittap.com/api/v2/help_center/zh-cn/articles.json?per_page=5", "zendesk", ""),
    OfficialSource("mgbx_zendesk", "MGBX", "api",
        "https://support.mgbx.com/api/v2/help_center/zh-cn/articles.json?per_page=5", "zendesk", ""),
    OfficialSource("mexc_api", "MEXC", "api",
        "https://www.mexc.com/help/announce/api/en-US/section/15425930840826/articles", "mexc",
        "维护/充提公告；名单内，近期被美国管控，重点看监管与提现"),
    OfficialSource("mexc_delisting_api", "MEXC", "api",
        "https://www.mexc.com/help/announce/api/en-US/section/15425930840822/articles", "mexc",
        "下架与风险提示"),
    OfficialSource("mexc_telegram", "MEXC", "telegram_preview",
        "https://t.me/s/MEXC_Announcement", "html",
        "公开预览兜底（帖子在 div 里，需专用解析）"),
]

OFFICIAL_EXCHANGES = sorted({s.exchange for s in SOURCES})


#: 源健康态：ok=有数据，empty=请求成功但 0 条（正常），
#: partial_failure=部分源挂了，failure=全挂（要告警）。
SOURCE_OK = "ok"
SOURCE_EMPTY = "empty"
SOURCE_PARTIAL = "partial_failure"
SOURCE_FAILURE = "failure"


def source_health(reports) -> str:
    """按一轮的源报告给出整体健康态；空源不算故障。"""
    rows = list(reports)
    if not rows:
        return SOURCE_EMPTY
    if all(not r.ok for r in rows):
        return SOURCE_FAILURE
    if any(not r.ok for r in rows):
        return SOURCE_PARTIAL
    return SOURCE_OK


@dataclass
class SourceReport:
    name: str
    exchange: str
    layer: str
    ok: bool
    status: str = ""
    error: str = ""
    items: List[Item] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def health(self) -> str:
        """单源健康态：成功有数据=ok，成功 0 条=empty，失败=partial_failure。"""
        if self.ok:
            return SOURCE_EMPTY if not self.items else SOURCE_OK
        return SOURCE_PARTIAL

    def to_dict(self) -> dict:
        return {
            "name": self.name, "exchange": self.exchange, "layer": self.layer,
            "status": self.status, "health": self.health,
            "error": self.error[:300], "items": len(self.items),
            "duration_ms": self.duration_ms,
        }


def _first(mapping: dict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _walk(value) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _timestamp(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        number = int(float(raw))
        if number > 10_000_000_000:
            number //= 1000
        return datetime.fromtimestamp(number, CST).isoformat()
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)
        return parsed.astimezone(CST).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(raw[:30], fmt).replace(tzinfo=CST).isoformat()
        except ValueError:
            continue
    return raw[:40]


def _json_items(data, exchange: str, src: OfficialSource) -> List[Item]:
    items: List[Item] = []
    seen = set()
    for node in _walk(data):
        title = _first(node, "title", "titleEn", "titleZh", "name", "subject")
        link = _first(node, "url", "link", "articleUrl", "html_url", "detailUrl", "href")
        if not title or not link:
            continue
        link = urljoin(src.url, link)
        if link in seen or len(title) > 500:
            continue
        seen.add(link)
        published = _timestamp(_first(node, "publishTime", "pTime", "published_at", "created_at",
                                      "publishDate", "date", "time", "updated_at"))
        summary = http.strip_tags(_first(node, "content", "summary", "body", "description"))[:2000]
        items.append(Item(exchange, title[:500], link, published, summary,
                          source_name=src.name, source_layer=src.layer))
        if len(items) >= 30:
            break
    return items


_ANCHOR_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
#: 列表页的导航/栏目占位，不是公告条目标题。
_NAV_TITLE_RE = re.compile(
    r"^(?:help\s*center|announcement\s*center|support\s*center|support|announcements?|"
    r"帮助中心|公告中心|支持中心|客服中心|更多|更多公告|查看全部)$", re.I)
_DATE_RE = re.compile(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}(?:[ 日T]\d{1,2}:\d{2}(?::\d{2})?)?)")
# Telegram 公开预览页：帖子在 div.tgme_widget_message 里，不是 a 标签，通用 HTML 解析抓不到。
_TG_POST_RE = re.compile(r'data-post="(?P<channel>[^"/]+)/(?P<pid>\d+)"')
_TG_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_TG_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)


#: 官方公告/帮助中心路径：HTML 列表页只收这些链接。
ANNOUNCEMENT_PATH_RE = re.compile(
    r"/(?:announcement|announcements|support|help|articles|notice|notices|"
    r"product-launch|newsflash|gong-gao|hc|status|incidents)(?:/|$)", re.I)
#: 新闻/资讯/博客/学院栏目：即使出现在公告页里也不收。
NON_ANNOUNCEMENT_PATH_RE = re.compile(
    r"/(?:news|blog|research|academy|insight|learn|article|articles?|media|"
    r"press|stories|topics)(?:/|$)", re.I)


def is_announcement_link(url: str) -> bool:
    """HTML 列表链接是否属于公告/帮助页。

    只收公告/帮助/文章/通知/产品公告这类路径；新闻、博客、学院、科普等
    栏目一律不收（即使它们出现在公告列表页里）。Telegram 公开预览页例外。
    """
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = parts.path or ""
    if parts.netloc.lower().endswith("t.me") and path.startswith("/s/"):
        return True
    return bool(ANNOUNCEMENT_PATH_RE.search(path))


def _html_items(text: str, exchange: str, src: OfficialSource) -> List[Item]:
    items: List[Item] = []
    seen = set()
    for href, inner in _ANCHOR_RE.findall(text):
        title = http.strip_tags(inner)
        if len(title) < 6 or len(title) > 300 or title.startswith("http"):
            continue
        if _NAV_TITLE_RE.match(title.strip()):
            continue  # 导航/栏目占位，不是公告条目
        link = urljoin(src.url, href)
        if link in seen or "#" in link:
            continue
        if not is_announcement_link(link):
            continue  # 新闻/博客/学院栏目不是公告，不收
        seen.add(link)
        date_match = _DATE_RE.search(text[max(0, text.find(href) - 160): text.find(href) + 400])
        published = _timestamp(date_match.group(1)) if date_match else ""
        items.append(Item(exchange, title, link, published, source_name=src.name, source_layer="html",
                          extra={"date_precision": "day" if len(published) <= 10 else "minute"}))
        if len(items) >= 30:
            break
    if not items:
        raise http.FetchFailure("HTML 列表没有解析出任何公告链接，疑似需要渲染", "low_quality")
    return items


def _telegram_preview_items(text: str, src: OfficialSource) -> List[Item]:
    """Telegram 公开预览页解析：t.me/s/<频道> 的帖子正文在 div 里。

    通用 HTML 解析只找 a 标签，这里按 data-post 切块，再取正文与时间。
    """
    marks = list(_TG_POST_RE.finditer(text))
    items: List[Item] = []
    seen = set()
    for index, mark in enumerate(marks):
        start = mark.end()
        end = marks[index + 1].start() if index + 1 < len(marks) else min(len(text), start + 20000)
        chunk = text[start:end]
        body_match = _TG_TEXT_RE.search(chunk)
        if not body_match:
            continue
        body = http.strip_tags(body_match.group(1))
        if len(body) < 8:
            continue
        link = f"https://t.me/{mark.group('channel')}/{mark.group('pid')}"
        if link in seen:
            continue
        seen.add(link)
        time_match = _TG_TIME_RE.search(chunk)
        published = _timestamp(time_match.group(1)) if time_match else ""
        title = body.split("。")[0].strip()[:160] or body[:160]
        items.append(Item(src.exchange, title, link, published, summary=body[:400],
                          source_name=src.name, source_layer="html",
                          extra={"date_precision": "minute" if published else "day"}))
        if len(items) >= 20:
            break
    if not items:
        raise http.FetchFailure("Telegram 预览页没有解析出帖子", "low_quality")
    return items


def _rss_items(data: bytes, exchange: str, src: OfficialSource) -> List[Item]:
    if re.search(br"<!\s*ENTITY\b", data[:4096], re.I):
        raise http.FetchFailure("XML 含 ENTITY 声明，拒绝解析", "unsafe_xml")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise http.FetchFailure(f"XML 解析失败: {exc}", "parse_error") from exc
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    items: List[Item] = []
    for node in nodes[:30]:
        def text(*tags):
            for tag in tags:
                found = node.find(tag)
                if found is not None:
                    return (found.text or found.attrib.get("href", "")).strip()
            return ""
        title = http.strip_tags(text("title", "{http://www.w3.org/2005/Atom}title"))
        link = text("link", "{http://www.w3.org/2005/Atom}link", "guid")
        if not title or not link:
            continue
        items.append(Item(exchange, title[:500], urljoin(src.url, link),
                          text("pubDate", "published", "updated"), source_name=src.name,
                          source_layer="rss_json"))
    if not items:
        raise http.FetchFailure("RSS 解析后为 0 条", "missing")
    return items


def _yubit_items(data: bytes, src: OfficialSource) -> List[Item]:
    text = data.decode("utf-8", "replace")
    paths = re.findall(r"<loc>([^<]+)</loc>", text)
    items = []
    for path in paths:
        if "gong-gao" not in path.lower() and "公告" not in path:
            continue
        md_url = path if path.endswith(".md") else path.rstrip("/") + ".md"
        page = http.fetch(md_url, timeout=10, retries=1, max_bytes=200_000,
                          headers={"Accept": "text/markdown"})
        if not page.ok:
            continue
        first_line = next((line.lstrip("# ").strip() for line in page.text().splitlines()
                           if line.strip().startswith("#")), "")
        title = first_line or md_url.rsplit("/", 1)[-1]
        items.append(Item(src.exchange, title[:500], path, source_name=src.name,
                          source_layer="rss_json", summary=f"来源：GitBook sitemap；正文：{md_url}"))
        if len(items) >= 20:
            break
    if not items:
        raise http.FetchFailure("sitemap 没有公告节点", "missing")
    return items


def _mexc_items(data, src: OfficialSource) -> List[Item]:
    """MEXC 栏目文章接口：data.results[]，routerUrl 是 /announcements/{id}。"""
    results = ((data.get("data") or {}).get("results") if isinstance(data, dict) else None) or []
    items: List[Item] = []
    seen = set()
    for row in results:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        article_id = str(row.get("id") or "").strip()
        router = str(row.get("routerUrl") or "").strip()
        if not title or not article_id:
            continue
        link = urljoin("https://www.mexc.com", router or f"/announcements/{article_id}")
        if link in seen:
            continue
        seen.add(link)
        published = _timestamp(str(row.get("createdAt") or row.get("updateTime") or ""))
        items.append(Item(src.exchange, title[:500], link, published,
                          source_name=src.name, source_layer=src.layer))
        if len(items) >= 30:
            break
    if not items:
        raise http.FetchFailure("MEXC 文章接口 data.results 为空，字段可能变化", "low_quality")
    return items


def _binance_items(data, src: OfficialSource) -> List[Item]:
    """币安公告 API 的文章只有 code 没有 url，需要按 code 拼详情链接。"""
    items: List[Item] = []
    seen = set()
    for node in _walk(data):
        title = _first(node, "title", "titleEn")
        code = _first(node, "code")
        if not title or not re.fullmatch(r"[0-9a-fA-F]{32}", code):
            continue
        link = f"https://www.binance.com/en/support/announcement/{code}"
        if link in seen:
            continue
        seen.add(link)
        items.append(Item(src.exchange, title[:500], link,
                          _timestamp(_first(node, "releaseDate", "publishTime")),
                          source_name=src.name, source_layer="api"))
        if len(items) >= 30:
            break
    return items


def parse_payload(src: OfficialSource, result: http.FetchResult) -> List[Item]:
    if src.kind == "blocked":
        raise http.FetchFailure("GitHub 出口实测 403，按预期跳过直抓", "blocked")
    if src.kind == "rss":
        return _rss_items(result.data, src.exchange, src)
    if src.kind == "yubit":
        return _yubit_items(result.data, src)
    if src.kind == "html":
        return _html_items(result.text(), src.exchange, src)
    if src.kind == "telegram_preview":
        return _telegram_preview_items(result.text(), src)
    data = result.json()
    if src.kind == "binance":
        return _binance_items(data, src)
    if src.kind == "mexc":
        return _mexc_items(data, src)
    return _json_items(data, src.exchange, src)


def fetch_source(src: OfficialSource, *, timeout: float = 15.0) -> SourceReport:
    started = __import__("time").time()
    if src.kind == "blocked":
        return SourceReport(src.name, src.exchange, src.layer, False, "blocked",
                            "GitHub 出口实测 403，按预期记失败，交云端搜索兜底")
    result = http.fetch(src.url, timeout=timeout, headers=src.headers, check_login_wall=True)
    if not result.ok:
        return SourceReport(src.name, src.exchange, src.layer, False, result.error_type,
                            result.error, duration_ms=result.duration_ms)
    try:
        items = parse_payload(src, result)
    except http.FetchFailure as exc:
        return SourceReport(src.name, src.exchange, src.layer, False, exc.error_type, str(exc),
                            duration_ms=int((__import__("time").time() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 单源解析异常必须被隔离
        return SourceReport(src.name, src.exchange, src.layer, False, "low_quality",
                            f"{type(exc).__name__}: {exc}",
                            duration_ms=int((__import__("time").time() - started) * 1000))
    if not items:
        return SourceReport(src.name, src.exchange, src.layer, True, SOURCE_EMPTY, "",
                            [], int((__import__("time").time() - started) * 1000))
    return SourceReport(src.name, src.exchange, src.layer, True, "ok", "", items,
                        int((__import__("time").time() - started) * 1000))


def fetch_body(url: str, *, timeout: float = 10.0) -> str:
    """命中风险词的条目才读正文；读不到就明确返回空，不假装看过。"""
    result = http.fetch(url, timeout=timeout, retries=1, max_bytes=600_000,
                        headers={"Accept": "text/html,application/xhtml+xml,text/plain"},
                        check_login_wall=True)
    if not result.ok:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", result.text(), flags=re.I | re.S)
    return http.strip_tags(text)[:8000]
