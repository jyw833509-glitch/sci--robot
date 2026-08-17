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
from translate import Translator


def _contains_chinese(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in (text or ""))


def _prepare_chinese_titles(articles, translator, db, remote_limit: int = 12, progress=None):
    """Prefer native/cached Chinese titles and remotely translate a small queue.

    ``articles`` is already relevance-ranked.  Cached translations never consume
    the per-search remote budget; uncached English titles are translated one at a
    time so free fallback providers cannot be flooded by concurrent requests.
    """
    native, cached, pending = [], [], []
    for article in articles:
        if _contains_chinese(article.title):
            native.append(article)
            continue
        if _contains_chinese(article.title_zh):
            cached.append(article)
            continue
        cached_title = db.get_translation(article.title) if article.title else None
        if _contains_chinese(cached_title or ""):
            article.title_zh = cached_title
            article.translate_provider = "cache"
            cached.append(article)
        else:
            pending.append(article)

    budget = max(0, int(remote_limit or 0))
    queue = pending[:budget]
    translated = []
    for index, article in enumerate(queue, 1):
        if progress:
            progress(index, len(queue), len(native), len(cached))
        title_zh, provider = translator.translate_text(article.title)
        if _contains_chinese(title_zh):
            article.title_zh = title_zh
            article.translate_provider = provider
            translated.append(article)

    visible = [*native, *cached, *translated]
    stats = {
        "native": len(native),
        "cached": len(cached),
        "translated": len(translated),
        "remote_attempted": len(queue),
        "not_shown": len(articles) - len(visible),
    }
    return visible, stats


