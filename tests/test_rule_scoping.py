import unittest

from monitor import rules

RULES = {name: pattern for name, pattern, _, _, _ in rules.RULES}


class TestRuleScoping(unittest.TestCase):
    """规则里的 `.*` 会因为 title+summary+body 拼成一行而横跨整篇，造成误报。

    实测事故：MEXC「消息与通知服务维护公告」被判成「客服失联·阶段4」并推了出去，
    因为正文里有 "Support Babylon ... unavailable"，被 `support.*unavailable` 命中。
    """

    def test_support_pattern_does_not_cross_sentence(self):
        text = ("Support Babylon (BABY) Network Upgrade Maintenance "
                "Please note that the service may be unavailable during the upgrade window.")
        self.assertIsNone(RULES["R06_support_gone"].search(text))

    def test_support_exact_phrase_still_matches(self):
        self.assertIsNotNone(RULES["R06_support_gone"].search("客服失联"))
        self.assertIsNotNone(RULES["R06_support_gone"].search("support not responding"))

    def test_other_loose_patterns_are_bounded(self):
        # 这些 `.*` 原本也能跨整篇；现在必须限定在小窗口内
        self.assertIsNone(RULES["R10_reserve_redeem"].search("储备金充足。" + "x" * 200 + "加息"))
        self.assertIsNotNone(RULES["R10_reserve_redeem"].search("理财加息"))


if __name__ == "__main__":
    unittest.main()
