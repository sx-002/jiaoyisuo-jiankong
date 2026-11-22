"""编排：官方源 → 云端搜索 → 规则 → 去重 → 消息 → 干跑/推送 → 状态。"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlsplit

from datetime import timedelta  # noqa: E402

from . import cloud, http, rules, sources, state as state_mod, telegram

CST = timezone(timedelta(hours=8))

MAX_PUSH = 10
EXIT_OK, EXIT_PARTIAL, EXIT_ALL_FAILED, EXIT_CONFIG = 0, 1, 2, 5
TITLE_HINT = [pattern for _, pattern, _, _, _ in rules.RULES] + [rules.RESOLVED_RE]


def _cst(raw: str) -> str:
    parsed = state_mod._parse(raw)
    return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M") if parsed else ""


def _has_title_hint(item) -> bool:
    text = f"{item.title} {item.summary}"
    return any(pattern.search(text) for pattern in TITLE_HINT)


def _load_fixtures(path: str):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    official = []
    for row in raw.get("official", []):
        official.append(sources.SourceReport(
            name=row.get("name", "fixture"), exchange=row.get("exchange", "Binance"),
            layer=row.get("layer", "fixture"), ok=True, status="ok",
            items=[sources.Item(**{k: v for k, v in row.items()
                                   if k in sources.Item.__dataclass_fields__})],
        ))
    hits = [cloud.SearchHit(**{k: v for k, v in row.items()
                               if k in cloud.SearchHit.__dataclass_fields__})
            for row in raw.get("cloud", [])]
    return official, hits


@dataclass
class Candidate:
    item: object
    decision: object
    status: str
    message: str


def run(*, fixtures: str = "", state_path: str = "state/state.json", report_path: str = "",
        max_queries: int = 12, no_cloud: bool = False, send: bool = False,
        max_push: int = MAX_PUSH) -> int:
    started = datetime.now(timezone.utc).isoformat()
    cloud_report = cloud.CloudReport()
    st = state_mod.State(state_path).load()
    last_run = st.last_run
    window_start, since_text = cloud.search_window(last_run)
    gap = st.record_run(started)
    print(f"本轮开始：{started}｜距上轮 {gap if gap >= 0 else '未知'} 分钟｜搜索起点 {since_text}")

    token = chat_id = ""
    if send:
        token, chat_id = telegram.credentials()
        if not token or not chat_id:
            print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
            return EXIT_CONFIG
        print("推送凭据：已读取（不回显）")

    if fixtures:
        reports, hits = _load_fixtures(fixtures)
        print(f"使用本地样例：官方条目 {len(reports)}｜云端命中 {len(hits)}")
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            reports = list(pool.map(sources.fetch_source, sources.SOURCES))
        cloud_report = cloud.run_cloud(st.daily_queries, max_queries=max_queries,
                                        enabled=not no_cloud, since_text=since_text)
        hits = cloud_report.hits
        print(f"云端搜索：{cloud_report.to_dict()}")
        st.mark_queries(cloud_report.queries_used, datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    items: List = []
    for report in reports:
        if not report.ok:
            # Gate 的 403 是实测已知边界，不作为「源坏了」反复告警。
            if not (report.name == "gate_blocked" and report.status == "blocked"):
                alert = st.add_source_status(report.name, False, report.status, report.error)
                if alert:
                    print(f"源健康告警：{alert}", file=sys.stderr)
            continue
        st.add_source_status(report.name, True, report.status)
        items.extend(report.items)

    cloud_items = cloud.hits_to_items(hits, window_start=window_start,
                                      window_end=datetime.now(timezone.utc))
    cloud_domains: dict = {}
    for item in cloud_items:
        host = (urlsplit(item.url).hostname or "").lower()
        cloud_domains.setdefault(item.exchange, set()).add(host)
    items.extend(cloud_items)
    empty_round = bool(reports) and not items and not fixtures
    if empty_round:
        print("警告：官方源与云端搜索均为 0 条，按异常空轮记录，0 条不等于正常。", file=sys.stderr)

    candidates: List[Candidate] = []
    body_reads = 0
    filtered_non_chinese = 0
    filtered_non_operational = 0
    for item in items:
        if not item.url or not item.url.startswith(("http://", "https://")):
            continue
        if not item.exchange:
            continue
        # 内容层：只要运营事件（提现/充值/维护/登录/下架/限额/监管/安全事故），
        # 行业新闻、科普、访谈、活动推广直接挡掉并计数。
        if not rules.is_operational_event(item.title, item.summary):
            filtered_non_operational += 1
            st.mark_pushed(item, rules.non_operational_decision(), "非运营")
            continue
        # 中文过滤：纯英文条目挡在推送之前（只影响是否推送，不改中文条目的分级与去重）。
        if rules.is_non_chinese(item.title, item.summary):
            filtered_non_chinese += 1
            st.mark_pushed(item, rules.non_chinese_decision(), "非中文")
            continue
        body = ""
        if item.source_layer not in ("cloud", "fixture") and _has_title_hint(item) and body_reads < 8:
            try:
                body = sources.fetch_body(item.url)
            except http.FetchFailure as exc:
                body = ""
                print(f"正文读取失败（{exc.error_type}）：{item.url[:120]}", file=sys.stderr)
            body_reads += 1
        independent = len(cloud_domains.get(item.exchange, set())) if item.source_layer == "cloud" else 1
        decision = rules.judge(item.title, item.summary, body,
                               cloud_sources=independent, cloud_users=0)
        if decision is None:
            continue
        if not body and decision.level in (rules.HIGH, rules.MEDIUM) and item.source_layer != "cloud":
            decision.reason += "；正文未取到，仅按标题与摘要判断"
        status = st.status_for(item, decision)
        if status == "跳过" or not decision.pushable:
            continue
        if any(key in st.pending for key in state_mod.event_keys(item.exchange, item.url, item.title)):
            continue  # 已经在待重推队列里，避免同一轮重复推送
        official = item.source_layer != "cloud"
        if not official:
            st.add_watch(item.exchange, item.exchange in sources.OFFICIAL_EXCHANGES, item.url)
        message = rules.format_message(
            exchange=item.exchange, title=item.title, published_cst=_cst(item.published_at),
            url=item.url, decision=decision, status=status, official=official,
        )
        candidates.append(Candidate(item, decision, status, message))

    candidates, merged_same_round = state_mod.merge_duplicates(candidates)
    deduped_same_round = len(merged_same_round)
    if deduped_same_round:
        print(f"同轮同事件合并 {deduped_same_round} 条：保留标题更具体/等级更高的一条")
    candidates.sort(key=lambda c: ({"high": 0, "medium": 1, "low": 2}.get(c.decision.level, 3),
                                   c.item.exchange))
    selected = candidates[:max_push]
    rest = candidates[max_push:]
    print(f"候选 {len(candidates)}｜本轮选入 {len(selected)}｜其余进汇总 {len(rest)}｜"
          f"纯英文挡下 {filtered_non_chinese}")

    all_failed = bool(reports) and all(not r.ok for r in reports)
    if all_failed:
        print("全部官方源同时失败，单独报警。", file=sys.stderr)

    pushed = []
    failed = []
    pending_before = st.pending_list()
    if pending_before:
        print(f"待重推条目：{len(pending_before)}")
    if send:
        queue: List[Tuple[str, object]] = [(entry["message"], entry) for entry in pending_before]
        queue.extend((candidate.message, candidate) for candidate in selected)
        for message, ref in queue:
            try:
                telegram.send_message(token, chat_id, message)
                if isinstance(ref, Candidate):
                    st.mark_pushed(ref.item, ref.decision, ref.status)
                    pushed.append(ref.item.exchange + "｜" + ref.item.title[:60])
                else:
                    st.clear_pending(state_mod.event_keys(ref.get("exchange", ""), ref.get("url", ""),
                                                          ref.get("title", "")))
                    pushed.append("重试｜" + str(ref.get("title", ""))[:60])
            except telegram.PushError as exc:
                failed.append(str(exc)[:120])
                print(f"推送失败，保持未推送下轮重试：{exc}", file=sys.stderr)
                if isinstance(ref, Candidate):
                    st.add_pending(item_id=state_mod.event_id(ref.item.exchange, ref.item.url, ref.item.title),
                                   url=ref.item.url, title=ref.item.title, message=ref.message,
                                   exchange=ref.item.exchange, error=str(exc))
    else:
        for entry in pending_before:
            print("\n--- 干跑：不会发送（待重推）---\n" + entry["message"])
            st.mark_pushed({"exchange": entry.get("exchange", ""), "url": entry.get("url", ""),
                            "title": entry.get("title", ""), "published_at": entry.get("published_at", "")},
                           {"level": "low", "stage": 0}, "重试")
            st.clear_pending(state_mod.event_keys(entry.get("exchange", ""), entry.get("url", ""),
                                                  entry.get("title", "")))
            pushed.append("重试｜" + str(entry.get("title", ""))[:60])
        for candidate in selected:
            print("\n--- 干跑：不会发送 ---\n" + candidate.message)
            st.mark_pushed(candidate.item, candidate.decision, candidate.status)
            pushed.append(candidate.item.exchange + "｜" + candidate.item.title[:60])

    st.prune()
    st.save()

    report = {
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "gap_minutes": gap, "mode": "send" if send else "dry-run",
        "sources": [r.to_dict() for r in reports],
        "source_failures": st.source_failures,
        "all_sources_failed": all_failed,
        "source_health": sources.source_health(reports),
        "source_health_by_name": {r.name: r.health for r in reports},
        "cloud": {**cloud_report.to_dict(), "since": since_text},
        "candidates": [{"exchange": c.item.exchange, "title": c.item.title,
                        "url": c.item.url, "status": c.status, "decision": c.decision.to_dict()}
                       for c in candidates],
        "pushed": pushed, "push_failed": failed,
        "deduped_same_round": deduped_same_round,
        "deduped_same_round_detail": merged_same_round,
        "filtered_non_chinese": filtered_non_chinese,
        "filtered_non_operational": filtered_non_operational,
        "pending_retry": [{"exchange": e.get("exchange", ""), "title": e.get("title", ""),
                           "url": e.get("url", ""), "last_error": e.get("last_error", "")}
                          for e in st.pending_list()],
        "summary_rest": [{"exchange": c.item.exchange, "title": c.item.title,
                          "url": c.item.url} for c in rest],
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if failed:
        return EXIT_ALL_FAILED
    if all_failed or empty_round or (reports and any(not r.ok for r in reports)):
        return EXIT_PARTIAL
    return EXIT_OK