def _lookback_days(value: str, unit: str) -> tuple[int, str]:
    """Convert a day/month/year UI choice to a bounded search window."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        amount = 1
    limits = {"日": 1825, "月": 60, "年": 5}
    amount = max(1, min(limits.get(unit, 1825), amount))
    days = amount if unit == "日" else amount * 30 if unit == "月" else amount * 365
    return days, f"{amount}{unit}"


def _bind_period_limit(spinbox, value_var: StringVar, unit_var: StringVar) -> None:
    limits = {"日": 1825, "月": 60, "年": 5}

    def update_limit(*_args):
        maximum = limits.get(unit_var.get(), 1825)
        spinbox.configure(to=maximum)
        try:
            if int(value_var.get()) > maximum:
                value_var.set(str(maximum))
        except ValueError:
            value_var.set("1")

    unit_var.trace_add("write", update_limit)
    update_limit()


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
    online_query = StringVar()
    days_var, period_unit = StringVar(value="1"), StringVar(value="月")
    online_status = StringVar(value="默认精准检索：逗号分隔的每个主题都必须命中。")
    strict_var = BooleanVar(value=True)
    online_bar = ttk.Frame(online); online_bar.pack(fill=X, pady=(0, 10))
    ttk.Label(online_bar, text="英文关键词：").pack(side=LEFT)
    online_entry = ttk.Entry(online_bar, textvariable=online_query, width=42); online_entry.pack(side=LEFT, padx=(0, 10))
    ttk.Label(online_bar, text="回溯：").pack(side=LEFT)
    online_period_spin = ttk.Spinbox(online_bar, from_=1, to=1825, width=5, textvariable=days_var)
    online_period_spin.pack(side=LEFT)
    ttk.Combobox(online_bar, values=("日", "月", "年"), textvariable=period_unit,
                 state="readonly", width=3).pack(side=LEFT, padx=(4, 10))
    _bind_period_limit(online_period_spin, days_var, period_unit)
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
        days, period_text = _lookback_days(days_var.get(), period_unit.get())
        strict = strict_var.get()
        online_status.set(f"正在检索近{period_text}文献并进行相关性核验，请稍候…")
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
    chinese_days, chinese_unit = StringVar(value="1"), StringVar(value="月")
    chinese_strict = BooleanVar(value=True)
    chinese_status = StringVar(value="只返回原文语言为中文的文献；部分 PubMed 记录只提供英文索引题名。")

    chinese_bar = ttk.Frame(chinese); chinese_bar.pack(fill=X, pady=(0, 8))
    ttk.Label(chinese_bar, text="中文主题：").pack(side=LEFT)
    chinese_entry = ttk.Entry(chinese_bar, textvariable=chinese_query, width=48)
    chinese_entry.pack(side=LEFT, padx=(0, 10))
    ttk.Label(chinese_bar, text="回溯：").pack(side=LEFT)
    chinese_period_spin = ttk.Spinbox(chinese_bar, from_=1, to=1825, width=5, textvariable=chinese_days)
    chinese_period_spin.pack(side=LEFT)
    ttk.Combobox(chinese_bar, values=("日", "月", "年"), textvariable=chinese_unit,
                 state="readonly", width=3).pack(side=LEFT, padx=(4, 10))
    _bind_period_limit(chinese_period_spin, chinese_days, chinese_unit)
    ttk.Checkbutton(chinese_bar, text="精准检索（全部命中）", variable=chinese_strict).pack(side=LEFT)
    ttk.Button(chinese_bar, text="搜索中文文献", command=lambda: run_chinese_search()).pack(side=LEFT, padx=(10, 0))

    chinese_body = ttk.Frame(chinese); chinese_body.pack(fill=BOTH, expand=True)
    chinese_tree = make_table(chinese_body); chinese_items = {}
    chinese_tree.tag_configure("preprint", foreground="#b45309")
    chinese_footer = ttk.Frame(chinese); chinese_footer.pack(fill=X, pady=(8, 0))
    ttk.Label(chinese_footer, textvariable=chinese_status, foreground="#64748b").pack(side=LEFT)

    def populate_chinese(articles, mode: str, title_stats=None):
        chinese_tree.delete(*chinese_tree.get_children()); chinese_items.clear()
        for article in articles:
            title = article.title_zh or article.title
            if article.source.startswith("ChinaXiv"):
                title = f"【预印本、未经严格同行评议】{title}"
            tags = ("preprint",) if article.source.startswith("ChinaXiv") else ()
            item = chinese_tree.insert("", END, values=(article.pub_date, article.source, article.journal or "—", title), tags=tags)
            chinese_items[item] = article
        title_stats = title_stats or {}
        native = int(title_stats.get("native", len(articles)))
        cached = int(title_stats.get("cached", 0))
        translated = int(title_stats.get("translated", 0))
        not_shown = int(title_stats.get("not_shown", 0))
        note = f"；另有 {not_shown} 篇未消耗额度、暂不显示" if not_shown else ""
        preprints = sum(article.source.startswith("ChinaXiv") for article in articles)
        warning = f"；其中 {preprints} 篇为预印本、未经严格同行评议" if preprints else ""
        chinese_status.set(
            f"{mode}检索显示 {len(articles)} 篇中文原文：原生题名 {native}、"
            f"缓存 {cached}、本次翻译 {translated}{note}。选择后可加入本地库{warning}。"
        )

    def translate_chinese_titles(articles):
        try:
            limit = max(0, int(cfg.get("translate.search_title_limit", 12) or 0))
        except (TypeError, ValueError):
            limit = 12
        translator = Translator(cfg, db)  # 复用实例：失败后不再反复请求同一后端

        def progress(index, total, native, cached):
            root.after(
                0,
                lambda: chinese_status.set(
                    f"已找到 {len(articles)} 篇中文原文；原生题名 {native}、缓存 {cached}，"
                    f"正在顺序翻译高相关标题 {index}/{total}…"
                ),
            )

        return _prepare_chinese_titles(articles, translator, db, limit, progress)

    def run_chinese_search():
        query = chinese_query.get().strip()
        if not query:
            chinese_status.set("请先输入中文主题或关键词。"); return
        days, period_text = _lookback_days(chinese_days.get(), chinese_unit.get())
        strict = chinese_strict.get()
        mode = "精准" if strict else "宽泛"
        chinese_status.set(f"正在检索近{period_text}中文原文并进行相关性核验，请稍候…")

        def worker():
            try:
                english, _method = translate_chinese_query(query, cfg)
                articles = MultiSourceClient(cfg).search_chinese(query, english, days, strict=strict)
                articles, title_stats = translate_chinese_titles(articles)
                root.after(0, lambda: populate_chinese(articles, mode, title_stats))
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
