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
from preferences import load_preferences, personal_mode_active
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
    configured_mode = cfg.get("content.mode") or "local"
    if configured_mode == "feed" and not personal_mode_active():
        return _collect_from_feed(cfg)
    if configured_mode == "feed":
        log.info("已启用个人偏好优先：跳过共享 feed，使用本机 PubMed 检索")

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
    preferences = load_preferences()
    daily_limit = int(preferences.get("daily_limit") or cfg.get("pipeline.daily_limit", 0) or 0)
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


# 托盘事件标志（由 subclass WNDPROC 在主线程设置，tkinter after 轮询）
_tray_event_flags: list = []  # ["popup"] or ["quit"]

# 模块级引用，防止被垃圾回收
_tray_wndproc_prev = None  # 原始窗口过程
_tray_wndproc_new = None   # 新窗口过程（保持引用）
_tray_nid_ref = None       # NOTIFYICONDATA 结构
_tray_hicon_ref = None     # 图标句柄
_tray_root_ref = None      # tkinter root（清理用）


def _run_tray(cfg) -> None:
    """启动系统托盘图标。

    架构（v3 — 主线程 subclassing）：
        不再使用独立 daemon 线程创建 message-only 窗口。
        改为 subclass tkinter 根窗口的 WNDPROC，使其直接接收并
        拦截 WM_TRAYICON 消息。所有逻辑运行在主线程 tkinter
        mainloop 中，彻底杜绝线程逸出和 daemon 异常被吞问题。
    """
    global _running, _tray_wndproc_prev, _tray_wndproc_new
    global _tray_nid_ref, _tray_hicon_ref, _tray_root_ref
    import ctypes
    import ctypes.wintypes
    import os
    import subprocess
    import sys
    import tempfile
    import atexit
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
    TPM_RETURNCMD = 0x0100
    IDI_APPLICATION = 32512
    NOTIFYICON_VERSION_4 = 4
    GWLP_WNDPROC = -4

    # Explorer 重启后会广播此消息
    WM_TASKBAR_CREATED = ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")

    # ---- 生成 ico（纯字节，零依赖）----
    ico_path = None
    hicon = None

    def _make_ico_bytes(sizes=(16, 32)):
        """生成透明底、黄绿双色 S 的多尺寸 .ico 文件字节（32 位 BGRA）。"""
        import struct

        def _render(size):
            w = h = size
            pixels = []
            s_pattern = [
                "01111110", "10000001", "10000000", "01111110",
                "00000001", "00000001", "10000001", "01111110",
            ]
            scale = max(1, size // 8)
            offset = (size - 8 * scale) // 2
            for y in range(h):
                for x in range(w):
                    py = (y - offset) // scale
                    px = (x - offset) // scale
                    if 0 <= py < 8 and 0 <= px < 8 and s_pattern[py][px] == "1":
                        # 黄色（上）到绿色（下）的双色渐变。BGRA 字节顺序。
                        ratio = py / 7
                        red = round(250 + (34 - 250) * ratio)
                        green = round(204 + (197 - 204) * ratio)
                        blue = round(21 + (94 - 21) * ratio)
                        pixels.append((blue, green, red, 255))
                    else:
                        pixels.append((0, 0, 0, 0))
            xor_data = b""
            for y in range(h - 1, -1, -1):
                for x in range(w):
                    b, g, r_, a = pixels[y * w + x]
                    xor_data += struct.pack("BBBB", b, g, r_, a)
            raw_row = (w + 7) // 8
            padded_row = (raw_row + 3) & ~3
            and_rows = []
            for y in range(h):
                row_bits = []
                for x in range(w):
                    _, _, _, a = pixels[y * w + x]
                    row_bits.append('1' if a >= 128 else '0')
                row_bits.extend(['0'] * (padded_row * 8 - w))
                row_bytes = b''.join(
                    bytes([int(''.join(row_bits[i:i + 8][::-1]), 2)])
                    for i in range(0, padded_row * 8, 8)
                )
                and_rows.append(row_bytes)
            and_data = b''.join(and_rows)
            bmp_size = 40 + len(xor_data) + len(and_data)
            bmp_header = struct.pack(
                "<IiiHHIIiiII",
                40, w, h * 2, 1, 32, 0, len(xor_data), 0, 0, 0, 0,
            )
            return bmp_header + xor_data + and_data, bmp_size

        images = []
        offset = 6 + 16 * len(sizes)
        for size in sizes:
            data, bmp_size = _render(size)
            images.append((size, data, bmp_size, offset))
            offset += bmp_size
        header = struct.pack("<HHH", 0, 1, len(sizes))
        entries = b""
        data_blob = b""
        for size, data, bmp_size, off in images:
            entries += struct.pack(
                "<BBBBHHII",
                size if size < 256 else 0,
                size if size < 256 else 0,
                0, 0, 1, 32, bmp_size, off,
            )
            data_blob += data
        return header + entries + data_blob

    try:
        ico_bytes = _make_ico_bytes((16, 32))
        ico_path = os.path.join(tempfile.gettempdir(), "scirobot_tray.ico")
        with open(ico_path, "wb") as f:
            f.write(ico_bytes)
        atexit.register(lambda p=ico_path: os.remove(p) if os.path.exists(p) else None)
        hicon = ctypes.windll.user32.LoadImageW(
            0, ico_path, IMAGE_ICON,
            ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON),
            ctypes.windll.user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
        if hicon:
            log.info("托盘 ICO 已生成，hicon=%s", hicon)
        else:
            log.warning("LoadImageW 返回 NULL，GetLastError=%d",
                        ctypes.windll.kernel32.GetLastError())
    except Exception as exc:
        log.warning("生成托盘图标失败：%s", exc)
        hicon = None

    _tray_hicon_ref = hicon  # 保持引用

    # ---- NOTIFYICONDATAW 结构 ----
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

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.uID = 1
    nid.uCallbackMessage = WM_TRAYICON
    nid.szTip = "SciRobot 文献推送"
    nid.hIcon = hicon or ctypes.windll.user32.LoadIconW(None, IDI_APPLICATION) or 0
    nid.uFlags = NIF_MESSAGE | NIF_TIP
    if nid.hIcon:
        nid.uFlags |= NIF_ICON
    else:
        log.warning("托盘图标加载失败，将以纯文字提示方式运行")

    _tray_nid_ref = nid  # 保持引用

    # ---- WNDPROC 签名和 subclass ----
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong,
        ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
    )

    # 右键菜单
    def _tray_show_menu(hwnd_):
        menu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(menu, 0x00000000, 1001, "查看今日文献")
        ctypes.windll.user32.AppendMenuW(menu, 0x00000000, 1003, "偏好设置")
        ctypes.windll.user32.AppendMenuW(menu, 0x00000800, 0, "")
        ctypes.windll.user32.AppendMenuW(menu, 0x00000000, 1002, "退出 SciRobot")
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(hwnd_)
        cmd = ctypes.windll.user32.TrackPopupMenu(
            # 没有 TPM_RETURNCMD 时，函数只返回是否成功显示菜单（通常是 1），
            # 而不会返回菜单项的 1001 / 1002 命令 ID。
            menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, hwnd_, None,
        )
        ctypes.windll.user32.DestroyMenu(menu)
        log.info("托盘菜单选择: cmd=%s", cmd)
        if cmd == 1001:
            _tray_event_flags.append("popup")
            log.info("托盘事件: 加入 popup 标志")
        elif cmd == 1002:
            _tray_event_flags.append("quit")
            log.info("托盘事件: 加入 quit 标志")
        elif cmd == 1003:
            _tray_event_flags.append("preferences")
            log.info("托盘事件: 加入 preferences 标志")

    # ---- 设置 CallWindowProcW 的正确签名（64-bit 下 LR ESULT 是 8 字节）----
    _CallWindowProcW = ctypes.windll.user32.CallWindowProcW
    _CallWindowProcW.argtypes = [
        WNDPROC, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
    ]
    _CallWindowProcW.restype = ctypes.c_longlong

    # ---- 新的窗口过程（subclass tkinter 窗口）----
    _tray_msg_count = [0]  # 使用列表以便在闭包中修改

    @WNDPROC
    def _new_wndproc(hwnd_, msg, wparam, lparam):
        _tray_msg_count[0] += 1
        is_tray = (msg == WM_TRAYICON)

        # 首条消息确认 subclass 生效。鼠标悬停会产生大量托盘消息，不能逐条
        # 写日志，否则会拖慢 tkinter 的 UI 线程。
        if _tray_msg_count[0] == 1:
            log.info("WNDPROC subclass 已生效！首条消息 msg=0x%x", msg)
        if _tray_msg_count[0] % 5000 == 0:
            log.info("WNDPROC 已处理 %d 条消息", _tray_msg_count[0])

        if msg == WM_TRAYICON:
            # NOTIFYICON_VERSION_4 会把鼠标事件放在 lParam 的低 16 位，
            # 高 16 位是图标 ID / 坐标信息；不能直接比较完整 lParam。
            tray_event = lparam & 0xFFFF
            if tray_event == WM_RBUTTONUP:
                log.info("托盘右键 → 显示菜单")
                _tray_show_menu(hwnd_)
                return 0
            elif tray_event == WM_LBUTTONDBLCLK:
                # 双击还会先产生两次 WM_LBUTTONUP；若同时处理它们会连续弹出
                # 三个窗口。因此只把“双击完成”作为打开文献的唯一触发条件。
                log.info("托盘左键双击 → 弹出文献")
                _tray_event_flags.append("popup")
                return 0
        elif msg == WM_TASKBAR_CREATED:
            log.info("检测到 Explorer 重启，重新挂载托盘图标")
            _tray_add_icon(hwnd_)
            return 0
        # 其余消息转发给原始窗口过程
        return _CallWindowProcW(
            _tray_wndproc_prev, hwnd_, msg, wparam, lparam,
        )

    _tray_wndproc_new = _new_wndproc  # 保持引用防 GC

    def _tray_add_icon(hwnd_):
        """挂载/重新挂载托盘图标。"""
        nid.hWnd = hwnd_
        ok = ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ok:
            err = ctypes.windll.kernel32.GetLastError()
            log.error("Shell_NotifyIcon(ADD) 失败，GetLastError=%d，uFlags=0x%x",
                      err, nid.uFlags)
            return
        # 设置版本 4（现代托盘行为）
        nid2 = NOTIFYICONDATAW()
        ctypes.memmove(ctypes.byref(nid2), ctypes.byref(nid), ctypes.sizeof(NOTIFYICONDATAW))
        nid2.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid2))
        log.info("Shell_NotifyIcon(ADD) 成功！SciRobot 托盘图标已创建")

    # ============================================================
    # 主线程：创建 tkinter 窗口 → subclass WNDPROC → 挂托盘 → mainloop
    # ============================================================
    root = tk.Tk()
    root.withdraw()
    _tray_root_ref = root

    # 获取 tkinter 根窗口的 HWND
    # tkinter 在 Windows 上使用 winfo_id() 返回 HWND
    tk_hwnd = root.winfo_id()
    log.info("tkinter 根窗口 HWND = %s", tk_hwnd)

    # Subclass：替换 WNDPROC，拦截 WM_TRAYICON
    SetWindowLongPtrW = ctypes.windll.user32.SetWindowLongPtrW
    SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, WNDPROC]
    SetWindowLongPtrW.restype = WNDPROC

    _tray_wndproc_prev = SetWindowLongPtrW(tk_hwnd, GWLP_WNDPROC, _new_wndproc)
    if not _tray_wndproc_prev:
        log.error("Subclass 窗口过程失败！SetWindowLongPtrW 返回 NULL，GetLastError=%d",
                  ctypes.windll.kernel32.GetLastError())
    else:
        log.info("窗口过程 subclass 成功，原 WNDPROC = %s", _tray_wndproc_prev)

    # 挂载托盘图标
    _tray_add_icon(tk_hwnd)

    # 主线程轮询托盘事件标志
    def _poll_tray_events():
        """在 tkinter mainloop 中消费托盘事件。"""
        while _tray_event_flags:
            evt = _tray_event_flags.pop(0)
            if evt == "popup":
                try:
                    _trigger_popup_only(cfg)
                except Exception as exc:
                    log.exception("托盘弹窗失败：%s", exc)
            elif evt == "preferences":
                command = [sys.executable, "preferences"] if getattr(sys, "frozen", False) else [sys.executable, "main.py", "preferences"]
                subprocess.Popen(command, cwd=os.getcwd())
            elif evt == "quit":
                _running = False
                # 删除托盘图标
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                log.info("托盘图标已删除，退出程序")
                root.destroy()
                return
        root.after(200, _poll_tray_events)

    root.after(200, _poll_tray_events)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
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
