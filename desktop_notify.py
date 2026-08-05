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
C_WIN_BG       = "#e5e5ea"   # 窗外背景（macOS 浅灰）
C_CARD_BG      = "#ffffff"   # 卡片白
C_HEADER_BG    = "#f9f9f9"   # 标题栏淡灰底
C_ACCENT       = "#007AFF"   # Apple Blue
C_ACCENT_HOVER = "#0056cc"   # 深蓝 hover
C_TEXT         = "#1d1d1f"   # 主文字（近黑）
C_TEXT_SEC     = "#86868b"   # 次要文字
C_TEXT_TER     = "#aeaeb2"   # 三级文字
C_DIVIDER      = "#e5e5ea"   # 分割线
C_ABSTRACT_BG  = "#f5f5f7"   # 摘要块底
C_TAG_BG       = "#007AFF"   # 标签背景
C_TAG_TEXT     = "#ffffff"
C_CLOSE        = "#ff5f57"   # 红绿灯 — 关闭
C_CLOSE_HOVER  = "#ff3b30"

# 窗口尺寸
WIN_W = 460
WIN_H = 680


def open_url(url: str) -> None:
    if url:
        webbrowser.open(url)


def _on_link_enter(event):
    event.widget.configure(fg=C_ACCENT_HOVER)


def _on_link_leave(event, orig_color):
    event.widget.configure(fg=orig_color)


# ═══════════════════════════════════════════════════════════════
# 字体 — 优先 Segoe UI (Win11), 回退 Microsoft YaHei
# ═══════════════════════════════════════════════════════════════
def _make_fonts():
    families = ["Segoe UI", "Microsoft YaHei"]
    try:
        return {
            "title":   Font(family=families[0], size=15, weight="bold"),
            "heading": Font(family=families[0], size=12, weight="bold"),
            "body":    Font(family=families[0], size=10),
            "small":   Font(family=families[0], size=9),
            "tiny":    Font(family=families[0], size=8),
            "link":    Font(family=families[0], size=9, underline=False),
            "badge":   Font(family=families[0], size=11, weight="bold"),
        }
    except Exception:
        return {
            "title":   Font(size=15, weight="bold"),
            "heading": Font(size=12, weight="bold"),
            "body":    Font(size=10),
            "small":   Font(size=9),
            "tiny":    Font(size=8),
            "link":    Font(size=9, underline=False),
            "badge":   Font(size=11, weight="bold"),
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
    outer = Frame(win, bg=C_WIN_BG, padx=10, pady=10)
    outer.pack(fill=BOTH, expand=True)

    # 白色卡片主体
    card = Frame(outer, bg=C_CARD_BG)
    card.pack(fill=BOTH, expand=True)

    # ── 标题栏 ──
    _build_header(card, title, f, win, root)

    # ── 分割线 ──
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, padx=0, pady=0)

    # ── 内容区（纯 Frame，不需要 Canvas 滚动） ──
    if not articles:
        _show_empty(card, f)
    else:
        _show_articles(card, articles, f)

    # ── 底部分割线 + 提示 ──
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, side=BOTTOM)
    footer = Frame(card, bg=C_CARD_BG, padx=20, pady=10)
    footer.pack(fill=X, side=BOTTOM)
    Label(footer, text="SciRobot  ·  每个工作日 08:30 自动推送",
          bg=C_CARD_BG, fg=C_TEXT_TER, font=f["tiny"]).pack(side=LEFT)

    if auto_close > 0:
        win.after(auto_close * 1000, lambda: _close(root, win))

    win.bind("<Escape>", lambda e: _close(root, win))
    win.protocol("WM_DELETE_WINDOW", lambda: _close(root, win))
    win.lift()
    win.focus_force()

    # 强制刷新布局，确保所有 pack 计算完成
    win.update_idletasks()
    win.mainloop()


