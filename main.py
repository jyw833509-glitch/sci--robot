#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main.py —— 命令行入口

常用命令：
    python main.py init             初始化：生成 config.yaml + 建库
    python main.py check            检查配置是否完整
    python main.py search           只检索不入库不推送（看看今天有什么）
    python main.py run              执行一次完整流水线（检索→翻译→日报→推送）
    python main.py run --dry-run    只生成日报不推送
    python main.py show             弹窗预览今日待推送文献（不推送、不标记）
    python main.py schedule         常驻运行，按配置的时间每天自动执行
    python main.py test-mail        发一封测试邮件验证 SMTP
    python main.py stats            查看数据库统计
    python main.py backfill --start 2026-01-01 --end 2026-06-30
                                    历史回填（只入库不推送）
    python main.py publish         （发布者）检索+翻译+生成 feed.json 内容日历，
                                    托管后所有客户端 content.mode=feed 即内容一致
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_CONFIG_FILE, EXAMPLE_CONFIG_FILE, load_config  # noqa: E402
from logger import get_logger, setup_logging  # noqa: E402

BANNER = r"""
+------------------------------------------------------------+
|          抗体纯化文献 · 自动订阅推送机器人                  |
|       PubMed -> 去重 -> 中文翻译 -> 日报 -> 弹窗/邮件       |
+------------------------------------------------------------+
"""


def _init_logging(cfg) -> None:
    setup_logging(cfg.path("app.log_dir", "logs"), cfg.get("app.log_level", "INFO"))


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    print(BANNER)
    if DEFAULT_CONFIG_FILE.exists():
        print(f"[跳过] 配置文件已存在：{DEFAULT_CONFIG_FILE}")
    else:
        shutil.copyfile(EXAMPLE_CONFIG_FILE, DEFAULT_CONFIG_FILE)
        print(f"[完成] 已生成配置文件：{DEFAULT_CONFIG_FILE}")

    cfg = load_config(args.config)
    _init_logging(cfg)
    from database import get_database

    db = get_database(cfg)
    print(f"[完成] 数据库已就绪：{db.path}")
    for d in ("app.log_dir", "app.report_dir"):
        p = cfg.path(d)
        p.mkdir(parents=True, exist_ok=True)
        print(f"[完成] 目录已就绪：{p}")

    print("\n下一步：")
    print("  1. 默认已启用「桌面弹窗」推送，请确保机器人运行在你登录的电脑上（见 README 桌面弹窗章节）")
    print("  2. （可选）如需同时收邮件，把 config.yaml 的 channels 改为 [\"desktop\",\"email\"] 并填写 SMTP 账号/授权码")
    print("  3. （推荐）填写 translate.llm.api_key，翻译质量最好")
    print("  4. 执行  python main.py check      检查配置")
    print("  5. 执行  python main.py show       弹窗预览今日待推送文献")
    print("  6. 执行  python main.py schedule   常驻运行，每个工作日早 08:30 自动弹窗")
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from search import build_query
    from translate import Translator

    print(BANNER)
    print(f"配置文件      ：{cfg.source}")
    print(f"数据库        ：{cfg.path('database.path')}")
    print(f"日报目录      ：{cfg.path('app.report_dir')}")
    print(f"检索回溯      ：最近 {cfg.get('pubmed.lookback_days')} 天（datetype={cfg.get('pubmed.date_type')}）")
    print(f"相关度门槛    ：{cfg.get('relevance.min_score') if cfg.get('relevance.enabled') else '未启用'}")
    print(f"定时时间      ：{', '.join(str(t) for t in cfg.get('scheduler.run_at', []))}")
    print(f"推送渠道      ：{', '.join(cfg.get('notifier.channels', [])) or '（无）'}")
    print(f"收件人        ：{', '.join(cfg.get('notifier.email.to', [])) or '（未配置）'}")
    print(f"\n检索式：\n  {build_query(cfg)}")

    providers = Translator(cfg).available_providers()
    print(f"\n可用翻译后端  ：{' -> '.join(providers) if providers else '（无，将只推送英文摘要）'}")

    issues = cfg.validate()
    if issues:
        print("\n发现以下问题：")
        for i in issues:
            print(f"  [!] {i}")
        return 1
    print("\n配置检查通过。")
    return 0


