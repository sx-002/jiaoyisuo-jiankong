"""跨轮状态：去重、升级、源健康、关键词日额度、观察名单、cron 间隔。

状态文件提交在仓库的 state 分支（不是 Actions cache），避免缓存被逐出后每轮重发。
时间窗默认 7 天；推送失败不写入 seen，下轮重试；dry-run 写入模拟推送状态便于验证。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import rules, textsim

UTC = timezone.utc
LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}
RETENTION_DAYS = 7


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


#: 同一交易所、标题相似度超过这个值就当同一件事（可配置，其他仓照抄）。
TITLE_SIMILARITY_THRESHOLD = 0.6


def normalize_url(url: str) -> str:
    """兼容旧 state 的归一化，新代码请改用 textsim.normalize_url_for_id。"""
    return textsim.normalize_url_for_id(url)


GENERIC_TITLE_RE = re.compile(r"^(?:交易所的?公告|官方公告|公告|通知|title[:：].*)$", re.I)


def normalize_title(title: str) -> str:
    """标题归一：去标点、空白、大小写；用于同轮与跨轮的标题相似去重。"""
    return re.sub(r"[^\w\u3400-\u9fff]+", "", (title or "").lower())


def url_fingerprint(url: str) -> str:
    """URL 指纹：优先取文章长数字 id（API json 与页面地址共用），否则取末段路径。"""
    raw = normalize_url(url)
    if not raw:
        return ""
    path = raw.split("?", 1)[0]
    ids = re.findall(r"\d{6,}", path)
    if ids:
        return "id:" + ids[-1]
    segment = path.rstrip("/").split("/")[-1]
    segment = re.sub(r"\.(?:json|html?|shtml|xml|php)$", "", segment, flags=re.I)
    return "path:" + segment if segment else ""


def _digest(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def event_id(exchange: str, url: str, title: str) -> str:
    """主事件 id：交易所 + 归一化 URL；无有效链接时退回标题前缀。"""
    key = textsim.normalize_url_for_id(url)
    if key:
        return _digest(f"{exchange}|{key}")
    cleaned = normalize_title(title)[:24]
    return _digest(f"{exchange}|title|{cleaned}")


def legacy_event_id(exchange: str, url: str, title: str) -> str:
    """旧版 id，仅用于认出历史 state 里已推过的条目。"""
    key = normalize_url(url)
    if not key:
        cleaned = re.sub(r"[^\w\u3400-\u9fff]+", "", title).lower()
        key = f"{exchange}|{cleaned}"
    return _digest(key)


def event_keys(exchange: str, url: str, title: str) -> List[str]:
    """同一事件的所有别名：URL 指纹、标题前缀、旧 id，任一命中就算同一条。"""
    keys = {event_id(exchange, url, title), legacy_event_id(exchange, url, title)}
    fingerprint = url_fingerprint(url)
    if fingerprint:
        keys.add(_digest(f"alias|{exchange}|{fingerprint}"))
    cleaned = normalize_title(title)
    if cleaned:
        keys.add(_digest(f"alias|{exchange}|title|{cleaned[:24]}"))
    return sorted(keys)


def state_similar(left: str, right: str) -> bool:
    """同一件事的两种标题：二元组相似度过阈值，且没有相反词/不同数字。"""
    a, b = rules.clean_title_for_match(left), rules.clean_title_for_match(right)
    if not a or not b or rules.event_conflict(a, b):
        return False
    return textsim.similarity(a, b) >= TITLE_SIMILARITY_THRESHOLD


def title_specificity(title: str) -> int:
    """标题信息量：具体标题优先于「交易所的公告」这类泛标题。"""
    if GENERIC_TITLE_RE.match((title or "").strip()):
        return 0
    return 1 + len(normalize_title(title))


def merge_duplicates(candidates: List["Candidate"]) -> Tuple[List["Candidate"], List[dict]]:
    """同轮同事件只留信息最全的一条；返回（保留列表，被合并记录）。"""
    kept: List[Candidate] = []
    merged: List[dict] = []
    for candidate in candidates:
        item = candidate.item
        keys = set(event_keys(item.exchange, item.url, item.title))
        hit = None
        for existing in kept:
            other = existing.item
            if set(event_keys(other.exchange, other.url, other.title)) & keys:
                hit = existing
                break
            # 同交易所 + 标题二元组高度相似 = 同一件事的不同写法。
            if item.exchange == other.exchange and state_similar(item.title, other.title):
                hit = existing
                break
        if hit is None:
            kept.append(candidate)
            continue
        score_new = (LEVEL_RANK.get(candidate.decision.level, 0),
                     title_specificity(item.title), len(item.summary or ""))
        score_old = (LEVEL_RANK.get(hit.decision.level, 0),
                     title_specificity(hit.item.title), len(hit.item.summary or ""))
        merged.append({"exchange": item.exchange, "title": item.title[:200],
                        "url": item.url, "kept_title": hit.item.title[:200]})
        if score_new > score_old:
            index = kept.index(hit)
            kept[index] = candidate
            merged[-1]["kept_title"] = item.title[:200]
    return kept, merged


class State:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: Dict[str, dict] = {}
        self.pending: Dict[str, dict] = {}
        self.source_failures: Dict[str, dict] = {}
        self.daily_queries: Dict[str, Dict[str, str]] = {}
        self.watchlist: Dict[str, dict] = {}
        self.runs: List[dict] = []
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "State":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = dict(raw.get("seen") or {})
        self.pending = dict(raw.get("pending") or {})
        self.source_failures = dict(raw.get("source_failures") or {})
        self.daily_queries = dict(raw.get("daily_queries") or {})
        self.watchlist = dict(raw.get("watchlist") or {})
        self.runs = list(raw.get("runs") or [])[-48:]
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "schema_version": "1.0.0",
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "seen": self.seen,
            "pending": self.pending,
            "source_failures": self.source_failures,
            "daily_queries": self.daily_queries,
            "watchlist": self.watchlist,
            "runs": self.runs[-48:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_run(self, started_at: str) -> float:
        prior = _parse(self.last_run)
        current = _parse(started_at)
        gap = round((current - prior).total_seconds() / 60, 1) if prior and current else -1
        self.runs.append({"at": started_at, "gap_minutes": gap})
        self.last_run = started_at
        return gap

    def add_source_status(self, name: str, ok: bool, error_type: str = "", error: str = "") -> Optional[str]:
        """返回健康告警文本（连续 3 轮失败时），恢复后清除。"""
        entry = dict(self.source_failures.get(name) or {})
        if ok:
            if entry.get("count", 0) >= 3:
                self.source_failures[name] = {"count": 0, "recovered_at": _now(), "last_error": ""}
                return f"来源恢复：{name}"
            self.source_failures.pop(name, None)
            return None
        count = int(entry.get("count") or 0) + 1
        self.source_failures[name] = {
            "count": count, "error_type": error_type, "last_error": error[:300], "at": _now()
        }
        if count == 3:
            return f"来源连续 3 轮失败：{name}（{error_type}）"
        return None

    def is_used_today(self, query: str, today: str) -> bool:
        return query in self.daily_queries.get(today, {})
    def mark_queries(self, queries: List[str], today: str) -> None:
        bucket = self.daily_queries.setdefault(today, {})
        for query in queries:
            bucket[query] = _now()

    def add_watch(self, exchange: str, official: bool, url: str) -> bool:
        if official or not exchange:
            return False
        if exchange in self.watchlist:
            return False
        self.watchlist[exchange] = {"first_seen": _now(), "url": url, "note": "名单外，仅线索"}
        return True

    def status_for(self, item, decision) -> str:
        """新增/升级/已解除/跳过；跳过表示已推过且没有变化。"""
        keys = event_keys(item.exchange, item.url, item.title)
        previous = next((self.seen[key] for key in keys if key in self.seen), None)
        if not previous:
            return "新增"
        old_level = str(previous.get("level") or "low")
        if decision.resolved:
            # 解除事件只推一次：上轮已经报过解除就不再重复推。
            if previous.get("resolved"):
                return "跳过"
            if old_level in ("medium", "high"):
                return "已解除"
            return "跳过"
        if LEVEL_RANK.get(decision.level, 0) > LEVEL_RANK.get(old_level, 0):
            return "升级"
        return "跳过"

    def mark_pushed(self, item, decision, status: str) -> None:
        def field(obj, name, default=""):
            return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
        exchange = field(item, "exchange")
        url = field(item, "url")
        title = field(item, "title")
        published = field(item, "published_at")
        meta = {
            "exchange": exchange, "title": (title or "")[:300], "url": url,
            "level": field(decision, "level"), "status": status,
            "stage": field(decision, "stage", 0),
            "resolved": bool(field(decision, "resolved", False)),
            "published_at": published, "first_seen": _now(), "pushed_at": _now(),
        }
        for key in event_keys(exchange, url, title):
            self.seen[key] = dict(meta)

    # ---- 待重推：推送失败但还没送达的条目，必须下轮继续试，不能被时间窗吃掉 ----

    def add_pending(self, *, item_id: str, url: str, title: str, message: str,
                    exchange: str, error: str = "") -> None:
        entry = dict(self.pending.get(item_id) or {})
        entry.update({
            "url": url, "title": title[:300], "message": message, "exchange": exchange,
            "first_failed_at": entry.get("first_failed_at") or _now(),
            "last_failed_at": _now(), "last_error": str(error)[:200],
        })
        self.pending[item_id] = entry
        if len(self.pending) > 50:
            excess = len(self.pending) - 50
            oldest = sorted(self.pending.items(),
                            key=lambda kv: kv[1].get("first_failed_at") or "")[:excess]
            for key, _ in oldest:
                self.pending.pop(key, None)

    def pending_list(self) -> List[dict]:
        return sorted(self.pending.values(), key=lambda e: e.get("first_failed_at") or "")

    def clear_pending(self, item_ids: List[str]) -> None:
        for item_id in item_ids:
            self.pending.pop(item_id, None)

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        removed = 0
        for key, meta in list(self.seen.items()):
            stamp = _parse(str(meta.get("first_seen") or ""))
            if stamp is not None and stamp < cutoff:
                self.seen.pop(key, None)
                removed += 1
        for key, meta in list(self.pending.items()):
            stamp = _parse(str(meta.get("first_failed_at") or ""))
            if stamp is not None and stamp < cutoff:
                self.pending.pop(key, None)
        today = datetime.now(UTC).date()
        for day in list(self.daily_queries):
            parsed = None
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                pass
            if parsed is None or (today - parsed).days > RETENTION_DAYS:
                self.daily_queries.pop(day, None)
        return removed
