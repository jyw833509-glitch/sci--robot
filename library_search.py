"""Local, English live, and Chinese-assisted personal literature search."""
from __future__ import annotations

import threading
import webbrowser
from tkinter import BOTH, END, LEFT, RIGHT, X, BooleanVar, StringVar, Tk
from tkinter import ttk

from config import load_config
from database import get_database
from query_translate import translate_chinese_query
from sources import MultiSourceClient


def show_library_search() -> None:
    cfg, db = load_config(), get_database(load_config())
    root = Tk()
    root.title("SciRobot · 文献库搜索")
    root.geometry("1020x650")
    root.minsize(780, 500)
    notebook = ttk.Notebook(root)
    notebook.pack(fill=BOTH, expand=True, padx=14, pady=14)

    def make_table(parent):
        columns = ("date", "source", "journal", "title")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        for key, text, width in [("date", "日期", 110), ("source", "来源", 105), ("journal", "期刊", 150), ("title", "标题", 530)]:
            tree.heading(key, text=text); tree.column(key, width=width, stretch=key == "title")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True); scroll.pack(side=RIGHT, fill="y")
        return tree

    # ----- Local, offline search -----
    local = ttk.Frame(notebook, padding=14); notebook.add(local, text="本地文献库")
    local_query, local_source, local_status = StringVar(), StringVar(value="全部来源"), StringVar()
    bar = ttk.Frame(local); bar.pack(fill=X, pady=(0, 10))
    ttk.Label(bar, text="关键词：").pack(side=LEFT)
    entry = ttk.Entry(bar, textvariable=local_query, width=44); entry.pack(side=LEFT, padx=(0, 10))
    ttk.Label(bar, text="来源：").pack(side=LEFT)
    source_box = ttk.Combobox(bar, textvariable=local_source, state="readonly", width=14); source_box.pack(side=LEFT, padx=(0, 10))
    local_body = ttk.Frame(local); local_body.pack(fill=BOTH, expand=True)
    local_tree = make_table(local_body); local_items = {}
    ttk.Label(local, textvariable=local_status, foreground="#64748b").pack(anchor="w", pady=(8, 0))

    def load_local(_event=None):
        source_box["values"] = ["全部来源", *db.sources()]
        if local_source.get() not in source_box["values"]: local_source.set("全部来源")
        articles = db.search_library(local_query.get(), local_source.get())
        local_tree.delete(*local_tree.get_children()); local_items.clear()
        for article in articles:
            item = local_tree.insert("", END, values=(article.pushed_at or article.pub_date, article.source, article.journal or "—", article.title_zh or article.title))
            local_items[item] = article
        local_status.set(f"本地找到 {len(articles)} 篇；包含已推送、待推送和手动收藏的文献。")

    ttk.Button(bar, text="搜索本地库", command=load_local).pack(side=LEFT)
    entry.bind("<Return>", load_local)
    local_tree.bind("<Double-1>", lambda _e: webbrowser.open(local_items[local_tree.selection()[0]].pubmed_url) if local_tree.selection() else None)
    load_local()

    # ----- Live, online search -----
    online = ttk.Frame(notebook, padding=14); notebook.add(online, text="联网即时检索")
    online_query, days_var, online_status = StringVar(), StringVar(value="30"), StringVar(value="默认精准检索：逗号分隔的每个主题都必须命中。")
    strict_var = BooleanVar(value=True)
    online_bar = ttk.Frame(online); online_bar.pack(fill=X, pady=(0, 10))
    ttk.Label(online_bar, text="英文关键词：").pack(side=LEFT)
    online_entry = ttk.Entry(online_bar, textvariable=online_query, width=42); online_entry.pack(side=LEFT, padx=(0, 10))
    ttk.Label(online_bar, text="回溯天数：").pack(side=LEFT)
    ttk.Spinbox(online_bar, from_=1, to=365, width=5, textvariable=days_var).pack(side=LEFT, padx=(0, 10))
    ttk.Checkbutton(online_bar, text="精准检索（全部命中）", variable=strict_var).pack(side=LEFT)
    online_body = ttk.Frame(online); online_body.pack(fill=BOTH, expand=True)
    online_tree = make_table(online_body); online_items = {}
    footer = ttk.Frame(online); footer.pack(fill=X, pady=(8, 0))
    ttk.Label(footer, textvariable=online_status, foreground="#64748b").pack(side=LEFT)

    def populate_online(articles):
        online_tree.delete(*online_tree.get_children()); online_items.clear()
        for article in articles:
            item = online_tree.insert("", END, values=(article.pub_date, article.source, article.journal or "—", article.title))
            online_items[item] = article
        mode = "精准" if strict_var.get() else "宽泛"
        online_status.set(f"{mode}检索找到 {len(articles)} 篇（已按 DOI、PMID 和标题去重）。选择后可加入本地库。")

    def run_online():
        keywords = online_query.get().strip()
        if not keywords:
            online_status.set("请先输入英文关键词。"); return
        try: days = int(days_var.get())
        except ValueError: days = 30
        strict = strict_var.get()
        online_status.set("正在从五个免费来源检索，请稍候…")
        def worker():
            try:
                articles = MultiSourceClient(cfg).search_keywords(keywords, days, strict=strict)
                root.after(0, lambda: populate_online(articles))
            except Exception as exc:
                root.after(0, lambda exc=exc: online_status.set(f"检索失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def add_selected():
        selected = [online_items[item] for item in online_tree.selection()]
        if not selected:
            online_status.set("请先选择要加入的文献。"); return
        inserted, skipped = db.save_articles(selected, manual_saved=True)
        online_status.set(f"已加入本地库 {inserted} 篇，已存在跳过 {skipped} 篇；这些文献不会进入每日推送队列。")
        load_local()

    ttk.Button(online_bar, text="联网搜索", command=run_online).pack(side=LEFT)
    ttk.Button(footer, text="加入本地库", command=add_selected).pack(side=RIGHT)
    online_entry.bind("<Return>", lambda _e: run_online())
    online_tree.bind("<Double-1>", lambda _e: webbrowser.open(online_items[online_tree.selection()[0]].pubmed_url) if online_tree.selection() else None)

    # ----- Chinese-assisted live search -----
    chinese = ttk.Frame(notebook, padding=14); notebook.add(chinese, text="中文文献检索")
    chinese_query = StringVar()
    chinese_days = StringVar(value="30")
    chinese_strict = BooleanVar(value=True)
    chinese_status = StringVar(value="只返回原文语言为中文的文献；部分 PubMed 记录只提供英文索引题名。")

    chinese_bar = ttk.Frame(chinese); chinese_bar.pack(fill=X, pady=(0, 8))
    ttk.Label(chinese_bar, text="中文主题：").pack(side=LEFT)
    chinese_entry = ttk.Entry(chinese_bar, textvariable=chinese_query, width=48)
    chinese_entry.pack(side=LEFT, padx=(0, 10))
    ttk.Label(chinese_bar, text="回溯天数：").pack(side=LEFT)
    ttk.Spinbox(chinese_bar, from_=1, to=365, width=5, textvariable=chinese_days).pack(side=LEFT, padx=(0, 10))
    ttk.Checkbutton(chinese_bar, text="精准检索（全部命中）", variable=chinese_strict).pack(side=LEFT)
    ttk.Button(chinese_bar, text="搜索中文文献", command=lambda: run_chinese_search()).pack(side=LEFT, padx=(10, 0))

    chinese_body = ttk.Frame(chinese); chinese_body.pack(fill=BOTH, expand=True)
    chinese_tree = make_table(chinese_body); chinese_items = {}
    chinese_footer = ttk.Frame(chinese); chinese_footer.pack(fill=X, pady=(8, 0))
    ttk.Label(chinese_footer, textvariable=chinese_status, foreground="#64748b").pack(side=LEFT)

    def populate_chinese(articles, mode: str):
        chinese_tree.delete(*chinese_tree.get_children()); chinese_items.clear()
        for article in articles:
            item = chinese_tree.insert("", END, values=(article.pub_date, article.source, article.journal or "—", article.title_zh or article.title))
            chinese_items[item] = article
        chinese_status.set(f"{mode}检索找到 {len(articles)} 篇中文原文（已去重）。选择后可加入本地库。")

    def run_chinese_search():
        query = chinese_query.get().strip()
        if not query:
            chinese_status.set("请先输入中文主题或关键词。"); return
        try: days = int(chinese_days.get())
        except ValueError: days = 30
        strict = chinese_strict.get()
        mode = "精准" if strict else "宽泛"
        chinese_status.set("正在转换检索词并筛选中文原文，请稍候…")

        def worker():
            try:
                english, _method = translate_chinese_query(query, cfg)
                articles = MultiSourceClient(cfg).search_chinese(query, english, days, strict=strict)
                root.after(0, lambda: populate_chinese(articles, mode))
            except Exception as exc:
                root.after(0, lambda exc=exc: chinese_status.set(f"检索失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def add_chinese_selected():
        selected = [chinese_items[item] for item in chinese_tree.selection()]
        if not selected:
            chinese_status.set("请先选择要加入的文献。"); return
        inserted, skipped = db.save_articles(selected, manual_saved=True)
        chinese_status.set(f"已加入本地库 {inserted} 篇，已存在跳过 {skipped} 篇；这些文献不会进入每日推送队列。")
        load_local()

    ttk.Button(chinese_footer, text="加入本地库", command=add_chinese_selected).pack(side=RIGHT)
    chinese_entry.bind("<Return>", lambda _e: run_chinese_search())
    chinese_tree.bind("<Double-1>", lambda _e: webbrowser.open(chinese_items[chinese_tree.selection()[0]].pubmed_url) if chinese_tree.selection() else None)
    root.mainloop()


if __name__ == "__main__":
    show_library_search()
