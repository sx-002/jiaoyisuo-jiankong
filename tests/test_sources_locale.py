import unittest

from monitor import sources


class TestChineseLocale(unittest.TestCase):
    """币安公告接口默认返回英文标题，会被「只推中文」过滤全挡掉。

    必须带中文语言头，否则这个仓等于白跑（2026-11-24 实测：46 轮全部候选 0）。
    """

    def setUp(self):
        self.binance = [s for s in sources.SOURCES if s.name == "binance_api"][0]

    def test_binance_requests_chinese(self):
        headers = self.binance.headers or {}
        self.assertIn("zh", str(headers.get("Accept-Language", "")).lower())
        self.assertIn("zh", str(headers.get("lang", "")).lower())

    def test_binance_link_is_chinese_page(self):
        payload = {
            "data": {
                "catalogs": [
                    {"articles": [{"code": "b3eb06ad744b4398911a1b2c3d4e5f60", "title": "币安将上线 XXX", "releaseDate": 1700000000000}]}
                ]
            }
        }
        items = sources._binance_items(payload, self.binance)
        self.assertEqual(len(items), 1)
        self.assertIn("/zh-CN/", items[0].url)


if __name__ == "__main__":
    unittest.main()