def cmd_search(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from search import PubMedClient

    items = PubMedClient(cfg).search_recent(days=args.days, max_results=args.limit)
    print(f"\n共 {len(items)} 篇：\n")
    for i, a in enumerate(items, 1):
        print(f"{i:>2}. [score {a.score}] {a.title}")
        print(f"    {a.journal_abbr or a.journal} | {a.pub_date or a.entrez_date} | PMID {a.pmid} | DOI {a.doi or '—'}")
        print(f"    {a.pubmed_url}\n")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from scheduler import run_once

    # --limit N 临时覆盖每天推送篇数（0 = 不限制），不影响 config.yaml
    if args.limit is not None:
        cfg.set("pipeline.daily_limit", int(args.limit))

    summary = run_once(cfg, days=args.days, no_push=args.no_push, dry_run=args.dry_run)
    print("\n运行摘要：")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0 if "错误" not in summary else 1


def cmd_show(args) -> int:
    """弹窗预览今日待推送文献：检索 + 去重 + 翻译，但只弹窗、不推送、不标记已读。"""
    cfg = load_config(args.config)
    _init_logging(cfg)
    from scheduler import collect_articles
    from notifier import DesktopNotifier

    if args.limit is not None:
        cfg.set("pipeline.daily_limit", int(args.limit))

    db, pending, report, stats = collect_articles(cfg, days=args.days)
    print(f"\n今日待推送 {report.count} 篇，正在弹窗预览（不推送、不标记已读）...")
    print(f"  检索命中 {stats['检索命中']} | 新增入库 {stats['新增入库']} | 重复跳过 {stats['重复跳过']}")
    if report.count == 0:
        print("  没有可预览的文献，弹窗将提示「今日无新文献」。")
    DesktopNotifier(cfg).send(report)
    return 0


def cmd_schedule(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from scheduler import start_scheduler

    print(BANNER)
    start_scheduler(cfg)
    return 0


def cmd_test_mail(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from notifier import Notifier

    print("正在发送测试消息...")
    results = Notifier(cfg).send_test()
    if not results:
        print("没有启用任何推送渠道，请检查 notifier.channels")
        return 1
    for ch, ok in results.items():
        print(f"  {ch}: {'成功，请查收' if ok else '失败，详见 logs/bot.log'}")
    return 0 if all(results.values()) else 1


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from database import get_database

    print(BANNER)
    for k, v in get_database(cfg).stats().items():
        print(f"  {k}: {v}")
    return 0


def cmd_backfill(args) -> int:
    cfg = load_config(args.config)
    _init_logging(cfg)
    from scheduler import backfill

    result = backfill(cfg, args.start, args.end, no_push=not args.push)
    print("\n回填结果：")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


def cmd_publish(args) -> int:
    """发布者端：检索 + 翻译 + 生成内容日历 feed.json（供所有客户端同步）。"""
    cfg = load_config(args.config)
    _init_logging(cfg)
    import subprocess

    from database import get_database
    from feed import build_feed, save_feed
    from scheduler import _translate_pending
    from search import PubMedClient

    db = get_database(cfg)
    client = PubMedClient(cfg)
    found = client.search_recent(days=args.days)
    if not found:
        print("没有检索到符合条件的文献，无法生成内容日历。")
        return 1

    fresh = db.filter_new_articles(found)
    db.save_articles(fresh)
    # 发布者端补齐中文译文（客户端拿到 feed 后无需再翻译）
    _translate_pending(cfg, db, found)

    articles = found[: args.max_articles] if args.max_articles else found
    from datetime import datetime
    start = None
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d")
    feed = build_feed(cfg, articles, start_date=start)
    out = save_feed(feed, cfg.path("content.feed_output", "data/feed.json"))

    dates = list(feed["calendar"].keys())
    print(f"\n内容日历已生成：{out}")
    print(f"  纳入 {feed['total_articles']} 篇，首个推送日 {min(dates)}，末日 {max(dates)}")
    print(f"  每日推送 {cfg.get('pipeline.daily_limit', 1)} 篇")

    upload = cfg.get("content.feed_upload_cmd") or ""
    if upload:
        print(f"\n执行同步命令：{upload}")
        subprocess.run(upload, shell=True, check=False)
    else:
        print("\n未配置 content.feed_upload_cmd：")
        print("  请把上面生成的 feed.json 托管到一个所有人可访问的 URL")
        print("  （GitHub .raw / 腾讯云 COS / 内网静态文件…），")
        print("  然后在 config.yaml 设 content.mode=feed 且 content.feed_url=<该URL> 分发给同事。")
        print("  或在 config.yaml 配 content.feed_upload_cmd 实现 publish 后自动上传。")
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="抗体纯化文献自动订阅推送机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", default=None, help="指定配置文件路径（默认 config.yaml）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化配置与数据库").set_defaults(func=cmd_init)
    sub.add_parser("check", help="检查配置完整性").set_defaults(func=cmd_check)
    sub.add_parser("stats", help="查看数据库统计").set_defaults(func=cmd_stats)
    sub.add_parser("test-mail", help="发送测试消息验证推送配置").set_defaults(func=cmd_test_mail)
    sub.add_parser("schedule", help="常驻运行定时任务").set_defaults(func=cmd_schedule)

    p_search = sub.add_parser("search", help="只检索不入库不推送")
    p_search.add_argument("--days", type=int, default=None, help="回溯天数，默认取配置值")
    p_search.add_argument("--limit", type=int, default=20, help="最多显示条数")
    p_search.set_defaults(func=cmd_search)

    p_run = sub.add_parser("run", help="执行一次完整流水线")
    p_run.add_argument("--days", type=int, default=None, help="回溯天数，默认取配置值")
    p_run.add_argument("--limit", type=int, default=None, help="本次推送篇数上限（覆盖 daily_limit，0=不限）")
    p_run.add_argument("--dry-run", action="store_true", help="只生成日报，不推送")
    p_run.add_argument("--no-push", action="store_true", help="同 --dry-run")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="弹窗预览今日待推送文献（不推送、不标记）")
    p_show.add_argument("--days", type=int, default=None, help="回溯天数，默认取配置值")
    p_show.add_argument("--limit", type=int, default=None, help="预览篇数上限（覆盖 daily_limit，0=不限）")
    p_show.set_defaults(func=cmd_show)

    p_back = sub.add_parser("backfill", help="历史回填（默认只入库不推送）")
    p_back.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p_back.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p_back.add_argument("--push", action="store_true", help="回填后允许后续推送这些文献")
    p_back.set_defaults(func=cmd_backfill)

    p_pub = sub.add_parser("publish", help="（发布者）生成内容日历 feed.json 供全员同步")
    p_pub.add_argument("--days", type=int, default=180, help="检索回溯天数，默认 180")
    p_pub.add_argument("--max-articles", type=int, default=30, help="纳入日历的文献上限，默认 30")
    p_pub.add_argument("--start", default=None, help="日历起始日 YYYY-MM-DD；不填=今天。重复发布时用固定起始日可避免内容错位")
    p_pub.set_defaults(func=cmd_publish)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        print(BANNER)
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:
        log = get_logger("main")
        log.exception("执行失败：%s", exc)
        print(f"\n执行失败：{exc}\n详细堆栈见 logs/bot.log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
