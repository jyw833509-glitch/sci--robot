"""
desktop_notify.py —— 桌面弹窗（右下角原生通知窗口）

被 notifier 以「独立子进程」方式调用，避免阻塞主调度循环：
    python desktop_notify.py <payload.json>

Apple 风格浅色界面，无系统标题栏，带滚动，可点击 PubMed / DOI 链接。
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from tkinter import Tk, Toplevel, Frame, Label, Canvas, Scrollbar
from tkinter import BOTH, RIGHT, LEFT, TOP, BOTTOM, X, Y, W, END, VERTICAL
from tkinter.font import Font


# ── 配色：Apple 银灰色系 ─────────────────────────────────
C_BG = "#f5f5f7"
C_CARD = "#ffffff"
C_HEADER = "#e8e8ed"
C_ACCENT = "#0071e3"
C_ACCENT_HOVER = "#0060c0"
C_TEXT = "#1d1d1f"
C_TEXT_MUTED = "#6e6e73"
C_TEXT_DIM = "#aeaeb2"
C_DIVIDER = "#d2d2d7"
C_ABSTRACT_BG = "#f5f5f7"
C_TAG_BG = "#0071e3"
C_BORDER = "#d2d2d7"
C_CLOSE_HOVER = "#ff3b30"


def open_url(url: str) -> None:
    if url:
        webbrowser.open(url)


def _on_link_enter(event):
    event.widget.configure(fg=C_ACCENT_HOVER)


def _on_link_leave(event, orig_color):
    event.widget.configure(fg=orig_color)


def show_popup(data: dict) -> None:
    articles = data.get("articles", [])
    title = data.get("title", "SciRobot")
    auto_close = int(data.get("auto_close_seconds", 0) or 0)

    root = Tk()
    root.withdraw()

    win = Toplevel(root)
    win.title(title)
    win.configure(bg=C_BG)
    win.overrideredirect(True)

    win_width = 480
    win_height = 640

    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    x = screen_w - win_width - 20
    y = screen_h - win_height - 48
    win.geometry(f"{win_width}x{win_height}+{x}+{y}")

    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.98)

    # 窗口圆角——在外部套一层白边
    outer = Frame(win, bg=C_BORDER)
    outer.pack(fill=BOTH, expand=True, padx=0, pady=0)

    inner = Frame(outer, bg=C_BG)
    inner.pack(fill=BOTH, expand=True, padx=1, pady=1)

    try:
        f_h1 = Font(family="Microsoft YaHei", size=14, weight="bold")
        f_h2 = Font(family="Microsoft YaHei", size=12, weight="bold")
        f_body = Font(family="Microsoft YaHei", size=10)
        f_small = Font(family="Microsoft YaHei", size=9)
        f_tiny = Font(family="Microsoft YaHei", size=8)
        f_link = Font(family="Microsoft YaHei", size=9, underline=False)
    except Exception:
        f_h1 = Font(size=14, weight="bold")
        f_h2 = Font(size=12, weight="bold")
        f_body = Font(size=10)
        f_small = Font(size=9)
        f_tiny = Font(size=8)
        f_link = Font(size=9, underline=False)

    # ── 顶栏（可拖拽） ──
    header = Frame(inner, bg=C_HEADER, padx=16, pady=12)
    header.pack(fill=X)
    header.bind("<Button-1>", lambda e: _start_move(e, win))
    header.bind("<B1-Motion>", lambda e: _do_move(e, win))

    header_left = Frame(header, bg=C_HEADER)
    header_left.pack(side=LEFT)
    dot = Label(header_left, text="\u25cf", bg=C_HEADER, fg=C_ACCENT,
                font=Font(family="Microsoft YaHei", size=8))
    dot.pack(side=LEFT, padx=(0, 8))
    Label(header_left, text=title, bg=C_HEADER, fg=C_TEXT,
          font=f_h2).pack(side=LEFT)

    header_right = Frame(header, bg=C_HEADER)
    header_right.pack(side=RIGHT)
    close_btn = Label(header_right, text="\u2715", bg=C_HEADER, fg=C_TEXT_DIM,
                      font=Font(family="Microsoft YaHei", size=12), cursor="hand2")
    close_btn.pack(side=LEFT)
    close_btn.bind("<Button-1>", lambda e: _close(root, win))
    close_btn.bind("<Enter>", lambda e: close_btn.configure(fg=C_CLOSE_HOVER))
    close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=C_TEXT_DIM))

    # ── 可滚动内容 ──
    if not articles:
        _show_empty(inner, f_body)
    else:
        _show_articles(inner, articles, f_h1, f_h2, f_body, f_small, f_tiny, f_link)

    if auto_close > 0:
        win.after(auto_close * 1000, lambda: _close(root, win))

    win.bind("<Escape>", lambda e: _close(root, win))
    win.protocol("WM_DELETE_WINDOW", lambda: _close(root, win))
    win.lift()
    win.focus_force()
    win.mainloop()


def _start_move(event, win):
    win._x = event.x
    win._y = event.y


def _do_move(event, win):
    x = win.winfo_x() + event.x - win._x
    y = win.winfo_y() + event.y - win._y
    win.geometry(f"+{x}+{y}")


def _show_empty(win, font_empty):
    empty = Frame(win, bg=C_BG, padx=30, pady=80)
    empty.pack(fill=BOTH, expand=True)
    Label(empty, text="--", bg=C_BG, fg=C_TEXT_DIM,
          font=Font(size=24)).pack()
    Label(empty, text="今日暂无新文献", bg=C_BG, fg=C_TEXT_MUTED,
          font=font_empty).pack(pady=(16, 4))
    Label(empty, text="SciRobot 已就位，明天继续为你检索 PubMed",
          bg=C_BG, fg=C_TEXT_DIM, font=Font(size=9)).pack()


def _show_articles(win, articles, f_h1, f_h2, f_body, f_small, f_tiny, f_link):
    outer = Frame(win, bg=C_BG, padx=0, pady=0)
    outer.pack(fill=BOTH, expand=True)

    canvas = Canvas(outer, bg=C_BG, highlightthickness=0)
    scrollbar = Scrollbar(outer, orient=VERTICAL, command=canvas.yview,
                          bg=C_DIVIDER, troughcolor=C_BG, width=5)
    canvas.configure(yscrollcommand=scrollbar.set)

    scrollbar.pack(side=RIGHT, fill=Y, padx=(0, 6), pady=8)
    canvas.pack(side=LEFT, fill=BOTH, expand=True, padx=8, pady=8)

    content = Frame(canvas, bg=C_BG)
    canvas_window = canvas.create_window((0, 0), window=content, anchor="nw", width=448)

    def _on_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfig(canvas_window, width=event.width - 12)

    content.bind("<Configure>", _on_configure)
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width - 12))

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", _on_mousewheel)

    for i, art in enumerate(articles, 1):
        _build_card(content, i, art, f_h1, f_h2, f_body, f_small, f_tiny, f_link)

    Frame(content, bg=C_BG, height=8).pack(fill=X)


def _build_card(parent, index, art, f_h1, f_h2, f_body, f_small, f_tiny, f_link):
    card = Frame(parent, bg=C_CARD, padx=16, pady=16)
    card.pack(fill=X, padx=4, pady=(4, 6))

    # 序号 + 期刊
    tag_row = Frame(card, bg=C_CARD)
    tag_row.pack(fill=X)
    Label(tag_row, text=f"  {index}  ", bg=C_TAG_BG, fg="white",
          font=f_tiny, padx=6, pady=1).pack(side=LEFT)
    journal = art.get("journal", "") or art.get("journal_abbr", "") or "—"
    if len(journal) > 50:
        journal = journal[:47] + "..."
    Label(tag_row, text=journal, bg=C_CARD, fg=C_TEXT_MUTED,
          font=f_small).pack(side=LEFT, padx=(8, 0))

    # 中文标题
    title_zh = art.get("title_zh") or art.get("title") or f"PMID {art.get('pmid', '')}"
    Label(card, text=title_zh, bg=C_CARD, fg=C_TEXT,
          font=f_h1, justify="left", wraplength=410).pack(anchor=W, pady=(12, 6))

    # 英文标题
    en_title = art.get("title", "")
    if art.get("title_zh") and en_title:
        Label(card, text=en_title, bg=C_CARD, fg=C_TEXT_MUTED,
              font=f_small, justify="left", wraplength=410).pack(anchor=W, pady=(0, 10))

    # 分割线
    Frame(card, bg=C_DIVIDER, height=1).pack(fill=X, pady=(0, 10))

    # 元信息
    pub_date = art.get("pub_date", "—")
    pmid = art.get("pmid", "—")
    doi = art.get("doi", "")
    doi_short = f"DOI: {doi}" if doi else ""
    meta_parts = [pub_date]
    if doi_short:
        meta_parts.append(doi_short)
    meta_parts.append(f"PMID {pmid}")
    Label(card, text="  \u00b7  ".join(meta_parts), bg=C_CARD, fg=C_TEXT_DIM,
          font=f_tiny).pack(anchor=W)

    # 作者
    authors = art.get("authors", "—")
    if isinstance(authors, list):
        authors = ", ".join(authors)
    if len(authors) > 60:
        authors = authors[:57] + "..."
    Label(card, text=authors, bg=C_CARD, fg=C_TEXT_DIM,
          font=f_tiny).pack(anchor=W, pady=(2, 0))

    # 链接行
    link_frame = Frame(card, bg=C_CARD)
    link_frame.pack(fill=X, pady=(10, 0))

    pubmed_url = art.get("pubmed_url", "")
    doi_url = art.get("doi_url", "")

    if pubmed_url:
        lbl = Label(link_frame, text="PubMed", bg=C_CARD, fg=C_ACCENT,
                    font=f_link, cursor="hand2")
        lbl.pack(side=LEFT)
        lbl.bind("<Button-1>", lambda e, u=pubmed_url: open_url(u))
        lbl.bind("<Enter>", _on_link_enter)
        lbl.bind("<Leave>", lambda e, c=C_ACCENT: _on_link_leave(e, c))

    if doi_url:
        sep = "  \u00b7  " if pubmed_url else ""
        lbl = Label(link_frame, text=f"{sep}DOI", bg=C_CARD, fg=C_ACCENT,
                    font=f_link, cursor="hand2")
        lbl.pack(side=LEFT)
        lbl.bind("<Button-1>", lambda e, u=doi_url: open_url(u))
        lbl.bind("<Enter>", _on_link_enter)
        lbl.bind("<Leave>", lambda e, c=C_ACCENT: _on_link_leave(e, c))

    # ── 中文摘要 ──
    abstract_zh = art.get("abstract_zh", "")
    if abstract_zh:
        ab_card = Frame(card, bg=C_ABSTRACT_BG, padx=12, pady=12)
        ab_card.pack(fill=X, pady=(14, 0))

        Label(ab_card, text="Chinese Abstract", bg=C_ABSTRACT_BG, fg=C_ACCENT,
              font=f_tiny).pack(anchor=W)

        Label(ab_card, text=abstract_zh, bg=C_ABSTRACT_BG, fg=C_TEXT,
              font=f_body, justify="left", wraplength=400).pack(anchor=W, pady=(6, 0))

    # ── 英文原文摘要 ──
    abstract_en = art.get("abstract", "")
    if abstract_en:
        en_card = Frame(card, bg=C_ABSTRACT_BG, padx=12, pady=12)
        en_card.pack(fill=X, pady=(10, 0))

        Label(en_card, text="English Abstract", bg=C_ABSTRACT_BG, fg=C_TEXT_MUTED,
              font=f_tiny).pack(anchor=W)

        Label(en_card, text=abstract_en, bg=C_ABSTRACT_BG, fg=C_TEXT_MUTED,
              font=f_small, justify="left", wraplength=400).pack(anchor=W, pady=(6, 0))


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
