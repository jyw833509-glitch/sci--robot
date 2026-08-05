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
from datetime import datetime, timedelta, time as dt_time
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
    """常驻进程，按配置的时间点每天执行任务（含系统托盘图标）。
    不使用外部 schedule 库，自行实现定时逻辑以避免 PyInstaller 打包问题。"""
    global _running

    run_at: List[str] = [str(t).strip() for t in (cfg.get("scheduler.run_at") or ["08:30"])]
    run_on_start = bool(cfg.get("scheduler.run_on_start", False))

    # 解析定时任务时间列表
    job_times: List[dt_time] = []
    for t in run_at:
        try:
            h, m = map(int, t.split(":"))
            job_times.append(dt_time(h, m))
            log.info("已注册每日定时任务：%s", t)
        except Exception as exc:
            log.error("时间格式错误 %r（应为 HH:MM）：%s", t, exc)
    if not job_times:
        raise RuntimeError("没有成功注册任何定时任务，请检查 scheduler.run_at 配置")

    _running = True

    def job() -> None:
        today = datetime.now()
        if not _is_workday(today, cfg):
            log.info("今天（%s）是休息日，按配置跳过本次推送。", today.strftime("%Y-%m-%d"))
            return
        try:
            run_once(cfg)
        except Exception as exc:  # pragma: no cover
            log.exception("定时任务执行异常：%s", exc)

    # 计算下次运行时间
    def _next_run_time() -> Optional[datetime]:
        """返回最近一个未过期的定时时间点（datetime）。"""
        now = datetime.now()
        today = now.date()
        candidates = [
            datetime.combine(today, t) for t in sorted(job_times)
        ]
        # 如果所有时间点都已过，返回明天的第一个
        future = [c for c in candidates if c > now]
        if future:
            return future[0]
        return datetime.combine(today + timedelta(days=1), sorted(job_times)[0])

    def _should_run_now(now: datetime, last_run_date: list) -> bool:
        """检查是否需要在这个时间点执行。「last_run_date」用 list 包装以支持闭包内修改。"""
        now_time = now.time()
        for jt in sorted(job_times):
            # 在当前分钟窗口内
            if abs((now.hour * 60 + now.minute) - (jt.hour * 60 + jt.minute)) <= 1:
                key = (now.date(), jt)
                if key not in last_run_date:
                    last_run_date.append(key)
                    return True
        return False

    # ---- 调度循环跑在后台线程 ----
    import threading

    def _scheduler_loop() -> None:
        last_run: list = []  # 已执行的 (date, time) 记录
        if run_on_start:
            log.info("run_on_start=true，先立即执行一次")
            job()
        next_run = _next_run_time()
        log.info("调度器已启动，下一次运行：%s",
                 next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "未知")
        while _running:
            now = datetime.now()
            if _should_run_now(now, last_run):
                log.info("到达预定时间，开始执行每日任务")
                job()
                # 清理过期记录
                today = now.date()
                last_run[:] = [k for k in last_run if k[0] >= today]
            time.sleep(20)
        log.info("调度器线程已退出")

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scirobot-scheduler")
    t.start()

    # ---- 系统托盘图标（主线程） ----
    _run_tray(cfg)


# 托盘事件队列（由托盘线程写入，tkinter 主线程消费）
# 放在模块级别以避免闭包引用问题
import queue as _tray_queue_module
_tray_event_queue: "_tray_queue_module.Queue" = _tray_queue_module.Queue()

# 托盘线程引用
_tray_thread_ref = None
_tray_cleanup_ref = None  # 清理函数引用


