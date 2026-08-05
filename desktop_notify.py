"""
desktop_notify.py —— 桌面弹窗（tkinter）

被 notifier 以「独立子进程」方式调用，避免阻塞主调度循环：
    python desktop_notify.py <payload.json>

payload.json 结构：
    {
      "title": "抗体纯化文献日报",
      "articles": [
        {
          "pmid": "...", "title": "...", "title_zh": "...",
          "authors": "...", "journal": "...", "pub_date": "...",
          "doi": "...", "pubmed_url": "...", "doi_url": "...",
          "abstract": "...", "abstract_zh": "...", "keywords": [...]
        }
      ]
    }

也可手动测试： python desktop_notify.py sample.json
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
import tkinter as tk
from tkinter import ttk, scrolledtext


FONT = ("Microsoft YaHei", 10)
FONT_TITLE = ("Microsoft YaHei", 13, "bold")
FONT_HEAD = ("Microsoft YaHei", 18, "bold")
FONT_SUB = ("Microsoft YaHei", 10)
FONT_META = ("Microsoft YaHei", 10)

C_PRIMARY = "#0e7490"
C_PRIMARY_DARK = "#155e75"
C_ACCENT = "#0891b2"
C_BG = "#f4f6f8"
C_CARD = "#ffffff"
C_BORDER = "#e2e8f0"
C_TEXT = "#1f2937"
C_MUTED = "#64748b"
C_LIGHT = "#94a3b8"


def open_url(url: str) -> None:
    if url:
        webbrowser.open(url)


def build_card(parent: tk.Widget, art: dict, index: int) -> None:
    """在 parent 中构建一张文献卡片。"""
    card = tk.Frame(
        parent, bg=C_CARD, bd=1, relief="solid",
        highlightbackground=C_BORDER, highlightthickness=1,
    )
    card.pack(fill="x", padx=14, pady=9)

    # 序号 + 标题
    header = tk.Frame(card, bg=C_CARD)
    header.pack(fill="x", padx=16, pady=(14, 4))
    num = tk.Label(header, text=str(index), bg=C_PRIMARY, fg="white",
                   font=("Microsoft YaHei", 10, "bold"), width=3, height=1)
    num.pack(side="left")
    title_text = art.get("title_zh") or art.get("title") or f"PMID {art.get('pmid', '')}"
    title = tk.Label(header, text=title_text, bg=C_CARD, fg=C_TEXT,
                     font=FONT_TITLE, wraplength=620, justify="left", anchor="w")
    title.pack(side="left", fill="x", expand=True, padx=(10, 0))

    # 英文原标题（仅当存在中文译名时补充展示）
    if art.get("title_zh") and art.get("title"):
        en = tk.Label(card, text=art.get("title", ""), bg=C_CARD, fg=C_MUTED,
                      font=FONT, wraplength=620, justify="left", anchor="w")
        en.pack(fill="x", padx=16, pady=(0, 6))

    # 元信息
    meta = tk.Frame(card, bg=C_CARD)
    meta.pack(fill="x", padx=16, pady=(0, 6))
    info = "期刊：{}   ·   发表：{}   ·   PMID：{}\n作者：{}".format(
        art.get("journal", "—"), art.get("pub_date", "—"),
        art.get("pmid", "—"), art.get("authors", "—"),
    )
    tk.Label(meta, text=info, bg=C_CARD, fg=C_MUTED, font=FONT_META,
             justify="left", anchor="w").pack(anchor="w")

    # 链接（PubMed / DOI 点击打开浏览器）
    links = tk.Frame(card, bg=C_CARD)
    links.pack(fill="x", padx=16, pady=(0, 6))
    pub_url = art.get("pubmed_url")
    if pub_url:
        l1 = tk.Label(links, text="PubMed 链接", fg=C_ACCENT,
                      font=("Microsoft YaHei", 10, "underline"), cursor="hand2", bg=C_CARD)
        l1.pack(side="left", padx=(0, 14))
        l1.bind("<Button-1>", lambda e, u=pub_url: open_url(u))
    doi_url = art.get("doi_url")
    if doi_url:
        l2 = tk.Label(links, text="DOI: {}".format(art.get("doi", "")), fg=C_ACCENT,
                      font=("Microsoft YaHei", 10, "underline"), cursor="hand2", bg=C_CARD)
        l2.pack(side="left", padx=(0, 14))
        l2.bind("<Button-1>", lambda e, u=doi_url: open_url(u))

    # 中文摘要
    if art.get("abstract_zh"):
        tk.Label(card, text="中文摘要", bg=C_CARD, fg=C_PRIMARY,
                 font=("Microsoft YaHei", 11, "bold"), anchor="w").pack(fill="x", padx=16, pady=(4, 2))
        zh = scrolledtext.ScrolledText(card, wrap="word", height=8, bg="#f0fdfa",
                                       fg=C_TEXT, font=FONT, relief="flat", bd=0)
        zh.insert("1.0", art.get("abstract_zh", ""))
        zh.config(state="disabled")
        zh.pack(fill="x", padx=16, pady=(0, 6))

    # 英文摘要（可折叠）
    if art.get("abstract"):
        en_frame = tk.Frame(card, bg=C_CARD)
        en_frame.pack(fill="x", padx=16, pady=(0, 8))
        toggle = tk.Button(en_frame, text="展开英文原文摘要", bg=C_CARD, fg=C_LIGHT,
                           font=FONT, relief="flat", cursor="hand2", anchor="w")
        toggle.pack(anchor="w")

        en_box = scrolledtext.ScrolledText(en_frame, wrap="word", height=8, bg="#f8fafc",
                                           fg=C_MUTED, font=FONT, relief="flat", bd=0)
        en_box.insert("1.0", art.get("abstract", ""))
        en_box.config(state="disabled")
        en_box.pack_forget()

        def _make_toggle(box, btn):
            def _toggle():
                if box.winfo_viewable():
                    box.pack_forget()
                    btn.config(text="展开英文原文摘要")
                else:
                    box.pack(fill="x", pady=(4, 0))
                    btn.config(text="收起英文原文摘要")
            return _toggle

        toggle.config(command=_make_toggle(en_box, toggle))


def run_window(data: dict) -> None:
    articles = data.get("articles", [])
    title = data.get("title", "抗体纯化文献日报")

    root = tk.Tk()
    root.title(title)
    root.geometry("820x640")

    # 顶部标题栏
    header = tk.Frame(root, bg=C_PRIMARY_DARK, height=70)
    header.pack(fill="x")
    tk.Label(header, text=title, bg=C_PRIMARY_DARK, fg="white", font=FONT_HEAD).pack(side="left", padx=20, pady=18)
    tk.Label(header, text="本期 {} 篇  ·  数据来源 PubMed".format(len(articles)),
             bg=C_PRIMARY_DARK, fg="#cffafe", font=FONT_SUB).pack(side="right", padx=20)

    # 滚动区
    canvas = tk.Canvas(root, bg=C_BG, highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=C_BG)
    canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_configure(event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    inner.bind("<Configure>", _on_configure)

    if not articles:
        tk.Label(inner, text="今日没有检索到符合条件的新文献", bg=C_BG, fg=C_MUTED, font=FONT).pack(pady=40)
    else:
        for i, art in enumerate(articles, 1):
            build_card(inner, art, i)

    # 底部关闭栏
    footer = tk.Frame(root, bg=C_BG)
    footer.pack(fill="x", side="bottom")
    btn = tk.Button(footer, text="关闭", bg=C_PRIMARY, fg="white", font=("Microsoft YaHei", 11),
                    relief="flat", padx=22, pady=6, command=root.destroy)
    btn.pack(side="right", padx=16, pady=8)

    # 鼠标滚轮滚动
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    root.mainloop()


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python desktop_notify.py <payload.json>")
        sys.exit(1)
    path = sys.argv[1]
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        print("读取弹窗数据失败：", exc)
        sys.exit(1)
    try:
        run_window(data)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
