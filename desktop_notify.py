"""
desktop_notify.py —— 桌面弹窗（右下角 macOS 通知风格原生窗口）

被 notifier 以「独立子进程」方式调用，避免阻塞主调度循环：
    python desktop_notify.py <payload.json>

macOS 通知中心风格：白色卡片 + 浅灰背景 + 大圆角视觉 + SF 风排版。
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from tkinter import Tk, Toplevel, Frame, Label, Canvas, Scrollbar
from tkinter import BOTH, RIGHT, LEFT, TOP, BOTTOM, X, Y, W, END, VERTICAL
from tkinter.font import Font


# ═══════════════════════════════════════════════════════════════
# 配色 — Apple Human Interface Guidelines 浅色模式
# ═══════════════════════════════════════════════════════════════
C_WIN_BG       = "#eef2f7"   # 窗外背景
C_CARD_BG      = "#ffffff"   # 卡片白
C_HEADER_BG    = "#f8fafc"   # 标题栏淡灰底
C_ACCENT       = "#007AFF"   # Apple Blue
C_ACCENT_HOVER = "#0056cc"   # 深蓝 hover
C_TEXT         = "#1d1d1f"   # 主文字（近黑）
C_TEXT_SEC     = "#475569"   # 次要文字
C_TEXT_TER     = "#94a3b8"   # 三级文字
C_DIVIDER      = "#e2e8f0"   # 分割线
C_ABSTRACT_BG  = "#f5f5f7"   # 摘要块底
C_TAG_BG       = "#007AFF"   # 标签背景
C_TAG_TEXT     = "#ffffff"
C_CLOSE        = "#ff5f57"   # 红绿灯 — 关闭
C_CLOSE_HOVER  = "#ff3b30"

# 窗口尺寸
WIN_W = 620
WIN_H = 720


def open_url(url: str) -> None:
    if url:
        webbrowser.open(url)


def _on_link_enter(event):
    event.widget.configure(fg=C_ACCENT_HOVER)


def _on_link_leave(event, orig_color):
    event.widget.configure(fg=orig_color)


# ═══════════════════════════════════════════════════════════════
# 字体 — Windows 原生 UI 字体，优先保证中英文在高分屏上的清晰度
# ═══════════════════════════════════════════════════════════════
def _make_fonts():
    # 微软雅黑 UI 的字形和抗锯齿均更适合 Windows 通知窗口；宋体在小字号
    # 下容易发虚，Times New Roman 也无法完整覆盖中文。
    family = "Microsoft YaHei UI"
    try:
        return {
            "title":   Font(family=family, size=12, weight="bold"),
            "heading": Font(family=family, size=10, weight="bold"),
            "body":    Font(family=family, size=10),
            "small":   Font(family=family, size=9),
            "tiny":    Font(family=family, size=8),
            "link":    Font(family=family, size=9, underline=False),
            "badge":   Font(family=family, size=10, weight="bold"),
        }
    except Exception:
        return {
            "title":   Font(size=12, weight="bold"),
            "heading": Font(size=10, weight="bold"),
            "body":    Font(size=10),
            "small":   Font(size=9),
            "tiny":    Font(size=8),
            "link":    Font(size=9, underline=False),
            "badge":   Font(size=10, weight="bold"),
        }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
def show_popup(data: dict) -> None:
    articles = data.get("articles", [])
    title = data.get("title", "SciRobot")
    auto_close = int(data.get("auto_close_seconds", 0) or 0)

    root = Tk()
    root.withdraw()

    win = Toplevel(root)
    win.title(title)
    win.configure(bg=C_WIN_BG)
    win.overrideredirect(True)
    win.attributes("-topmost", True)

    win.geometry(
        f"{WIN_W}x{WIN_H}+{win.winfo_screenwidth() - WIN_W - 24}"
        f"+{win.winfo_screenheight() - WIN_H - 64}"
    )

    f = _make_fonts()

    # ── 外层容器（模拟卡片圆角 + 阴影效果） ──
    outer = Frame(win, bg=C_WIN_BG, padx=12, pady=12)
    outer.pack(fill=BOTH, expand=True)

    # 白色卡片主体
    card = Frame(outer, bg=C_CARD_BG)
    card.pack(fill=BOTH, expand=True)

    # ── 标题栏 ──
    _build_header(
        card, title, f, win,
        on_minimize=lambda: _close(root, win, canvas),
        on_close=lambda: _close(root, win, canvas),
    )

    # ── 分割线 ──
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, padx=0, pady=0)

    # ── 内容区（Canvas + Scrollbar 滚动）──
    canvas = None
    if not articles:
        _show_empty(card, f)
    else:
        canvas = _show_articles(card, articles, f)

    # ── 底部分割线 + 提示 ──
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, side=BOTTOM)
    footer = Frame(card, bg=C_CARD_BG, padx=22, pady=11)
    footer.pack(fill=X, side=BOTTOM)
    Label(footer, text="SciRobot  ·  双击主托盘图标可再次打开今日文献",
          bg=C_CARD_BG, fg=C_TEXT_TER, font=f["tiny"]).pack(side=LEFT)

    if auto_close > 0:
        win.after(auto_close * 1000, lambda: _close(root, win, canvas))

    win.bind("<Escape>", lambda e: _close(root, win, canvas))
    win.protocol("WM_DELETE_WINDOW", lambda: _close(root, win, canvas))
    win.lift()
    win.focus_force()

    # ── 全局滚轮绑定（inner Frame 覆盖了 Canvas，Canvas 的 Enter/Leave 永远收不到）──
    def _on_mousewheel(event):
        if canvas:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    win.bind_all("<MouseWheel>", _on_mousewheel)

    # 强制刷新布局，确保所有 pack 计算完成
    win.update_idletasks()
    if canvas:
        canvas.update_idletasks()
        # 保险：强制把 inner 宽度同步为 Canvas 实际显示宽度
        if hasattr(canvas, '_inner') and hasattr(canvas, '_inner_id'):
            cw = canvas.winfo_width()
            if cw > 1:
                canvas._inner.config(width=cw)
                canvas.itemconfig(canvas._inner_id, width=cw)
        # 刷新滚动区域
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)

    win.mainloop()


# ═══════════════════════════════════════════════════════════════
# 标题栏
# ═══════════════════════════════════════════════════════════════
def _build_header(card, title, f, win, on_minimize, on_close):
    """带品牌识别与阅读层级的自定义标题栏。"""
    header = Frame(card, bg=C_HEADER_BG, padx=16, pady=11)
    header.pack(fill=X)
    header.bind("<Button-1>", lambda e: _start_move(e, win))
    header.bind("<B1-Motion>", lambda e: _do_move(e, win))

    Frame(header, bg=C_ACCENT, width=4, height=34).pack(side=LEFT, padx=(0, 10))
    heading = Frame(header, bg=C_HEADER_BG)
    heading.pack(side=LEFT, fill=X, expand=True)
    Label(heading, text="SCIROBOT  ·  DAILY RESEARCH DIGEST", bg=C_HEADER_BG,
          fg=C_ACCENT, font=f["tiny"]).pack(anchor=W)
    Label(heading, text=title, bg=C_HEADER_BG, fg=C_TEXT,
          font=f["heading"]).pack(anchor=W, pady=(1, 0))

    # SciRobot 只使用主程序的一个托盘图标。收起当前通知后，可双击主
    # 托盘图标再次打开今日文献，避免每个通知都注册一个重复托盘图标。
    minimize = Label(
        header, text="—", bg=C_HEADER_BG, fg=C_TEXT_SEC, font=f["heading"],
        cursor="hand2", width=2,
    )
    minimize.pack(side=RIGHT, padx=(4, 0))
    minimize.bind("<Button-1>", lambda e: on_minimize())
    minimize.bind("<Enter>", lambda e: minimize.configure(fg=C_ACCENT))
    minimize.bind("<Leave>", lambda e: minimize.configure(fg=C_TEXT_SEC))

    close = Label(
        header, text="×", bg=C_HEADER_BG, fg=C_CLOSE, font=f["heading"],
        cursor="hand2", width=2,
    )
    close.pack(side=RIGHT)
    close.bind("<Button-1>", lambda e: on_close())
    close.bind("<Enter>", lambda e: close.configure(fg=C_CLOSE_HOVER))
    close.bind("<Leave>", lambda e: close.configure(fg=C_CLOSE))


# ═══════════════════════════════════════════════════════════════
# 拖拽
# ═══════════════════════════════════════════════════════════════
def _start_move(event, win):
    win._x = event.x
    win._y = event.y


def _do_move(event, win):
    win.geometry(f"+{win.winfo_x() + event.x - win._x}+{win.winfo_y() + event.y - win._y}")


# ═══════════════════════════════════════════════════════════════
# 空状态
# ═══════════════════════════════════════════════════════════════
def _show_empty(parent, f):
    box = Frame(parent, bg=C_CARD_BG, padx=40, pady=80)
    box.pack(fill=BOTH, expand=True)
    Label(box, text="📭", bg=C_CARD_BG, font=Font(size=36)).pack()
    Label(box, text="今日暂无新文献", bg=C_CARD_BG, fg=C_TEXT_SEC,
          font=f["body"]).pack(pady=(18, 6))
    Label(box, text="SciRobot 已就位，明天继续为你检索",
          bg=C_CARD_BG, fg=C_TEXT_TER, font=f["small"]).pack()


# ═══════════════════════════════════════════════════════════════
# 文献列表（Canvas + Scrollbar，支持滚动）
# ═══════════════════════════════════════════════════════════════
def _show_articles(parent, articles, f):
    """Canvas + Scrollbar 布局，内容超出时可滚动查看。"""
    # ── 滚动容器 ──
    scroll_container = Frame(parent, bg=C_CARD_BG)
    scroll_container.pack(fill=BOTH, expand=True)

    canvas = Canvas(scroll_container, bg=C_CARD_BG, highlightthickness=0)
    vbar = Scrollbar(scroll_container, orient=VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)

    vbar.pack(side=RIGHT, fill=Y)
    canvas.pack(side=LEFT, fill=BOTH, expand=True)

    # ── 内部内容 Frame ──
    inner = Frame(canvas, bg=C_CARD_BG, padx=8)   # 8px 缓冲防止左侧截断
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    # 把引用存到 canvas 上，供外部做最终保险同步
    canvas._inner = inner
    canvas._inner_id = inner_id

    # Canvas 宽度变化时同步内部 frame 宽度
    def _on_canvas_configure(event):
        w = event.width
        if w > 1:
            inner.config(width=w)
            canvas.itemconfig(inner_id, width=w)
    canvas.bind("<Configure>", _on_canvas_configure)

    # 内部 frame 大小变化时刷新滚动区域
    def _on_inner_configure(event):
        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)
    inner.bind("<Configure>", _on_inner_configure)

    # ── 填充文献 ──
    for i, art in enumerate(articles, 1):
        _build_card(inner, i, art, f)

    Frame(inner, bg=C_CARD_BG, height=4).pack(fill=X)

    return canvas


# 可用内容宽度（用于 wraplength）
# 窗口 620 - 外层/滚动条/卡片边距，留出足够的中文换行空间
_AVAILABLE_W = 520


# ═══════════════════════════════════════════════════════════════
# 单篇文献卡片
# ═══════════════════════════════════════════════════════════════
def _build_card(parent, index, art, f):
    """macOS 风格文献卡片。"""
    card = Frame(
        parent, bg=C_CARD_BG, padx=20, pady=17,
        highlightthickness=1, highlightbackground=C_DIVIDER,
    )
    card.pack(fill=X, padx=4, pady=(0, 10))

    # ---- 期刊标签 + 日期 ----
    meta_row = Frame(card, bg=C_CARD_BG)
    meta_row.pack(fill=X)

    journal = art.get("journal") or art.get("journal_abbr") or ""
    if len(journal) > 45:
        journal = journal[:42] + "…"
    Label(meta_row, text=journal, bg="#eaf2ff", fg="#0056cc",
          font=f["tiny"], padx=7, pady=2).pack(side=LEFT)

    pub_date = art.get("pub_date", "") or ""
    pub_type = art.get("publication_types", [])
    if isinstance(pub_type, list) and pub_type:
        pub_date = f"{pub_date}  ·  {pub_type[0]}"
    Label(meta_row, text=pub_date, bg=C_CARD_BG, fg=C_TEXT_TER,
          font=f["tiny"]).pack(side=RIGHT)

    # ---- 中文标题 ----
    title_zh = art.get("title_zh") or art.get("title") or f"PMID {art.get('pmid','')}"
    Label(card, text=title_zh, bg=C_CARD_BG, fg=C_TEXT,
          font=f["title"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(12, 7))

    # ---- 英文标题 ----
    en_title = art.get("title", "")
    if art.get("title_zh") and en_title:
        Label(card, text=en_title, bg=C_CARD_BG, fg=C_TEXT_SEC,
              font=f["small"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(0, 12))

    # ---- 作者 + PMID ----
    authors = art.get("authors", "")
    if isinstance(authors, list):
        authors = ", ".join(authors)
    if len(authors) > 55:
        authors = authors[:52] + "…"
    pmid = art.get("pmid", "")

    info_row = Frame(card, bg=C_CARD_BG)
    info_row.pack(fill=X)
    Label(info_row, text=authors or "—", bg=C_CARD_BG, fg=C_TEXT_TER,
          font=f["tiny"]).pack(side=LEFT)
    Label(info_row, text=f"PMID {pmid}", bg=C_CARD_BG, fg=C_TEXT_TER,
          font=f["tiny"]).pack(side=RIGHT)

    # ---- PMID / DOI 链接（可点击号码，显示完整） ----
    pmid_val = art.get("pmid", "")
    doi_val = art.get("doi", "")
    pubmed_url = art.get("pubmed_url", "")
    doi_url = art.get("doi_url", "")

    if pmid_val or doi_val:
        link_box = Frame(card, bg=C_CARD_BG)
        link_box.pack(fill=X, pady=(10, 0))

        if pmid_val and pubmed_url:
            pmid_frame = Frame(link_box, bg=C_CARD_BG)
            pmid_frame.pack(fill=X, pady=(0, 2))
            Label(pmid_frame, text="PMID: ", bg=C_CARD_BG, fg=C_TEXT_TER,
                  font=f["tiny"]).pack(side=LEFT)
            pmid_lbl = Label(pmid_frame, text=pmid_val, bg=C_CARD_BG, fg=C_ACCENT,
                             font=f["small"], cursor="hand2")
            pmid_lbl.pack(side=LEFT)
            pmid_lbl.bind("<Button-1>", lambda e, u=pubmed_url: open_url(u))
            pmid_lbl.bind("<Enter>", lambda e: pmid_lbl.configure(fg=C_ACCENT_HOVER))
            pmid_lbl.bind("<Leave>", lambda e: pmid_lbl.configure(fg=C_ACCENT))

        if doi_val and doi_url:
            doi_frame = Frame(link_box, bg=C_CARD_BG)
            doi_frame.pack(fill=X, pady=(0, 2))
            Label(doi_frame, text="DOI: ", bg=C_CARD_BG, fg=C_TEXT_TER,
                  font=f["tiny"]).pack(side=LEFT)
            # DOI 可能很长，截断显示但保留完整链接
            display_doi = doi_val
            if len(display_doi) > 50:
                display_doi = display_doi[:47] + "…"
            doi_lbl = Label(doi_frame, text=display_doi, bg=C_CARD_BG, fg=C_ACCENT,
                            font=f["small"], cursor="hand2")
            doi_lbl.pack(side=LEFT)
            doi_lbl.bind("<Button-1>", lambda e, u=doi_url: open_url(u))
            doi_lbl.bind("<Enter>", lambda e: doi_lbl.configure(fg=C_ACCENT_HOVER))
            doi_lbl.bind("<Leave>", lambda e: doi_lbl.configure(fg=C_ACCENT))

    # ---- 分割线 ----
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, pady=(15, 0))

    # ---- 中文摘要 ----
    abstract_zh = art.get("abstract_zh", "")
    if abstract_zh:
        ab_frame = Frame(card, bg=C_CARD_BG, padx=0, pady=0)
        ab_frame.pack(fill=X, pady=(12, 0))

        lbl = Label(ab_frame, text="中文摘要", bg=C_CARD_BG, fg=C_ACCENT,
                    font=f["heading"])
        lbl.pack(anchor=W)

        Label(ab_frame, text=abstract_zh, bg=C_CARD_BG, fg=C_TEXT,
              font=f["body"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(6, 0))

    # ---- 英文摘要 ----
    abstract_en = art.get("abstract", "")
    if abstract_en:
        en_frame = Frame(card, bg=C_CARD_BG, padx=0, pady=0)
        en_frame.pack(fill=X, pady=(12, 4))

        Label(en_frame, text="ENGLISH ABSTRACT", bg=C_CARD_BG, fg=C_TEXT_TER,
              font=f["tiny"]).pack(anchor=W)

        Label(en_frame, text=abstract_en, bg=C_CARD_BG, fg=C_TEXT_SEC,
              font=f["small"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(4, 0))


# ═══════════════════════════════════════════════════════════════
# 关闭
# ═══════════════════════════════════════════════════════════════
def _close(root, win, canvas=None):
    try:
        win.unbind_all("<MouseWheel>")
    except Exception:
        pass
    try:
        win.destroy()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


def show_from_file(payload_path: str) -> None:
    """从 JSON 文件读取并弹出右下角窗口。"""
    try:
        with open(payload_path, encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        print("读取弹窗数据失败：", exc)
        sys.exit(1)
    try:
        show_popup(data)
    finally:
        try:
            os.remove(payload_path)
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python desktop_notify.py <payload.json>")
        sys.exit(1)
    show_from_file(sys.argv[1])
