"""
scheduler.py —— 业务流水线 + 定时任务

流水线（run_once）：
    1. PubMed 检索最近 N 天文献
    2. 与数据库比对，过滤掉已推送过的 PMID（去重）
    3. 新文献入库
    4. 取出「待推送」文献并翻译摘要（只为真正要推送的内容付翻译成本）
    5. 生成日报（HTML / Markdown）
    6. 邮件 / Webhook 推送
    7. 推送成功后标记 pushed，失败则保留待下次重试

定时任务（start_scheduler）：
    读取 scheduler.run_at 里的时间点，每天定时触发 run_once。

对外接口：
    run_once(cfg, days=None, no_push=False, dry_run=False) -> dict
    backfill(cfg, start, end, no_push=True)                -> dict
    start_scheduler(cfg)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_database
from feed import articles_for_date, load_feed
from logger import get_logger
from notifier import Notifier
from report import build_report, save_report
from search import PubMedClient
from translate import Translator

log = get_logger("scheduler")

SEP = "=" * 62


def _is_workday(check_date: "datetime", cfg) -> bool:
    """判断某天是否为工作日（需要推送的日子）。

    判定顺序：
        1. 显式节假日列表 -> 非工作日
        2. 补班日列表     -> 工作日（即使是周末）
        3. chinese_calendar（若启用且已安装）-> 以官方节假日/调休为准
        4. 周末判断（skip_weekends=true 时周六日算休息日）
    """
    wd = cfg.get("scheduler.workday", {}) or {}
    ds = check_date.strftime("%Y-%m-%d")

    holidays = set(wd.get("holidays") or [])
    if ds in holidays:
        log.info("%s 在节假日列表中，视为休息日", ds)
        return False

    makeup = set(wd.get("makeup_workdays") or [])
    if ds in makeup:
        log.info("%s 在补班日列表中，视为工作日", ds)
        return True

    if wd.get("use_chinese_calendar"):
        try:
            import chinese_calendar as clc  # type: ignore
            return clc.is_workday(check_date)
        except Exception as exc:  # pragma: no cover
            log.warning("use_chinese_calendar=true 但 chinese_calendar 不可用（%s），回退到周末判断", exc)

    if wd.get("skip_weekends", True):
        return check_date.weekday() < 5  # 0=周一 ... 4=周五
    return True


def _translate_pending(cfg, db, articles) -> None:
    """为待推送文献补齐中文翻译，并回写数据库。"""
    if not cfg.get("translate.enabled", True):
        return
    todo = [a for a in articles if a.abstract and not a.abstract_zh]
    if not todo:
        log.info("待推送文献均已有译文，跳过翻译")
        return

    translator = Translator(cfg, db=db)
    if not translator.available_providers():
        log.warning("没有可用翻译后端，日报将只含英文摘要")
        return

    translator.translate_articles(todo)
    for art in todo:
        if art.abstract_zh or art.title_zh:
            db.update_translation(art.pmid, art.title_zh, art.abstract_zh, art.translate_provider)


def _collect_from_feed(cfg) -> tuple:
    """全局同步模式：内容来自中央 feed，不再各自检索 PubMed。

    返回 (db, pending, report, stats)。同一份 feed 下，所有客户端在任意
    给定日期取到的文献都完全一致。
    """
    db = get_database(cfg)
    feed_url = cfg.get("content.feed_url") or ""
    if not feed_url:
        raise RuntimeError("content.mode=feed 但 content.feed_url 未配置")
    feed = load_feed(
        feed_url, cfg,
        cache_path=cfg.path("content.feed_cache", "data/feed_cache.json"),
    )

    today = datetime.now().strftime("%Y-%m-%d")
    # 全局同步（广播）：所有客户端按「今天」这个日历日期取同一篇，
    # 因此在任意给定真实日期，每个客户端推送的文献都完全一致。
    # （不使用「补播到最近未推日期」，否则晚安装的人会读到过去的 slot，
    #  与早安装的人在同一天拿到不同内容，破坏一致性。）
    pending = articles_for_date(feed, today, db=db)  # 已含中文译文，无需再翻译
    if not pending:
        log.info(
            "内容日历中今天（%s）没有可推送的文献（可能尚未到首个推送日，或当天无排期）",
            today,
        )
        return db, [], build_report([], cfg, total_found=0), {
            "检索命中": 0, "新增入库": 0, "重复跳过": 0,
        }

    # 每天只推 N 篇（默认 1 篇）；feed 通常已按 daily_limit 分槽
    daily_limit = int(cfg.get("pipeline.daily_limit", 0) or 0)
    if daily_limit and len(pending) > daily_limit:
        log.info("daily_limit=%d，本次仅取 %d 篇，其余留待后续", daily_limit, daily_limit)
        pending = pending[:daily_limit]

    report = build_report(pending, cfg, total_found=feed.get("total_articles", 0))
    stats = {"检索命中": feed.get("total_articles", 0), "新增入库": 0, "重复跳过": 0}
    return db, pending, report, stats


def collect_articles(cfg, days: Optional[int] = None):
    """检索 -> 去重入库 -> 取待推送 -> 翻译 -> 生成日报。

    若 content.mode=feed，则改为从中央内容日历取内容（见 _collect_from_feed），
    保证所有安装者推送一致。

    返回 (db, pending, report, stats)，供 run_once 与预览弹窗命令复用。
    stats 含：检索命中 / 新增入库 / 重复跳过。
    """
    if (cfg.get("content.mode") or "local") == "feed":
        return _collect_from_feed(cfg)

    db = get_database(cfg)
    client = PubMedClient(cfg)
    found = client.search_recent(days=days)

    inserted = 0
    fresh_count = 0
    if found:
        fresh = db.filter_new_articles(found)
        fresh_count = len(fresh)
        inserted, _ = db.save_articles(fresh) if fresh else (0, 0)
        log.info("去重结果：%d 篇为新文献，%d 篇此前已入库", fresh_count, len(found) - fresh_count)
    else:
        log.info("本次检索没有命中任何文献")

    # ---- 4. 取待推送 ----
    max_items = int(cfg.get("report.max_items", 30))
    pending = db.get_unpushed(limit=max_items if max_items > 0 else 100)
    log.info("待推送文献 %d 篇", len(pending))

    # 每天只推 N 篇（默认 1 篇），其余留待后续工作日
    daily_limit = int(cfg.get("pipeline.daily_limit", 0) or 0)
    if daily_limit and len(pending) > daily_limit:
        log.info(
            "daily_limit=%d，本次仅取最新 %d 篇，其余 %d 篇留待后续工作日",
            daily_limit, daily_limit, len(pending) - daily_limit,
        )
        pending = pending[:daily_limit]

    if pending:
        _translate_pending(cfg, db, pending)

    # ---- 5. 生成日报 ----
    report = build_report(pending, cfg, total_found=len(found))
    stats = {
        "检索命中": len(found),
        "新增入库": inserted,
        "重复跳过": (len(found) - fresh_count) if found else 0,
    }
    return db, pending, report, stats


def run_once(
    cfg,
    days: Optional[int] = None,
    no_push: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """执行一次完整流水线，返回运行摘要。"""
    started = time.time()
    log.info(SEP)
    log.info("开始执行文献推送任务  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info(SEP)

    summary: Dict[str, Any] = {
        "开始时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "检索命中": 0, "新增入库": 0, "重复跳过": 0,
        "本期推送": 0, "推送结果": {}, "日报文件": [],
    }

    try:
        db, pending, report, stats = collect_articles(cfg, days=days)
    except Exception as exc:
        log.exception("流水线执行失败：%s", exc)
        summary["错误"] = f"执行失败：{exc}"
        return summary
    summary.update(stats)
    summary["本期推送"] = report.count

    saved = save_report(report, cfg)
    summary["日报文件"] = [str(p) for p in saved]

    # ---- 6 & 7. 推送 ----
    if dry_run or no_push:
        log.info("%s 模式：跳过推送", "试运行" if dry_run else "免推送")
    else:
        results = Notifier(cfg, db=db).send(report)
        summary["推送结果"] = results
        if results and any(results.values()) and report.pmids:
            db.mark_pushed(report.pmids)
        elif report.pmids and results:
            log.warning("全部推送渠道失败，本期 %d 篇保留待下次重试", report.count)

    # ---- 收尾 ----
    keep_days = int(cfg.get("database.keep_days", 0))
    if keep_days > 0:
        db.cleanup(keep_days)

    summary["耗时"] = f"{time.time() - started:.1f}s"
    log.info(SEP)
    log.info(
        "任务完成 | 命中 %s 篇 | 新增 %s 篇 | 推送 %s 篇 | 耗时 %s",
        summary["检索命中"], summary["新增入库"], summary["本期推送"], summary["耗时"],
    )
    log.info(SEP)
    return summary


def backfill(cfg, start: str, end: str, no_push: bool = True) -> Dict[str, Any]:
    """历史回填：把某段时间的文献抓进数据库（默认不推送，避免刷屏）。"""
    log.info("历史回填：%s ~ %s", start, end)
    db = get_database(cfg)
    client = PubMedClient(cfg)
    articles = client.search_by_range(start, end, max_results=int(cfg.get("pubmed.max_results", 100)))
    fresh = db.filter_new_articles(articles)
    inserted, _ = db.save_articles(fresh) if fresh else (0, 0)

    if no_push:
        # 直接标记为已推送，避免下次日报被历史文献淹没
        db.mark_pushed([a.pmid for a in fresh])

    log.info("回填完成：命中 %d 篇，新增 %d 篇", len(articles), inserted)
    return {"命中": len(articles), "新增": inserted, "已标记为已推送": no_push}


def start_scheduler(cfg) -> None:
    """常驻进程，按配置的时间点每天执行任务。"""
    try:
        import schedule  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少依赖 schedule，请执行： pip install -r requirements.txt") from exc

    run_at: List[str] = [str(t).strip() for t in (cfg.get("scheduler.run_at") or ["08:30"])]
    run_on_start = bool(cfg.get("scheduler.run_on_start", False))

    def job() -> None:
        today = datetime.now()
        if not _is_workday(today, cfg):
            log.info("今天（%s）是休息日，按配置跳过本次推送。", today.strftime("%Y-%m-%d"))
            return
        try:
            run_once(cfg)
        except Exception as exc:  # pragma: no cover
            log.exception("定时任务执行异常：%s", exc)

    schedule.clear()
    for t in run_at:
        try:
            schedule.every().day.at(t).do(job)
            log.info("已注册每日定时任务：%s", t)
        except Exception as exc:
            log.error("时间格式错误 %r（应为 HH:MM）：%s", t, exc)

    if not schedule.get_jobs():
        raise RuntimeError("没有成功注册任何定时任务，请检查 scheduler.run_at 配置")

    if run_on_start:
        log.info("run_on_start=true，先立即执行一次")
        job()

    next_run = schedule.next_run()
    log.info("调度器已启动，下一次运行：%s（Ctrl+C 退出）",
             next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "未知")

    try:
        while True:
            schedule.run_pending()
            time.sleep(20)
    except KeyboardInterrupt:
        log.info("收到退出信号，调度器已停止")
