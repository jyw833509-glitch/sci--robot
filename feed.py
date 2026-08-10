"""
feed.py —— 全局同步内容源（中央内容日历）

设计目标：
    让「不论谁安装这个 app，所有人每天推送到的内容都完全一致」。
    做法：由【发布者】一次性把检索 + 翻译好的文献排成一张「内容日历」
    （feed.json），托管到一个所有人可访问的 URL；【客户端】不再各自去
    PubMed 检索，而是每天去拉这份日历，播放「今天」那一条。因为所有
    人读的是同一份文件，内容必然一致，且随发布者更新而同步更新。

feed.json 结构：
{
  "schema_version": 1,
  "generated_at": "2026-08-04 11:00:00",
  "timezone": "Asia/Shanghai",
  "query": "<检索式>",
  "total_articles": 12,
  "calendar": {            // 日期 -> 该日推送的 PMID 列表（默认每天 1 篇）
     "2026-08-05": ["40123456"],
     "2026-08-06": ["40123457"]
  },
  "articles": {           // PMID -> 完整文献（已含中文译文），客户端无需再翻译
     "40123456": { "pmid": "...", "title": "...", "title_zh": "...", ... }
  }
}

对外接口：
    build_feed(cfg, articles, start_date=None) -> dict
    save_feed(feed, path)                         -> 写本地 JSON
    load_feed(source, cfg=None)                  -> dict  （URL 或本地路径）
    next_workday(after, cfg)                     -> datetime
    articles_for_date(feed, date_str, db=None)   -> list[Article]
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from logger import get_logger
from search import Article

log = get_logger("feed")

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# 工作日计算（与 scheduler._is_workday 同源逻辑，但独立实现避免循环依赖）
# --------------------------------------------------------------------------
def _is_workday(check_date: datetime, cfg) -> bool:
    wd = (cfg.get("scheduler.workday") if cfg else None) or {}
    ds = check_date.strftime("%Y-%m-%d")

    if ds in set(wd.get("holidays") or []):
        return False
    if ds in set(wd.get("makeup_workdays") or []):
        return True
    if wd.get("use_chinese_calendar"):
        try:
            import chinese_calendar as clc  # type: ignore
            return clc.is_workday(check_date)
        except Exception:
            pass
    if wd.get("skip_weekends", True):
        return check_date.weekday() < 5
    return True


def next_workday(after: datetime, cfg) -> datetime:
    """返回 after 之后的下一个工作日（含判断节假日/补班）。"""
    d = after + timedelta(days=1)
    while not _is_workday(d, cfg):
        d += timedelta(days=1)
    return d


# --------------------------------------------------------------------------
# 构建内容日历
# --------------------------------------------------------------------------
def build_feed(cfg, articles: List[Article], start_date: Optional[datetime] = None) -> Dict[str, Any]:
    """
    把已翻译的文献铺成内容日历。

    - 先按相关度得分（score 降序）、其次按进入 PubMed 日期（entrez_date 降序）排序，
      保证质量最高的排在前面、最先被推送。
    - 默认每天 slots 1 篇（取 pipeline.daily_limit）；多个 slot 落到连续工作日。
    - start_date 缺省 = 今天（发布后即可被客户端取用）。发布者若想让日历从某个
      固定「上线日」起排、且重复发布时不前后错位，可用 build_feed(start_date=...) 传入。
    """
    if not articles:
        raise ValueError("没有可发布的文献，feed 为空")

    daily_limit = int(cfg.get("pipeline.daily_limit", 1) or 1) or 1

    ordered = sorted(
        articles,
        key=lambda a: (getattr(a, "score", 0) or 0, a.entrez_date or ""),
        reverse=True,
    )

    start = start_date or datetime.now()
    # 起点对齐到下一个工作日
    cursor = start
    if not _is_workday(cursor, cfg):
        cursor = next_workday(cursor - timedelta(days=1), cfg)

    calendar: Dict[str, List[str]] = {}
    articles_map: Dict[str, Dict[str, Any]] = {}

    idx = 0
    while idx < len(ordered):
        ds = cursor.strftime("%Y-%m-%d")
        slot: List[str] = []
        for _ in range(daily_limit):
            if idx >= len(ordered):
                break
            art = ordered[idx]
            slot.append(str(art.pmid))
            articles_map[str(art.pmid)] = art.to_dict()
            idx += 1
        if slot:
            calendar[ds] = slot
        # 下一天（推进到下一个工作日）
        cursor = next_workday(cursor, cfg)

    feed = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(cfg.get("content.timezone", "Asia/Shanghai")),
        "query": cfg.get("pubmed.query") or "(见 keyword_groups)",
        "total_articles": len(ordered),
        "calendar": calendar,
        "articles": articles_map,
    }
    log.info(
        "内容日历构建完成：共 %d 篇，从 %s 起按工作日推送，每日 %d 篇",
        len(ordered), min(calendar) if calendar else "-", daily_limit,
    )
    return feed


def save_feed(feed: Dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("内容日历已保存：%s（%d 篇）", p, feed.get("total_articles", 0))
    return p


# --------------------------------------------------------------------------
# 读取内容日历（URL 或本地文件）
# --------------------------------------------------------------------------
def load_feed(source: str, cfg=None, cache_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    从 URL 或本地路径加载 feed.json。
    - http(s) 开头 -> GET 请求
    - 其余 -> 视为本地路径（相对项目根或绝对）
    若配置了 cache_path，会把最近一次成功获取的结果缓存下来，离线时回退使用。
    """
    cache_path = Path(cache_path) if cache_path else None
    raw: Optional[str] = None

    if source.startswith("http://") or source.startswith("https://"):
        try:
            resp = requests.get(source, timeout=30)
            resp.raise_for_status()
            raw = resp.text
            log.info("已从远程拉取内容日历：%s", source)
        except Exception as exc:
            log.warning("远程 feed 拉取失败（%s），尝试使用本地缓存", exc)
    else:
        path = Path(source)
        if not path.is_absolute() and cfg is not None:
            path = cfg.base_dir / source
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            log.info("已从本地加载内容日历：%s", path)
        else:
            log.warning("本地 feed 文件不存在：%s", path)

    if raw is None and cache_path and cache_path.exists():
        raw = cache_path.read_text(encoding="utf-8")
        log.warning("回退使用缓存的内容日历：%s", cache_path)

    if raw is None:
        raise RuntimeError(f"无法加载内容日历（source={source}）")

    feed = json.loads(raw)
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(raw, encoding="utf-8")
        except Exception:
            pass
    return feed


