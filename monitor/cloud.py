"""第二、三层：云端搜索与定点读取。

第一层 Exa 公共 MCP（免 key，只当第一层，入口 https://mcp.exa.ai/mcp）；
失败或限额后用 Anysearch 正式 API（4 个账号轮换，密钥只从环境变量读，绝不落盘）。
X 单帖先 XActions 免费 x_post，失败换 x-reader 公开接口；两者都低频顺序调用。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from . import http, rules, sources

EXA_MCP = "https://mcp.exa.ai/mcp"
ANYSEARCH_DEFAULT_BASE_URL = "https://api.anysearch.com"
XACTIONS_MCP = "https://xactions.app/mcp"
XREADER_API = "https://xreader.leolabs.me/api/read"

DISCOVERY_QUERIES = [
    ("中文", "交易所跑路"), ("中文", "限制提币"), ("中文", "暂停提币"), ("中文", "无法提币"),
    ("中文", "提币不到账"), ("中文", "交易所维护"), ("中文", "维护延期"), ("中文", "限制提现"),
    ("中文", "暂停提现"), ("英文", "exchange withdrawal suspended"),
    ("英文", "exchange exit scam"), ("英文", "exchange maintenance extended"),
]
VERIFY_WORDS = ["提现暂停", "暂停提币", "维护延期", "登录不了",
                "withdrawal suspended", "maintenance extended"]
JSONRPC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": http.BROWSER_UA,
    "Origin": "https://mcp.exa.ai",
}

#: 搜索起点：上次运行时间往前重叠 30 分钟；停摆超过 3 天则回看 3 天，避免断档。
OVERLAP_MINUTES = 30
MAX_LOOKBACK_DAYS = 3


@dataclass
class SearchHit:
    query: str
    engine: str
    url: str
    title: str
    snippet: str = ""
    published_at: str = ""

    def to_dict(self) -> dict:
        return {"query": self.query, "engine": self.engine, "url": self.url,
                "title": self.title, "snippet": self.snippet[:300]}


@dataclass
class CloudReport:
    hits: List[SearchHit] = field(default_factory=list)
    queries_used: List[str] = field(default_factory=list)
    engines: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    since: str = ""

    def to_dict(self) -> dict:
        return {"since": self.since, "queries": self.queries_used, "engine_counts": self.engines,
                "hits": len(self.hits), "errors": self.errors[:8], "skipped": self.skipped[:8]}


def search_window(
    last_run: str,
    *,
    now: Optional[datetime] = None,
    overlap_minutes: int = OVERLAP_MINUTES,
    max_lookback_days: int = MAX_LOOKBACK_DAYS,
) -> Tuple[datetime, str]:
    """从 state.last_run 推本次搜索起点；首次运行或停摆过久自动回看 max_lookback_days。"""
    now = now or datetime.now(timezone.utc)
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat((last_run or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed > now + timedelta(minutes=5)
        or (now - parsed) > timedelta(days=max_lookback_days)
    ):
        parsed = now - timedelta(days=max_lookback_days)
    start = parsed - timedelta(minutes=overlap_minutes)
    return start, start.strftime("%Y-%m-%d %H:%M UTC")


def _build_objective(since_text: str) -> str:
    return (
        f"只返回 {since_text} 之后新发布或新更新的原文帖子、官方公告；"
        f"不要返回 {since_text} 之前发布的旧文章、帮助页、科普文、FAQ、历史公告或常驻页面；"
        "每条结果必须有明确发布时间；排除行情预测、价格讨论和无来源转述。"
    )


def _parse_published_time(raw: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """把命中里的时间串解析成 UTC；只有明确时间才参与新窗口判断。"""
    text = (raw or "").strip()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:40], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    relative = re.search(r"(\d+)\s*(minute|hour|day)s?\s*ago", text, re.I)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = {"minute": timedelta(minutes=amount),
                 "hour": timedelta(hours=amount),
                 "day": timedelta(days=amount)}[unit]
        return now - delta
    return None


def _parse_sse_or_json(payload: str):
    if payload.lstrip().startswith("{"):
        return json.loads(payload)
    messages = []
    for line in payload.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                try:
                    messages.append(json.loads(raw))
                except ValueError:
                    continue
    return messages[-1] if messages else {}


def mcp_call(endpoint: str, tool_name: str, arguments: dict, *, timeout: float = 25.0,
             origin: str = "https://mcp.exa.ai") -> str:
    """Streamable HTTP MCP：initialize → initialized → tools/call，返回文本结果。"""
    headers = dict(JSONRPC_HEADERS)
    headers["Origin"] = origin
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "jiaoyisuo-jiankong", "version": "1.0.0"}},
    }
    init = http.fetch(endpoint, method="POST", body=json.dumps(init_payload).encode(),
                      headers=headers, timeout=timeout, retries=1, max_bytes=1_000_000)
    if not init.ok:
        raise RuntimeError(f"MCP 初始化失败: {init.error_type} {init.error[:120]}")
    # 公共 MCP 支持无状态 tools/call；初始化只用来确认服务可用。
    try:
        _parse_sse_or_json(init.text())
    except ValueError:
        pass
    call_payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    result = http.fetch(endpoint, method="POST", body=json.dumps(call_payload).encode(),
                        headers=headers, timeout=timeout, retries=1, max_bytes=2_000_000)
    if not result.ok:
        raise RuntimeError(f"MCP {tool_name} 失败: {result.error_type} {result.error[:120]}")
    try:
        data = _parse_sse_or_json(result.text())
    except ValueError as exc:
        raise RuntimeError(f"MCP {tool_name} 返回非 JSON") from exc
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict) and item.get("id") == 2),
                    data[-1] if data and isinstance(data[-1], dict) else {})
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"MCP {tool_name} 报错: {str(data)[:200]}")
    payload = (data.get("result") or {}).get("content") or []
    texts = [str(block.get("text") or "") for block in payload if isinstance(block, dict)]
    return "\n".join(text for text in texts if text)


_MD_LINK_RE = re.compile(r"\[([^\]\n]{4,200})\]\((https?://[^\s)]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>\"']{10,500}")

_MONTHS = {name: idx for idx, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1)}


def _json_date(*texts: str) -> str:
    """Anysearch 的 JSON 结果没有发布时间字段，从标题/摘要里补一个明确日期。"""
    blob = " ".join(str(item or "") for item in texts)
    found = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", blob)
    if found:
        year, month, day = re.split(r"[-/.]", found.group(0))
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T00:00:00+00:00"
    found = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(20\d{2})\b", blob)
    if found:
        month = _MONTHS.get(found.group(1)[:3].lower())
        if month:
            return f"{found.group(3)}-{month:02d}-{int(found.group(2)):02d}T00:00:00+00:00"
    found = re.search(r"\d+\s*(?:minute|hour|day)s?\s*ago", blob, re.I)
    return found.group(0).lower() if found else ""


def _parse_exa_block(text: str, query: str, engine: str) -> List[SearchHit]:
    """Exa 公共 MCP 返回的是 Title/URL/Published/Highlights 文本块，逐块拆成干净命中。"""
    hits: List[SearchHit] = []
    chunks = re.split(r"\n(?=Title:)", text)
    for chunk in chunks:
        title_match = re.search(r"^Title:\s*(.+)$", chunk, re.M)
        url_match = re.search(r"^URL:\s*(https?://\S+)", chunk, re.M)
        if not url_match:
            continue
        title = (title_match.group(1).strip() if title_match else "")[:300]
        published_match = re.search(r"^Published:\s*(.+)$", chunk, re.M)
        published = ""
        if published_match:
            raw = published_match.group(1).strip()
            if raw and raw.upper() != "N/A":
                published = raw[:40]
        highlight_match = re.search(r"^Highlights:\s*(.*)$", chunk, re.M | re.S)
        snippet = highlight_match.group(1).strip()[:1000] if highlight_match else ""
        date = re.search(r"20\d{2}-\d{1,2}-\d{1,2}|20\d{2}/\d{1,2}/\d{1,2}", snippet)
        if published or date:
            published = published or (date.group(0) if date else "")
        hits.append(SearchHit(query, engine, url_match.group(1).rstrip(".,;)"),
                              title, snippet, published))
    return hits[:8]


def _hits_from_text(text: str, query: str, engine: str) -> List[SearchHit]:
    hits = _parse_exa_block(text, query, engine)
    if hits:
        return hits
    seen = set()
    try:
        parsed = json.loads(text)

        def walk(value):
            if isinstance(value, dict):
                url = str(value.get("url") or value.get("link") or "")
                if url.startswith("http") and url not in seen:
                    seen.add(url)
                    title = str(value.get("title") or value.get("name") or "")
                    snippet = str(value.get("snippet") or value.get("text")
                                  or value.get("content") or "")
                    published = (str(value.get("published") or value.get("date") or "")
                                 or _json_date(title, snippet))
                    hits.append(SearchHit(query, engine, url, title[:300],
                                          snippet[:800], published[:60]))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(parsed)
    except (ValueError, TypeError):
        pass
    for match in _MD_LINK_RE.finditer(text):
        url = match.group(2)
        if url in seen:
            continue
        seen.add(url)
        nearby = text[max(0, match.start() - 400): match.end() + 700]
        date = re.search(r"20\d{2}-\d{1,2}-\d{1,2}|20\d{2}/\d{1,2}/\d{1,2}", nearby)
        hits.append(SearchHit(query, engine, url, match.group(1), nearby[:800],
                              date.group(0) if date else ""))
    if not hits:
        for match in _URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;)")
            if url in seen:
                continue
            seen.add(url)
            hits.append(SearchHit(query, engine, url, "", text[:500]))
    return hits[:8]


def exa_search(query: str, since_text: str) -> List[SearchHit]:
    text = mcp_call(EXA_MCP, "web_search_exa",
                    {"query": query, "objective": _build_objective(since_text), "numResults": 5})
    return _hits_from_text(text, query, "exa_public")


def _anysearch_keys() -> List[str]:
    raw = os.environ.get("ANYSEARCH_KEYS", "")
    return [key.strip() for key in raw.replace(";", ",").split(",") if key.strip()]


def anysearch_search(query: str, key: str, since_text: str) -> List[SearchHit]:
    url = f"{os.environ.get('ANYSEARCH_BASE_URL', ANYSEARCH_DEFAULT_BASE_URL).rstrip('/')}/v1/search"
    scoped_query = (f"{query}；只返回 {since_text} 之后新发布的内容，"
                    f"排除旧文章、帮助页、科普文和常驻页面")
    payload = json.dumps({"query": scoped_query, "max_results": 5}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": http.BROWSER_UA,
               "Authorization": f"Bearer {key}", "X-API-Key": key}
    result = http.fetch(url, method="POST", body=payload, headers=headers, timeout=25, retries=1)
    if not result.ok:
        raise RuntimeError(f"Anysearch {result.error_type}: {result.error[:120]}")
    return _hits_from_text(result.text(), scoped_query, "anysearch")


def build_query_plan(daily_used: Dict[str, str], max_queries: int) -> List[Tuple[str, str]]:
    """同一关键词当天只搜一次；先风险词发现，再交易所+风险词核实。"""
    plan: List[Tuple[str, str]] = []
    for query in [q for _, q in DISCOVERY_QUERIES]:
        if query not in daily_used:
            plan.append(("discovery", query))
        if len(plan) >= max(1, max_queries // 2):
            break
    # 验证路轮流覆盖全部 15 家，避免每轮都把额度花在同一批交易所上。
    offset = len(daily_used) % len(sources.OFFICIAL_EXCHANGES)
    rotated = sources.OFFICIAL_EXCHANGES[offset:] + sources.OFFICIAL_EXCHANGES[:offset]
    for exchange in rotated:
        word = VERIFY_WORDS[(len(daily_used) + len(plan)) % len(VERIFY_WORDS)]
        query = f"{exchange} {word}"
        if query not in daily_used:
            plan.append(("verify", query))
        if len(plan) >= max_queries:
            break
    return plan


def run_cloud(
    daily_used: Dict[str, str],
    *,
    max_queries: int = 12,
    enabled: bool = True,
    since_text: str = "",
) -> CloudReport:
    report = CloudReport()
    report.since = since_text or search_window("")[1]
    if not enabled:
        report.skipped.append("已用 --no-cloud 关闭云端搜索")
        return report
    keys = _anysearch_keys()
    for kind, query in build_query_plan(daily_used, max_queries):
        try:
            hits = exa_search(query, report.since)
            if not hits and keys:
                hits = anysearch_search(query, keys[0], report.since)
        except Exception as exc:  # noqa: BLE001 云搜索失败留给 Anysearch 兜底
            message = f"{query}: {type(exc).__name__} {str(exc)[:120]}"
            if keys:
                try:
                    hits = anysearch_search(query, keys[0], report.since)
                    report.errors.append(message + " → Anysearch 兜底成功")
                except Exception as exc2:  # noqa: BLE001
                    report.errors.append(message + f" → Anysearch 也失败: {str(exc2)[:120]}")
                    hits = []
            else:
                report.errors.append(message + " → 未配置 ANYSEARCH_KEYS，无法兜底")
                hits = []
        if not hits:
            report.skipped.append(f"{query}: 无结果")
            continue
        report.hits.extend(hits)
        report.queries_used.append(query)
        report.engines[hits[0].engine] = report.engines.get(hits[0].engine, 0) + 1
    report.hits = _dedup_hits(report.hits)
    return report


def _dedup_hits(hits: List[SearchHit]) -> List[SearchHit]:
    seen = set()
    unique = []
    for hit in hits:
        key = re.sub(r"^https?://(www\.)?", "", hit.url).split("?")[0].lower()
        if key in seen or len(hit.url) > 500:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


EXCHANGE_ALIASES = {
    "binance": "Binance", "币安": "Binance", "okx": "OKX", "欧易": "OKX", "gate": "Gate",
    "bybit": "Bybit", "bitget": "Bitget", "coinbase": "Coinbase", "weex": "WEEX",
    "deepcoin": "Deepcoin", "easicoin": "EasiCoin", "bitbaby": "Bitbaby", "yubit": "YUBIT",
    "hotcoin": "Hotcoin", "bittap": "BitTap", "mgbx": "MGBX", "mexc": "MEXC",
    # 名单外的常见交易所/被提及的小所，只用于标注与观察，不建官方源。
    "kucoin": "KuCoin", "xt": "XT", "kraken": "Kraken", "bitfinex": "Bitfinex",
    "upbit": "Upbit Global", "basekx": "BaseKX", "aozx": "AOZX", "biking": "BiKing",
    "aeget": "AEGET", "tebbit": "Tebbit", "crypto.com": "Crypto.com", "coinw": "CoinW",
    "coinp": "CoinP", "ascendex": "AscendEX", "bitmart": "BitMart", "htx": "HTX",
}
_ALIAS_PATTERNS = [
    (name, re.compile((r"(?<![A-Za-z0-9])" if len(alias) <= 3 else r"")
                      + re.escape(alias)
                      + (r"(?![A-Za-z0-9])" if len(alias) <= 3 else r""), re.I))
    for alias, name in sorted(EXCHANGE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
]


def detect_exchange(text: str) -> str:
    for name, pattern in _ALIAS_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def hits_to_items(
    hits: List[SearchHit],
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> List[sources.Item]:
    """云搜索命中转事件：必须有交易所有名、链接、明确发布时间且在本次新窗口内。"""
    now = window_end or datetime.now(timezone.utc)
    items: List[sources.Item] = []
    for hit in hits:
        exchange = detect_exchange(hit.title) or detect_exchange(hit.snippet)
        if not exchange:
            continue
        if not hit.url.startswith(("http://", "https://")):
            continue
        if not hit.published_at:
            continue  # 没有发布时间的旧 FAQ / 帮助页不作为新信号
        published = _parse_published_time(hit.published_at, now)
        if published is None:
            continue
        if window_start is not None and published < window_start:
            continue  # 上次运行之前的内容不进候选
        if published > now + timedelta(minutes=10):
            continue  # 明显超前的时间不伪装成最新
        if re.search(r"/(help|learn|faq)/|/help$|support/.*/articles", hit.url, re.I):
            continue  # 静态帮助页不是用户端新抱怨
        text = f"{hit.title} {hit.snippet}"
        if not rules.judge(text) and not rules.RESOLVED_RE.search(text):
            continue
        items.append(sources.Item(
            exchange=exchange, title=hit.title or hit.snippet[:120], url=hit.url,
            published_at=hit.published_at, summary=hit.snippet[:1000],
            signal_type="用户端反馈（非官方公告）",
            source_name=f"cloud:{hit.engine}", source_layer="cloud",
            extra={"query": hit.query, "official": str(exchange in sources.OFFICIAL_EXCHANGES).lower()},
        ))
    return items


def read_x_post(url: str) -> str:
    """X 单帖：XActions 免费工具失败换 x-reader 公开接口；顺序低频调用。"""
    try:
        text = mcp_call(XACTIONS_MCP, "x_post", {"url": url},
                        origin="https://xactions.app", timeout=20)
        if text and "error" not in text[:50].lower():
            return text[:4000]
    except Exception:  # noqa: BLE001
        pass
    payload = json.dumps({"url": url}).encode()
    result = http.fetch(XREADER_API, method="POST", body=payload, timeout=25, retries=0,
                        headers={"Content-Type": "application/json", "User-Agent": http.BROWSER_UA})
    if not result.ok:
        return ""
    try:
        data = result.json()
    except ValueError:
        return result.text()[:4000]
    for key in ("content", "text", "body", "markdown"):
        value = data.get(key) if isinstance(data, dict) else None
        if value:
            return str(value)[:4000]
    return ""
