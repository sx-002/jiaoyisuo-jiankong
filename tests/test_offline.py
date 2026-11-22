"""离线自检：不联网、不需要任何 token，也不触碰真实 state。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unittest.mock import patch

from monitor import cloud, http, rules, sources, state as state_mod  # noqa: E402
from monitor import telegram  # noqa: E402
from monitor import run as monitor_run  # noqa: E402


class TestRules(unittest.TestCase):
    def test_withdrawal_suspension_is_high(self):
        decision = rules.judge("Binance 暂停提现", "全站用户提现暂停")
        self.assertEqual(decision.level, rules.HIGH)
        self.assertEqual(decision.stage, 3)
        self.assertTrue(decision.rule_name.startswith("R05"))

    def test_first_rule_wins_and_records_name(self):
        decision = rules.judge("交易所跑路，提现也暂停")
        self.assertEqual(decision.rule_name, "R01_exit_scam")
        self.assertEqual(decision.stage, 5)

    def test_listing_noise_ignored(self):
        self.assertIsNone(rules.judge("OKX 上线新币并开启交易活动"))

    def test_recovery_marked_resolved(self):
        decision = rules.judge("Bitget 提现已恢复")
        self.assertTrue(decision.resolved)

    def test_no_time_is_risk_upgrade(self):
        decision = rules.judge("某交易所暂停提现，稍后恢复")
        self.assertEqual(decision.level, rules.HIGH)
        self.assertIn("恢复时间", decision.reason)

    def test_message_has_required_fields(self):
        decision = rules.judge("Bitget 暂停 USDT 提币")
        text = rules.format_message(exchange="Bitget", title="Bitget 暂停 USDT 提币",
                                    published_cst="2026-11-16 09:44", url="https://example.com/a",
                                    decision=decision, status="新增", official=True)
        for field in ("Bitget", "标题：", "时间：", "原文："):
            self.assertIn(field, text)

    def test_message_hides_internal_details(self):
        """规则名、阶段编号、信号分类、命中文本是排查用的，不能推给用户。"""
        decision = rules.judge("Bitget 暂停 USDT 提币")
        text = rules.format_message(exchange="Bitget", title="Bitget 暂停 USDT 提币",
                                    published_cst="2026-11-16 09:44", url="https://example.com/a",
                                    decision=decision, status="新增", official=True)
        for hidden in ("为什么可疑", "命中规则", "R05_withdraw_suspended", "信号类型",
                       "阶段3", "阶段 3", "全面暂停", "单币"):
            self.assertNotIn(hidden, text)
        self.assertIn("高风险", text)
        # 内部字段仍然保留在决策对象里，供报告与排查。
        self.assertIn("R05_withdraw_suspended", decision.reason)
        self.assertEqual(decision.stage, 3)

    def test_pure_english_is_filtered(self):
        self.assertTrue(rules.is_non_chinese(
            "Binance suspends USDT withdrawals indefinitely",
            "Withdrawals suspended on all networks until further notice"))
        self.assertEqual(rules.chinese_ratio("Binance suspends withdrawals"), 0.0)

    def test_mixed_chinese_english_is_kept(self):
        self.assertFalse(rules.is_non_chinese(
            "Binance 暂停 USDT 提现", "Withdrawals suspended on all networks"))
        self.assertFalse(rules.is_non_chinese(
            "Binance USDT withdrawals suspended", "中文摘要：提现暂停，稍后恢复"))
        self.assertGreaterEqual(rules.chinese_ratio("Binance 暂停 USDT 提现"), 0.05)


    def test_recovery_time_makes_withdrawal_medium(self):
        decision = rules.judge("Bitget USDC-AVAX 网络因钱包维护暂停提现，预计 20:20 恢复",
                               "钱包维护期间暂停该网络提现")
        self.assertEqual(decision.level, rules.MEDIUM)
        self.assertIn("20:20", decision.recovery)
        text = rules.format_message(exchange="Bitget", title="Bitget USDC-AVAX 网络因钱包维护暂停提现",
                                    published_cst="2026-11-18 10:00", url="https://example.com/a",
                                    decision=decision, status="新增", official=True)
        # 恢复时间只参与内部判级，不推给用户。
        self.assertNotIn("恢复情况", text)
        self.assertNotIn("20:20", text)

    def test_without_recovery_time_withdrawal_is_high(self):
        decision = rules.judge("Bitget USDT-Arbitrum 暂停提现，恢复时间另告",
                               "该网络暂停提现")
        self.assertEqual(decision.level, rules.HIGH)
        self.assertEqual(decision.recovery, "恢复时间另告")
        text = rules.format_message(exchange="Bitget", title="Bitget USDT-Arbitrum 暂停提现",
                                    published_cst="2026-11-18 10:00", url="https://example.com/b",
                                    decision=decision, status="新增", official=True)
        self.assertNotIn("恢复情况", text)

    def test_limit_number_not_shown_for_maintenance(self):
        decision = rules.judge("Bitget 暂停提现，单笔限额 1000 USDT", "限额调整为 1000 USDT")
        self.assertIn("1000 USDT", decision.limit)       # 内部字段仍在
        self.assertFalse(decision.risk_limit)
        text = rules.format_message(exchange="Bitget", title="Bitget 暂停提现",
                                    published_cst="2026-11-18 10:00", url="https://example.com/c",
                                    decision=decision, status="新增", official=True)
        self.assertNotIn("限额/门槛", text)
        self.assertNotIn("1000 USDT", text)

    def test_risk_limit_shown_when_funds_stuck(self):
        decision = rules.judge("Bitget 仅允许小额提现，单日上限 100 USDT",
                               "提现限额调整为单日 100 USDT")
        self.assertTrue(decision.risk_limit)
        self.assertTrue(decision.pushable)
        text = rules.format_message(exchange="Bitget", title="Bitget 仅允许小额提现，单日上限 100 USDT",
                                    published_cst="2026-11-18 10:00", url="https://example.com/c2",
                                    decision=decision, status="新增", official=True)
        self.assertIn("限额/门槛", text)
        self.assertIn("100 USDT", text)

    def test_anti_rug_push_threshold(self):
        push = rules.judge("某交易所跑路")
        self.assertTrue(push.pushable)
        # 恢复的、小币种维护、有明确恢复时间的，只存档。
        for title, summary in (
            ("Bitget 提现已恢复", ""),
            ("Bitget 某个小币种暂停提现", "该币种暂停提现"),
            ("Bitget USDC-AVAX 网络暂停提现，预计 20:20 恢复", "钱包维护期间暂停提现"),
            ("OKX 维护延期公告", "原定维护延期"),
        ):
            d = rules.judge(title, summary)
            self.assertIsNotNone(d)
            self.assertFalse(d.pushable, title)
        # 链级/网络级暂停且没给具体时间，和全站一样要推。
        d = rules.judge("Bitget USDT-Arbitrum 网络暂停提现，恢复时间另告")
        self.assertTrue(d.pushable)

    def test_recovered_notice_carries_time(self):
        decision = rules.judge("Bitget USDC-BSC 网络因钱包维护暂停提现，19:17 已恢复")
        self.assertTrue(decision.resolved)
        self.assertIn("19:17 已恢复", decision.recovery)

    def test_delisting_notice_is_not_withdrawal_risk(self):
        decision = rules.judge(
            "[现货下架]MGBX将下架 W、CLAWNCH 现货交易对的公告",
            "暂停充值时间：2026 年 11 月 19 日 18:00（SGT）；暂停提现时间：2026 年 11 月 19 日 18:00（SGT）")
        self.assertIsNone(decision, "下架公告是例行业务，不能按提现中断判危")

    def test_generic_title_delisting_body_not_withdrawal(self):
        decision = rules.judge(
            "交易所的公告",
            "",
            "MGBX 将下架 W、CLAWNCH 现货交易对。暂停充值时间：2026 年 11 月 19 日 18:00（SGT）"
            "交易对下线时间：2026 年 11 月 19 日 18:00（SGT）暂停提现时间：2026 年 11 月 19 日 18:00（SGT）"
            "交易对名称：W/USDT、CLAWNCH/USDT")
        self.assertIsNone(decision)

    def test_delisting_word_in_body_does_not_hide_real_suspension(self):
        decision = rules.judge("Bitget 暂停全站提现", "维护期间暂停提现，部分币种将下架")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule_name, "R05_withdraw_suspended")

    def test_non_withdrawal_event_format_unchanged(self):
        decision = rules.judge("某交易所被黑，资产被盗", "安全事件")
        text = rules.format_message(exchange="某交易所", title="某交易所被黑",
                                    published_cst="2026-11-18 10:00", url="https://example.com/d",
                                    decision=decision, status="新增", official=True)
        self.assertNotIn("恢复情况", text)
        self.assertNotIn("限额/门槛", text)


    def test_same_event_two_sources_one_push(self):
        from monitor.run import Candidate
        item_a = sources.Item("MGBX", "交易所的公告",
                              "https://megabittechltd.zendesk.com/api/v2/help_center/zh-cn/articles/18413286941583.json",
                              published_at="2026-11-17T03:24:22Z")
        item_b = sources.Item("MGBX", "[现货下架]MGBX将下架 W、CLAWNCH 现货交易对的公告",
                              "https://support.mgbx.com/hc/zh-cn/articles/18413286941583",
                              published_at="2026-11-17T03:24:22Z")
        decision = rules.judge("Bitget 暂停提现，恢复时间另告")
        a = Candidate(item_a, decision, "新增", "A")
        b = Candidate(item_b, decision, "新增", "B")
        kept, merged = state_mod.merge_duplicates([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(merged), 1)
        # 标题更具体的那条胜出。
        self.assertIn("现货下架", kept[0].item.title)

    def test_dry_run_merges_same_event_and_skips_delisting(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            code = monitor_run.run(
                fixtures=str(ROOT / "tests/fixtures/sample.json"),
                state_path=os.path.join(tmp, "state.json"),
                report_path=os.path.join(tmp, "report.json"),
                send=False, no_cloud=True,
            )
            self.assertEqual(code, 0)
            payload = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(payload["deduped_same_round"], 1)
            self.assertFalse(any("fixture-008" in c["url"] for c in payload["candidates"] if "交易所的公告" == c["title"]))
            self.assertFalse(any("18413286941583" in c["url"] for c in payload["candidates"]))
            self.assertFalse(any("交易所的公告" == c["title"] for c in payload["candidates"]))


class TestOfficialSourceScope(unittest.TestCase):
    """源审查：公告路径才收，新闻/博客栏目不收；内容只留运营事件。"""

    def test_news_path_is_not_announcement(self):
        self.assertFalse(sources.is_announcement_link("https://www.weex.com/zh-CN/news/detail/123"))
        self.assertFalse(sources.is_announcement_link("https://x.com/blog/post/1"))
        self.assertFalse(sources.is_announcement_link("https://x.com/academy/lesson/2"))
        for ok in (
            "https://www.bitget.com/support/articles/12508313443483",
            "https://www.weex.com/zh-CN/product-launch/detail/1",
            "https://www.deepcoin.com/ps/zh/announcements",
            "https://www.binance.com/zh-CN/support/announcement/fixture",
        ):
            self.assertTrue(sources.is_announcement_link(ok), ok)

    def test_industry_news_filtered_operational_kept(self):
        news = "欧洲多国遇黑客袭击后，英国找到商用冰箱及冰柜系统漏洞修复方案"
        self.assertFalse(rules.is_operational_event(news))
        self.assertIsNone(rules.judge(news), "行业新闻不能按交易所风险推")
        for title in (
            "Bitget 暂停 USDC-Arbitrum One 网络提现，恢复时间另告",
            "Bitget INJ（Injective 网络）提现已恢复",
            "HTX 部分用户须在 11-23 前申请提现授权，未获批可能无法提币",
        ):
            self.assertTrue(rules.is_operational_event(title), title)

    def test_help_page_and_content_site_filtered(self):
        bianbtc_title = "公告里的「暂停提现」通常意味着什么？先分清三种情况 - Binance 币安中文网"
        bianbtc_url = "https://www.bianbtc.com/post/978.html"
        self.assertTrue(rules.is_help_or_content_page(bianbtc_title, bianbtc_url))
        self.assertFalse(rules.is_operational_event(bianbtc_title))
        self.assertIsNone(rules.judge(bianbtc_title))
        # 标题没有科普词，但 URL 是内容站/博客，也要挡。
        self.assertTrue(rules.is_help_or_content_page(
            "Binance 暂停提现", "https://www.bianbtc.com/post/979.html"))
        self.assertTrue(rules.is_help_or_content_page(
            "某交易所提现异常", "https://someblog.com/news/123"))
        # 官方公告路径与用户反馈不能误挡。
        self.assertFalse(rules.is_help_or_content_page(
            "Bitget 暂停 USDC-Arbitrum One 网络提现，恢复时间另告",
            "https://www.bitget.com/support/articles/12508313443483"))
        self.assertFalse(rules.is_help_or_content_page(
            "XT 限制提币，用户提不出来", "https://x.com/example/status/1234567890"))
        for title in ("Bitget 暂停 USDT 提币",
                      "HTX 部分用户须在 11-23 前申请提现授权，未获批可能无法提币"):
            self.assertFalse(rules.is_help_or_content_page(title, ""))
            self.assertTrue(rules.is_operational_event(title), title)
            self.assertIsNotNone(rules.judge(title), title)

    def test_weex_fixture_filtered_and_counted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            monitor_run.run(fixtures=str(ROOT / "tests/fixtures/sample.json"),
                            state_path=os.path.join(tmp, "state.json"),
                            report_path=os.path.join(tmp, "report.json"),
                            send=False, no_cloud=True)
            payload = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(payload["filtered_non_operational"], 2)
            self.assertFalse(any("weex.com/zh-CN/news" in c["url"] for c in payload["candidates"]))
            self.assertFalse(any("bianbtc.com" in c["url"] for c in payload["candidates"]))
            self.assertFalse(any("意味着什么" in c["title"] for c in payload["candidates"]))


class TestInfrastructure(unittest.TestCase):
    """可复制到其他仓的基础设施自检：URL 归一、中文二元组、源 empty 态。"""

    def test_url_normalization_for_id(self):
        from monitor import textsim
        a = "https://WWW.Example.com/Announcement/a/?utm_source=x&ref=tw&id=7#top"
        b = "http://www.example.com/Announcement/a/?id=7&spm=abc"
        self.assertEqual(textsim.normalize_url_for_id(a), textsim.normalize_url_for_id(b))
        self.assertEqual(state_mod.event_id("X", a, "标题 A"), state_mod.event_id("X", b, "标题 B"))

    def test_chinese_bigram_similarity(self):
        from monitor import textsim
        a = "Bitget USDC-AVAX 网络因钱包维护暂停提现"
        b = "Bitget 暂停 USDC-AVAX 网络提现，钱包维护"
        self.assertGreaterEqual(textsim.similarity(a, b), 0.6)
        self.assertTrue(state_mod.state_similar(a, b))
        self.assertFalse(state_mod.state_similar("Bitget 暂停全站提现", "Bitget 恢复全部提现"))

    def test_empty_source_is_not_failure(self):
        empty = sources.SourceReport("x", "X", "api", True, sources.SOURCE_EMPTY, "", [])
        self.assertEqual(empty.health, sources.SOURCE_EMPTY)
        self.assertEqual(sources.source_health([empty]), sources.SOURCE_OK)
        ok = sources.SourceReport("y", "Y", "api", True, "ok", "", [object()])
        bad = sources.SourceReport("z", "Z", "api", False, "blocked", "403")
        self.assertEqual(sources.source_health([ok, bad]), sources.SOURCE_PARTIAL)
        self.assertEqual(sources.source_health([bad]), sources.SOURCE_FAILURE)
        self.assertIn("health", empty.to_dict())


class TestSources(unittest.TestCase):
    def test_official_list_has_15(self):
        self.assertEqual(len(sources.OFFICIAL_EXCHANGES), 15)
        self.assertIn("MEXC", sources.OFFICIAL_EXCHANGES)
        self.assertNotIn("KuCoin", sources.OFFICIAL_EXCHANGES)

    def test_gate_blocked_not_fake_success(self):
        source = next(s for s in sources.SOURCES if s.exchange == "Gate")
        report = sources.fetch_source(source)
        self.assertFalse(report.ok)
        self.assertEqual(report.status, "blocked")

    def test_ssrf_rejects_private_host(self):
        with self.assertRaises(http.FetchFailure):
            http.validate_url("http://127.0.0.1:8080/admin")

    def test_binance_code_to_link(self):
        sample = {"data": {"catalogs": [{"articles": [
            {"title": "Wallet Maintenance for ETH", "code": "a" * 32, "releaseDate": 1793066417463}
        ]}]}}
        source = next(s for s in sources.SOURCES if s.name == "binance_api")
        result = http.FetchResult(source.url, json.dumps(sample).encode(), 200)
        items = sources.parse_payload(source, result)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].url.endswith("a" * 32))

    def test_timestamp_naive_iso(self):
        self.assertTrue(sources._timestamp("2026-11-17 10:00:00").startswith("2026-11-17T10:00"))

    def test_mexc_article_api_parse(self):
        sample = {"data": {"results": [{
            "id": 17827791539788,
            "title": "Suspension of DYDX Deposits and Withdrawals",
            "routerUrl": "/announcements/17827791539788",
            "createdAt": "2026-11-17T10:28:08Z",
            "updateTime": "2026-11-17T10:29:56Z",
        }]}}
        source = next(s for s in sources.SOURCES if s.name == "mexc_api")
        result = http.FetchResult(source.url, json.dumps(sample).encode(), 200)
        items = sources.parse_payload(source, result)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://www.mexc.com/announcements/17827791539788")
        self.assertTrue(items[0].published_at.startswith("2026-11-17T18:28"))

    def test_zendesk_fixture_parse(self):
        sample = {"articles": [{"title": "暂停提现公告", "html_url": "https://example.com/a",
                                "created_at": "2026-11-17T01:00:00Z"}]}
        source = next(s for s in sources.SOURCES if s.name == "easicoin_zendesk")
        result = http.FetchResult(source.url, json.dumps(sample).encode(), 200)
        items = sources.parse_payload(source, result)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].exchange, "EasiCoin")


class TestState(unittest.TestCase):
    def test_dedup_and_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.State(os.path.join(tmp, "state.json"))
            item = sources.Item("Binance", "暂停提现", "https://example.com/a")
            low = rules.judge("维护中")
            st.mark_pushed(item, low, "新增")
            high = rules.judge("暂停提现")
            self.assertEqual(st.status_for(item, low), "跳过")
            self.assertEqual(st.status_for(item, high), "升级")

    def test_corrupt_state_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                state_mod.State(str(path)).load()

    def test_cloud_exchange_detection(self):
        self.assertEqual(cloud.detect_exchange("XT 限制提币"), "XT")
        self.assertEqual(cloud.detect_exchange("Binance withdrawal suspended"), "Binance")
        self.assertEqual(cloud.detect_exchange("extract non-account problem"), "")

    def test_anysearch_endpoint_is_com(self):
        self.assertEqual(cloud.ANYSEARCH_DEFAULT_BASE_URL, "https://api.anysearch.com")

    def test_anysearch_json_parsed_with_date(self):
        payload = json.dumps({"code": 0, "data": {"results": [{
            "title": "Binance 暂停提现",
            "url": "https://x.com/user/status/1",
            "snippet": "用户反馈提现不到账 date: Nov 20, 2026",
        }]}})
        hits = cloud._hits_from_text(payload, "暂停提现", "anysearch")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, "https://x.com/user/status/1")
        self.assertTrue(hits[0].published_at.startswith("2026-11-20"))
        items = cloud.hits_to_items(hits, window_start=datetime(2026, 11, 19, tzinfo=timezone.utc),
                                    window_end=datetime(2026, 11, 21, tzinfo=timezone.utc))
        self.assertEqual(len(items), 1)

    def test_exa_block_parsing(self):
        text = ("Title: Binance 暂停提现\nURL: https://example.com/a\nPublished: 2026-11-17\n"
                "Highlights:\n用户反馈提现不到账")
        hits = cloud._hits_from_text(text, "暂停提现", "exa_public")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "Binance 暂停提现")
        self.assertEqual(hits[0].published_at, "2026-11-17")

    def test_search_window_recent_and_clamped(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        recent, text = cloud.search_window("2026-11-17T10:30:00+00:00", now=now)
        self.assertEqual(recent, now - timedelta(hours=2, minutes=0))
        self.assertIn("2026-11-17 10:00 UTC", text)
        missing, _ = cloud.search_window("", now=now)
        self.assertEqual(missing, now - timedelta(days=3, minutes=30))
        stale, _ = cloud.search_window("2026-11-01T00:00:00+00:00", now=now)
        self.assertEqual(stale, missing)

    def test_objective_uses_since(self):
        objective = cloud._build_objective("2026-11-17 09:30 UTC")
        self.assertIn("2026-11-17 09:30 UTC 之后", objective)
        self.assertIn("帮助页", objective)

    def test_no_time_cloud_hit_dropped(self):
        hit = cloud.SearchHit("q", "exa_public", "https://x.com/a/1", "XT 跑路", "用户反馈", "")
        self.assertEqual(cloud.hits_to_items([hit], window_start=datetime(2020, 1, 1, tzinfo=timezone.utc)), [])

    def test_stale_and_duplicate_old_hits_dropped(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        start = now - timedelta(hours=2)
        stale = cloud.SearchHit("q", "exa_public", "https://x.com/a/2", "XT 跑路",
                                "用户反馈提现不到账", "2026-01-01")
        self.assertEqual(cloud.hits_to_items([stale], window_start=start, window_end=now), [])
        fresh = cloud.SearchHit("q", "exa_public", "https://x.com/a/3", "XT 跑路",
                                "用户反馈提现不到账", "2026-11-17T11:30:00Z")
        self.assertEqual(len(cloud.hits_to_items([fresh], window_start=start, window_end=now)), 1)
        # 同一批旧内容重复喂入，不应再进候选。
        old_batch = [cloud.SearchHit("q", "exa_public", f"https://x.com/old/{i}",
                                     "XT 跑路", "旧科普文", "2026-01-01")
                     for i in range(3)]
        self.assertEqual(cloud.hits_to_items(old_batch + old_batch, window_start=start, window_end=now), [])

    def test_negated_exit_scam_not_high(self):
        decision = rules.judge("BitMart 创始人称没跑路，但用户提币卡单",
                               "withdrawals stalled for days")
        self.assertNotEqual(decision.rule_name, "R01_exit_scam")


class TestDryRun(unittest.TestCase):
    def test_fixture_dry_run_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            code = monitor_run.run(
                fixtures=str(ROOT / "tests/fixtures/sample.json"),
                state_path=os.path.join(tmp, "state.json"),
                report_path=os.path.join(tmp, "report.json"),
                send=False, no_cloud=True,
            )
            self.assertEqual(code, 0)
            payload = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(any(c["exchange"] == "Binance" for c in payload["candidates"]))
            # 防跑路口径：XT 的「提现不到账/限制提币」属于冷门线索，只存档不推。
            self.assertFalse(any(c["exchange"] == "XT" for c in payload["candidates"]))
            self.assertFalse(any(c["exchange"] == "Bybit" for c in payload["candidates"]))
            self.assertEqual(len(payload["pushed"]), len(payload["candidates"]))
            # 纯英文条目不进候选，并在报告里单独计数。
            self.assertEqual(payload["filtered_non_chinese"], 1)
            self.assertFalse(any("fixture-004" in c["url"] for c in payload["candidates"]))

    def test_failed_push_is_retried_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TELEGRAM_BOT_TOKEN"] = "t"
            os.environ["TELEGRAM_CHAT_ID"] = "c"
            state_path = os.path.join(tmp, "state.json")
            with patch("monitor.run.telegram.send_message",
                       side_effect=telegram.PushError("boom")):
                code = monitor_run.run(fixtures=str(ROOT / "tests/fixtures/sample.json"),
                                       state_path=state_path,
                                       report_path=os.path.join(tmp, "r1.json"),
                                       send=True, no_cloud=True)
            self.assertEqual(code, 2)  # 推送失败如实报错，不吞错
            saved = state_mod.State(state_path).load()
            self.assertTrue(saved.pending, "失败的条目必须进待重推队列")
            self.assertFalse(any(meta.get("url") == "https://www.binance.com/zh-CN/support/announcement/fixture-001"
                                 for meta in saved.seen.values()),
                             "推送失败的条目不能标记为已推")

            with patch("monitor.run.telegram.send_message") as send:
                code2 = monitor_run.run(fixtures=str(ROOT / "tests/fixtures/sample.json"),
                                        state_path=state_path,
                                        report_path=os.path.join(tmp, "r2.json"),
                                        send=True, no_cloud=True)
            self.assertEqual(code2, 0)
            self.assertTrue(send.called, "下一轮必须重推上一轮失败的条目")
            saved2 = state_mod.State(state_path).load()
            self.assertFalse(saved2.pending, "推送成功后待重推队列要清空")


if __name__ == "__main__":
    unittest.main()
