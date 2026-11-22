"""可复用小工具：URL 归一化与中文二元组相似度（纯标准库，其他仓可直接照抄）。

- normalize_url_for_id：去 fragment、去追踪参数、去协议和尾斜杠，域名小写、路径保留大小写。
- bigrams：中文取相邻两字；英文按词并去停用词；数字/百分比整体保留。
- similarity：共同二元组 / 较短一方的二元组数，适合判断「同一件事两种标题」。

不含任何业务规则；阈值由调用方配置。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: 不影响内容身份的追踪参数。
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "referrer", "spm", "from", "share_source", "_source",
    "fbclid", "gclid", "mc_cid", "mc_eid",
}

#: 英文停用词：只在算相似度时丢掉，不做任何业务判断。
STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "for", "on", "at", "by", "with", "and",
    "or", "is", "are", "was", "were", "be", "been", "this", "that", "it", "as",
}

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_url_for_id(url: str) -> str:
    """事件去重用的 URL 归一化；不能解析时退回去空白原文。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")
    if not parts.netloc:
        return raw.split("#", 1)[0].rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    # scheme 留空 = 无视 http/https；netloc 小写；path 保留大小写。
    return urlunsplit(("", parts.netloc.lower(), parts.path.rstrip("/"),
                       urlencode(query), ""))


def bigrams(text: str) -> set[str]:
    """中文二元组 + 英文实词 + 数字/百分比，返回集合。"""
    value = (text or "").strip()
    out: set[str] = set()
    # 数字/百分比先整体抽出，避免被中文二元组切开。
    for number in _NUMBER_RE.findall(value):
        out.add("n:" + number)
    # 中文连续串取相邻两字；单字也算一个组，短标题才有得比。
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", value):
        if len(run) == 1:
            out.add("c:" + run)
        for i in range(len(run) - 1):
            out.add("c:" + run[i:i + 2])
    # 英文按词，小写去停用词。
    for word in _LATIN_RE.findall(value):
        low = word.lower()
        if low not in STOPWORDS and len(low) > 1:
            out.add("w:" + low)
    return out


def similarity(left: str, right: str) -> float:
    """共同二元组 / 较短一方的二元组总数；0~1。"""
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
