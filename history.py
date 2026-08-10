"""Local push-history browser."""
from __future__ import annotations

import webbrowser
from tkinter import BOTH, END, LEFT, RIGHT, X, StringVar, Tk
from tkinter import ttk

from config import load_config
from database import get_database


def show_history() -> None:
    articles = get_database(load_config()).get_pushed_history()
    root = Tk()
    root.title("SciRobot · 推送历史")
    root.geometry("920x600")
    root.minsize(720, 430)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill=BOTH, expand=True)
    ttk.Label(frame, text="推送历史", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"已记录 {len(articles)} 篇成功推送的文献；双击条目可打开原文。", foreground="#64748b").pack(anchor="w", pady=(3, 12))

    columns = ("date", "source", "journal", "title")
    tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
    for key, label, width in [("date", "推送时间", 145), ("source", "来源", 105), ("journal", "期刊", 155), ("title", "标题", 480)]:
        tree.heading(key, text=label)
        tree.column(key, width=width, stretch=key == "title")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side=LEFT, fill=BOTH, expand=True)
    scroll.pack(side=RIGHT, fill="y")

    by_item = {}
    for article in articles:
        item = tree.insert("", END, values=(article.pushed_at, article.source, article.journal or "—", article.title_zh or article.title))
        by_item[item] = article

    status = StringVar(value="双击条目打开原文；所有记录仅保存在本机。")
    ttk.Label(frame, textvariable=status, foreground="#64748b").pack(fill=X, pady=(10, 0))

    def open_selected(_event=None) -> None:
        selected = tree.selection()
        if not selected:
            return
        article = by_item[selected[0]]
        webbrowser.open(article.pubmed_url)
        status.set(f"已打开：{article.source} · {article.title[:60]}")

    tree.bind("<Double-1>", open_selected)
    root.mainloop()


if __name__ == "__main__":
    show_history()