def _run_tray(cfg) -> None:
    """启动系统托盘图标。

    架构：独立线程创建纯 Win32 消息窗口（message-only window），
    专门接收托盘回调消息；用线程安全队列通知 tkinter 主线程。
    两个线程各自独立运行，彻底避免消息竞争。
    """
    global _running, _tray_thread_ref, _tray_cleanup_ref
    import ctypes
    import ctypes.wintypes
    import os
    import tempfile
    import atexit
    import threading
    import tkinter as tk

    # ---- 隐藏控制台 ----
    try:
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass

    # ---- Win32 常量 ----
    WM_TRAYICON = 0x8001  # WM_APP + 1
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIM_SETVERSION = 0x00000004
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONUP = 0x0202
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    SM_CXSMICON = 49
    SM_CYSMICON = 50
    TPM_LEFTALIGN = 0x0000
    TPM_RIGHTBUTTON = 0x0002
    HWND_MESSAGE = -3
    WS_POPUP = 0x80000000
    CS_HREDRAW = 0x0002
    CS_VREDRAW = 0x0001
    COLOR_WINDOW = 5
    IDI_APPLICATION = 32512

    # ---- 生成 ico ----
    ico_path = None
    hicon = None
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(0, 113, 227, 255))
        draw.text((25, 14), "S", fill=(255, 255, 255, 255))
        ico_path = os.path.join(tempfile.gettempdir(), "scirobot_tray.ico")
        img.save(ico_path, format="ICO", sizes=[(64, 64)])
        atexit.register(lambda p=ico_path: os.remove(p) if os.path.exists(p) else None)
        # LR_LOADFROMFILE 时 hinst 必须为 NULL(0)，否则 LoadImageW 失败
        hicon = ctypes.windll.user32.LoadImageW(
            0, ico_path, IMAGE_ICON,
            ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON),
            ctypes.windll.user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
    except Exception as exc:
        log.warning("生成托盘图标失败：%s，使用系统默认图标", exc)
        hicon = None

    # 兜底：如果 hicon 无效，用系统默认图标
    # ---- Shell_NotifyIcon 结构 ----
    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("hWnd", ctypes.wintypes.HWND),
            ("uID", ctypes.wintypes.UINT),
            ("uFlags", ctypes.wintypes.UINT),
            ("uCallbackMessage", ctypes.wintypes.UINT),
            ("hIcon", ctypes.wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", ctypes.wintypes.DWORD),
            ("dwStateMask", ctypes.wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uTimeoutOrVersion", ctypes.wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", ctypes.wintypes.DWORD),
        ]

    # 兜底：确保 hicon 有效（在 nid 创建之后处理标志位）
    if not hicon:
        hicon = ctypes.windll.user32.LoadIconW(None, IDI_APPLICATION)

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.uID = 1
    nid.uCallbackMessage = WM_TRAYICON
    nid.szTip = "SciRobot 文献推送"
    nid.hIcon = hicon or 0
    # 有图标才设 NIF_ICON 标志；无图标至少 NIF_MESSAGE+NIF_TIP 能让托盘文字提示工作
    nid.uFlags = NIF_MESSAGE | NIF_TIP
    if hicon:
        nid.uFlags |= NIF_ICON
    else:
        log.warning("托盘图标加载失败，将以纯文字提示方式运行")

    # ============================================================
    # 后台线程：纯 Win32 消息窗口 + 消息循环
    # ============================================================
    _tray_stop = threading.Event()

    # 窗口过程签名
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong,
        ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
    )

    # ---- 在消息窗口中显示右键菜单 ----
    def _tray_show_menu(msg_hwnd):
        menu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(menu, 0x00000000, 1001, "查看今日文献")
        ctypes.windll.user32.AppendMenuW(menu, 0x00000800, 0, "")
        ctypes.windll.user32.AppendMenuW(menu, 0x00000000, 1002, "退出 SciRobot")
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(msg_hwnd)
        cmd = ctypes.windll.user32.TrackPopupMenu(
            menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, msg_hwnd, None,
        )
        ctypes.windll.user32.DestroyMenu(menu)
        if cmd == 1001:
            _tray_event_queue.put("popup")
        elif cmd == 1002:
            _tray_event_queue.put("quit")

    # ---- 消息窗口过程 ----
    @WNDPROC
    def _tray_wndproc(hwnd_, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_RBUTTONUP:
                _tray_show_menu(hwnd_)
                return 0
            elif lparam == WM_LBUTTONDBLCLK:
                _tray_event_queue.put("popup")
                return 0
            elif lparam == WM_LBUTTONUP:
                _tray_event_queue.put("popup")
                return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd_, msg, wparam, lparam)

    def _tray_thread():
        """后台线程：注册窗口类 -> 创建消息窗口 -> 消息循环。"""
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

        # 注册窗口类
        class_name = "SciRobotTrayMsgWindow"
        wndclass = ctypes.wintypes.WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(ctypes.wintypes.WNDCLASSEXW)
        wndclass.lpfnWndProc = _tray_wndproc
        wndclass.hInstance = hinst
        wndclass.lpszClassName = class_name
        wndclass.style = CS_HREDRAW | CS_VREDRAW
        wndclass.hbrBackground = COLOR_WINDOW + 1

        atom = ctypes.windll.user32.RegisterClassExW(ctypes.byref(wndclass))
        if not atom:
            log.error("托盘消息窗口类注册失败，错误码 %d",
                      ctypes.windll.kernel32.GetLastError())
            return

        # 创建 message-only 窗口
        msg_hwnd = ctypes.windll.user32.CreateWindowExW(
            0, class_name, "", WS_POPUP,
            0, 0, 0, 0, HWND_MESSAGE, None, hinst, None,
        )
        if not msg_hwnd:
            log.error("托盘消息窗口创建失败，错误码 %d",
                      ctypes.windll.kernel32.GetLastError())
            return

        # 挂载托盘图标
        nid.hWnd = msg_hwnd
        result = ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not result:
            log.error("Shell_NotifyIcon(ADD) 失败，错误码 %d",
                      ctypes.windll.kernel32.GetLastError())
            return
        # 设置版本（Win2000+ 支持 NIF_INFO 等）
        nid_ver = NOTIFYICONDATAW()
        nid_ver.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid_ver.hWnd = msg_hwnd
        nid_ver.uID = 1
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid_ver))

        log.info("系统托盘图标已创建（独立消息窗口），右键退出 / 单击/双击查看今日文献")

        # 消息循环
        msg_struct = ctypes.wintypes.MSG()
        while not _tray_stop.is_set():
            # PeekMessage 非阻塞，60ms 轮询一次以响应停止信号
            if ctypes.windll.user32.PeekMessageW(
                ctypes.byref(msg_struct), None, 0, 0, 1,  # PM_REMOVE = 1
            ):
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg_struct))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg_struct))
            else:
                # 用 WaitMessage + 短超时替代纯 sleep，更快响应
                ctypes.windll.user32.WaitMessage()

        # 清理托盘图标
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        log.info("托盘图标已删除")

    # 清理函数
    def _tray_cleanup():
        _tray_stop.set()
        # 给托盘消息线程发一条假消息以唤醒 WaitMessage
        if hicon:
            ctypes.windll.user32.DestroyIcon(hicon)

    _tray_cleanup_ref = _tray_cleanup

    _tray_thread_ref = threading.Thread(
        target=_tray_thread, daemon=True, name="scirobot-tray")
    _tray_thread_ref.start()

    # ============================================================
    # 主线程：tkinter 事件循环 + 消费托盘事件队列
    # ============================================================
    root = tk.Tk()
    root.withdraw()

    def _process_tray_events():
        """定期检查托盘消息队列，在 tkinter 主线程执行回调。"""
        try:
            while True:
                evt = _tray_event_queue.get_nowait()
                if evt == "popup":
                    _trigger_popup_only(cfg)
                elif evt == "quit":
                    global _running
                    _running = False
                    _tray_cleanup()
                    root.destroy()
                    return
        except _tray_queue_module.Empty:
            pass
        root.after(150, _process_tray_events)

    root.after(200, _process_tray_events)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        _tray_cleanup()
        try:
            root.destroy()
        except Exception:
            pass
    log.info("托盘进程已退出")


# 全局变量供托盘菜单使用
_running = True


def _trigger_popup_only(cfg) -> None:
    """单独触发弹窗（不标记已推送，不影响正常排期）。"""
    from feed import articles_for_date, load_feed

    feed_mode = (cfg.get("content.mode") or "local") == "feed"
    if not feed_mode:
        log.warning("非 feed 模式，不支持托盘菜单查看今日文献")
        return

    try:
        feed = load_feed(
            cfg.get("content.feed_url") or "",
            cfg,
            cache_path=cfg.path("content.feed_cache", "data/feed_cache.json"),
        )
        today = datetime.now().strftime("%Y-%m-%d")
        pending = articles_for_date(feed, today)
        if not pending:
            log.info("今天没有排期文献")
            return
        daily_limit = int(cfg.get("pipeline.daily_limit", 0) or 0)
        if daily_limit:
            pending = pending[:daily_limit]
        Notifier(cfg).send_popup_only(pending)
    except Exception as exc:
        log.exception("托盘菜单触发展示失败：%s", exc)