# --------------------------------------------------------------------------
# 按日期取出当天要推送的文献
# --------------------------------------------------------------------------
def articles_for_date(feed: Dict[str, Any], date_str: str, db=None) -> List[Article]:
    """
    返回 date_str（YYYY-MM-DD）这一天应当推送的文献列表（Article 对象）。
    - 从 calendar 取该日 PMID 列表
    - 从 articles 还原为 Article（已含中文译文）
    - 若提供了 db，自动 upsert 进本地库（客户端无需 PubMed/翻译）
    """
    pmids = feed.get("calendar", {}).get(date_str, [])
    if not pmids:
        return []

    articles: List[Article] = []
    for pmid in pmids:
        data = feed.get("articles", {}).get(str(pmid))
        if not data:
            log.warning("日历中 PMID=%s 在 articles 里缺失，跳过", pmid)
            continue
        articles.append(Article(**{k: v for k, v in data.items() if k in _ARTICLE_FIELDS}))

    if db is not None and articles:
        db.upsert_articles(articles)
    return articles


# Article 字段白名单（避免 feed 里多出的键破坏构造）
_ARTICLE_FIELDS = {
    "pmid", "title", "abstract", "authors", "journal", "journal_abbr",
    "pub_date", "entrez_date", "doi", "publication_types", "keywords",
    "affiliation", "language", "title_zh", "abstract_zh", "translate_provider",
    "score", "source", "source_url",
    # 注意：pubmed_url / doi_url 是 @property 派生字段，不能作为构造参数，
    # 从 feed 反序列化时不需要传入，构造后会自动生成
}


def latest_pending_date(feed: Dict[str, Any], today_str: str, db) -> Optional[str]:
    """
    在「今天及之前」的所有日历日期里，找到最近一个『还有未推送文献』的日期。
    用于客户端补播：若某天关机漏推，下次运行会补上最近一次未推的内容，
    而因为日历是共享的，所有客户端在任意给定日期看到的是同一份内容。
    """
    dates = [d for d in feed.get("calendar", {}) if d <= today_str]
    dates.sort()
    for ds in reversed(dates):
        pmids = feed["calendar"][ds]
        pending = [p for p in pmids if not db.exists(p) or not db.is_pushed(p)]
        if pending:
            return ds
    return None
