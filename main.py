"""交易所风险哨兵入口。默认只干跑，不接 Telegram。

python main.py --self-check                 离线自检
python main.py --fixtures tests/fixtures/sample.json --dry-run
python main.py --dry-run                    真实抓源（无 token 也安全）
python main.py --send                       显式接推送后才真正发 Telegram
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor import cloud, rules, run as monitor_run, sources  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="交易所风险哨兵")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选消息，不发送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送")
    parser.add_argument("--self-check", action="store_true", help="离线自检，不联网不写状态")
    parser.add_argument("--list-sources", action="store_true", help="列出 15 家官方源")
    parser.add_argument("--fixtures", default="", help="用本地 JSON 样例替代真实网络，做离线演示")
    parser.add_argument("--state", default="state/state.json", help="状态文件路径")
    parser.add_argument("--report", default="report.json", help="运行报告路径")
    parser.add_argument("--max-queries", type=int, default=12, help="本轮云搜索查询上限")
    parser.add_argument("--no-cloud", action="store_true", help="关闭云端搜索（只跑官方源）")
    parser.add_argument("--max-push", type=int, default=10, help="每轮最多推送条数")
    args = parser.parse_args()

    if args.self_check:
        failures = []
        if len(sources.OFFICIAL_EXCHANGES) != 12:
            failures.append(f"官方名单应为 12 家，实际 {len(sources.OFFICIAL_EXCHANGES)}")
        if rules.judge("Binance 暂停提现") is None:
            failures.append("规则未识别暂停提现")
        if rules.judge("OKX 上线新币") is not None:
            failures.append("普通上币误判为风险")
        if not cloud.detect_exchange("XT 限制提币"):
            failures.append("名单外交易所识别失败")

        # 帮助页/科普/内容站过滤：bianbtc 当高风险推过，必须挡掉。
        bianbtc_title = "公告里的「暂停提现」通常意味着什么？先分清三种情况 - Binance 币安中文网"
        bianbtc_url = "https://www.bianbtc.com/post/978.html"
        if not rules.is_help_or_content_page(bianbtc_title, bianbtc_url):
            failures.append("bianbtc 科普页没被帮助页/内容站规则挡掉")
        if rules.is_operational_event(bianbtc_title):
            failures.append("bianbtc 科普页被当成运营事件")
        if rules.judge(bianbtc_title) is not None:
            failures.append("bianbtc 科普页仍被判为风险")
        # 真运营事件与官方公告路径不能误挡。
        for title in ("Bitget 暂停 USDT 提币",
                      "HTX 部分用户须在 11-23 前申请提现授权，未获批可能无法提币"):
            if not rules.is_operational_event(title):
                failures.append(f"真实运营事件被误挡：{title}")
            if rules.judge(title) is None:
                failures.append(f"真实运营事件没判定：{title}")
        if rules.is_help_or_content_page(
                "Bitget 暂停 USDC-Arbitrum One 网络提现，恢复时间另告",
                "https://www.bitget.com/support/articles/12508313443483"):
            failures.append("官方公告路径被内容站规则误挡")
        if failures:
            for item in failures:
                print("自检失败：" + item, file=sys.stderr)
            return 5
        print(f"自检通过：官方名单 {len(sources.OFFICIAL_EXCHANGES)} 家，"
              "规则与名单外识别正常；bianbtc 科普页挡掉，Bitget/HTX 真事件仍命中")
        return 0

    if args.list_sources:
        for src in sources.SOURCES:
            print(f"{src.exchange}\t{src.layer}\t{src.name}\t{src.url}")
        return 0

    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5
    return monitor_run.run(
        fixtures=args.fixtures, state_path=args.state, report_path=args.report,
        max_queries=args.max_queries, no_cloud=args.no_cloud, send=args.send,
        max_push=args.max_push,
    )


if __name__ == "__main__":
    sys.exit(main())
