import unittest

from monitor import sources

HTML = """
<div class="tgme_widget_message js-widget_message" data-post="MEXC_Announcement/12345">
  <div class="tgme_widget_message_user_name"><a href="https://t.me/MEXC_Announcement">MEXC</a></div>
  <div class="tgme_widget_message_text js-message_text">MEXC will delist BTC/USDT on November 30.<br>Please withdraw your funds in advance.</div>
  <a class="tgme_widget_message_date" href="https://t.me/MEXC_Announcement/12345"><time datetime="2026-11-23T10:00:00+08:00">10:00</time></a>
</div>
<div class="tgme_widget_message js-widget_message" data-post="MEXC_Announcement/12346">
  <div class="tgme_widget_message_text js-message_text">Short</div>
</div>
"""


class TestTelegramPreview(unittest.TestCase):
    def setUp(self):
        # 这个源已从监控名单移除（纯英文 Telegram 频道），但解析器保留，
        # 以后有中文频道可以直接复用，所以这里手工构造一个源来测解析。
        self.src = sources.OfficialSource(
            "tg_preview", "MEXC", "html", "https://t.me/s/example", "telegram_preview", "测试用")

    def test_parses_post_with_text_and_time(self):
        items = sources._telegram_preview_items(HTML, self.src)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("delist", item.title)
        self.assertEqual(item.url, "https://t.me/MEXC_Announcement/12345")
        self.assertTrue(item.published_at.startswith("2026-11-23"))
        self.assertIn("withdraw", item.summary)

    def test_too_short_body_skipped(self):
        items = sources._telegram_preview_items(HTML, self.src)
        self.assertNotIn("MEXC_Announcement/12346", [i.url for i in items])

    def test_empty_page_raises(self):
        with self.assertRaises(Exception):
            sources._telegram_preview_items("<html><body>no posts</body></html>", self.src)


if __name__ == "__main__":
    unittest.main()
