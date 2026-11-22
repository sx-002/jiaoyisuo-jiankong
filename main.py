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
        if len(sources.OFFICIAL_EXCHANGES) != 15:
            failures.append(f"官方名单应为 15 家，实际 {len(sources.OFFICIAL_EXCHANGES)}")
        if rules.judge("Binance 暂停提现") is None:
            failures.append("规则未识别暂停提现")
        if rules.judge("OKX 上线新币") is not None:
            failures.append("普通上币误判为风险")
        if not cloud.detect_exchange("XT 限制提币"):
            failures.append("名单外交易所识别失败")
        if failures:
            for item in failures:
                print("自检失败：" + item, file=sys.stderr)
            return 5
        print(f"自检通过：官方名单 {len(sources.OFFICIAL_EXCHANGES)} 家，规则与名单外识别正常")
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
