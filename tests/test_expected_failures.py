import unittest

from monitor import sources


class TestExpectedFailures(unittest.TestCase):
    """已知抓不到的源要标 expected，否则「部分源失败」的退出码就失去区分度。

    背景（2026-11-27）：Gate 固定 blocked、Deepcoin 必须前端渲染、BitTap 登录墙，
    导致交易所仓近 5 天 155/155 全是 exit=1，真有源挂掉时退出码也不会变。
    """

    def test_known_unreadable_sources_marked_expected(self):
        by_name = {s.name: s for s in sources.SOURCES}
        self.assertTrue(by_name["deepcoin_html"].expected)
        self.assertTrue(by_name["bittap_zendesk"].expected)

    def test_blocked_source_reports_expected(self):
        src = {s.name: s for s in sources.SOURCES}["gate_blocked"]
        report = sources.fetch_source(src)
        self.assertFalse(report.ok)
        self.assertTrue(report.expected)

    def test_normal_source_not_expected(self):
        by_name = {s.name: s for s in sources.SOURCES}
        for name in ("binance_api", "okx_api", "mexc_api"):
            self.assertFalse(by_name[name].expected, name)


if __name__ == "__main__":
    unittest.main()