# ═══════════════════════════════════════════════════════════════
# 标题栏
# ═══════════════════════════════════════════════════════════════
def _build_header(card, title, f, win, root):
    """macOS 风格标题栏：红绿灯 + 标题居中。"""
    header = Frame(card, bg=C_HEADER_BG, padx=14, pady=12)
    header.pack(fill=X)
    header.bind("<Button-1>", lambda e: _start_move(e, win))
    header.bind("<B1-Motion>", lambda e: _do_move(e, win))

    # 红绿灯关闭按钮
    close_btn = Label(header, text="●", bg=C_HEADER_BG, fg=C_CLOSE,
                      font=Font(family="Segoe UI", size=14), cursor="hand2")
    close_btn.pack(side=LEFT)
    close_btn.bind("<Button-1>", lambda e: _close(root, win))
    close_btn.bind("<Enter>", lambda e: close_btn.configure(fg=C_CLOSE_HOVER))
    close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=C_CLOSE))

    # 标题居中
    Label(header, text=title, bg=C_HEADER_BG, fg=C_TEXT_SEC,
          font=f["small"]).pack(side=LEFT, padx=(16, 0))


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
    inner = Frame(canvas, bg=C_CARD_BG, padx=16, pady=8)
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    # Canvas 宽度变化时同步内部 frame 宽度
    def _on_canvas_configure(event):
        canvas.itemconfig(inner_id, width=event.width)
    canvas.bind("<Configure>", _on_canvas_configure)

    # 内部 frame 大小变化时刷新滚动区域
    def _on_inner_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    inner.bind("<Configure>", _on_inner_configure)

    # ── 鼠标滚轮 ──
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # 鼠标进入 Canvas 区域时绑定滚轮，离开时解绑（避免影响其他窗口）
    def _on_enter(event):
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _on_leave(event):
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _on_enter)
    canvas.bind("<Leave>", _on_leave)

    # ── 填充文献 ──
    for i, art in enumerate(articles, 1):
        _build_card(inner, i, art, f)

    Frame(inner, bg=C_CARD_BG, height=4).pack(fill=X)


# 可用内容宽度（用于 wraplength）
# 窗口 460 - outer padx 20 - content padx 32 - card padx 40 = 368
_AVAILABLE_W = 368


# ═══════════════════════════════════════════════════════════════
# 单篇文献卡片
# ═══════════════════════════════════════════════════════════════
def _build_card(parent, index, art, f):
    """macOS 风格文献卡片。"""
    card = Frame(parent, bg=C_CARD_BG, padx=20, pady=16)
    card.pack(fill=X, padx=0, pady=(0, 8))

    # ---- 期刊标签 + 日期 ----
    meta_row = Frame(card, bg=C_CARD_BG)
    meta_row.pack(fill=X)

    journal = art.get("journal") or art.get("journal_abbr") or ""
    if len(journal) > 45:
        journal = journal[:42] + "…"
    Label(meta_row, text=journal, bg=C_CARD_BG, fg=C_ACCENT,
          font=f["small"]).pack(side=LEFT)

    pub_date = art.get("pub_date", "") or ""
    pub_type = art.get("publication_types", [])
    if isinstance(pub_type, list) and pub_type:
        pub_date = f"{pub_date}  ·  {pub_type[0]}"
    Label(meta_row, text=pub_date, bg=C_CARD_BG, fg=C_TEXT_TER,
          font=f["tiny"]).pack(side=RIGHT)

    # ---- 中文标题 ----
    title_zh = art.get("title_zh") or art.get("title") or f"PMID {art.get('pmid','')}"
    Label(card, text=title_zh, bg=C_CARD_BG, fg=C_TEXT,
          font=f["title"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(10, 6))

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
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, pady=(14, 0))

    # ---- 中文摘要 ----
    abstract_zh = art.get("abstract_zh", "")
    if abstract_zh:
        ab_frame = Frame(card, bg=C_CARD_BG, padx=0, pady=0)
        ab_frame.pack(fill=X, pady=(12, 0))

        lbl = Label(ab_frame, text="摘要", bg=C_CARD_BG, fg=C_TEXT_SEC,
                    font=f["small"])
        lbl.pack(anchor=W)

        Label(ab_frame, text=abstract_zh, bg=C_CARD_BG, fg=C_TEXT,
              font=f["body"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(6, 0))

    # ---- 英文摘要 ----
    abstract_en = art.get("abstract", "")
    if abstract_en:
        en_frame = Frame(card, bg=C_CARD_BG, padx=0, pady=0)
        en_frame.pack(fill=X, pady=(12, 4))

        Label(en_frame, text="English Abstract", bg=C_CARD_BG, fg=C_TEXT_TER,
              font=f["tiny"]).pack(anchor=W)

        Label(en_frame, text=abstract_en, bg=C_CARD_BG, fg=C_TEXT_SEC,
              font=f["small"], justify="left", wraplength=_AVAILABLE_W).pack(anchor=W, pady=(4, 0))


# ═══════════════════════════════════════════════════════════════
# 关闭
# ═══════════════════════════════════════════════════════════════
def _close(root, win):
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
