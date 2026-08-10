"""
database.py —— SQLite 数据管理模块

三张表：
    articles           文献主表，以 PMID 做唯一约束，天然去重
    translation_cache  翻译缓存，同一段英文只花一次翻译成本
    push_log           推送流水，记录每次运行的结果，便于排查

对外接口：
    Database(path)
        .init_schema()
        .filter_new_pmids(pmids)      -> 过滤掉已入库的 PMID
        .save_articles(articles)      -> (新增数, 已存在数)
        .get_unpushed(limit)          -> list[Article]
        .mark_pushed(pmids)
        .get_translation(text)        -> str | None
        .save_translation(...)
        .log_push(...)
        .stats()                      -> dict
        .cleanup(keep_days)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from logger import get_logger
from search import Article

log = get_logger("database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pmid               TEXT    NOT NULL UNIQUE,
    doi                TEXT    DEFAULT '',
    title              TEXT    DEFAULT '',
    title_zh           TEXT    DEFAULT '',
    abstract           TEXT    DEFAULT '',
    abstract_zh        TEXT    DEFAULT '',
    authors            TEXT    DEFAULT '[]',
    journal            TEXT    DEFAULT '',
    journal_abbr       TEXT    DEFAULT '',
    pub_date           TEXT    DEFAULT '',
    entrez_date        TEXT    DEFAULT '',
    publication_types  TEXT    DEFAULT '[]',
    keywords           TEXT    DEFAULT '[]',
    affiliation        TEXT    DEFAULT '',
    language           TEXT    DEFAULT '',
    translate_provider TEXT    DEFAULT '',
    url                TEXT    DEFAULT '',
    source             TEXT    DEFAULT 'PubMed',
    pushed             INTEGER NOT NULL DEFAULT 0,
    pushed_at          TEXT    DEFAULT '',
    created_at         TEXT    DEFAULT '',
    updated_at         TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_articles_pushed     ON articles(pushed);
CREATE INDEX IF NOT EXISTS idx_articles_created    ON articles(created_at);
CREATE INDEX IF NOT EXISTS idx_articles_entrezdate ON articles(entrez_date);

CREATE TABLE IF NOT EXISTS translation_cache (
    hash        TEXT PRIMARY KEY,
    source_text TEXT,
    target_text TEXT,
    provider    TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS push_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT,
    channel     TEXT,
    item_count  INTEGER DEFAULT 0,
    status      TEXT,
    message     TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pushlog_date ON push_log(run_date);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class Database:
    """SQLite 封装。所有方法都是短连接，线程安全性由 sqlite 自身保证。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ---------------- 连接 ----------------
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
            if "source" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN source TEXT DEFAULT 'PubMed'")

    # ---------------- 去重 ----------------
    def exists(self, pmid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM articles WHERE pmid=?", (str(pmid),)).fetchone()
        return row is not None

    def filter_new_pmids(self, pmids: Sequence[str]) -> List[str]:
        """返回数据库中尚不存在的 PMID（核心去重逻辑）。"""
        pmids = [str(p) for p in pmids]
        if not pmids:
            return []
        existing: set[str] = set()
        with self._conn() as conn:
            # 分批 IN 查询，避免 SQL 变量数量超限
            for i in range(0, len(pmids), 500):
                chunk = pmids[i : i + 500]
                placeholder = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT pmid FROM articles WHERE pmid IN ({placeholder})", chunk
                ).fetchall()
                existing.update(r["pmid"] for r in rows)
        return [p for p in pmids if p not in existing]

    def filter_new_articles(self, articles: Sequence[Article]) -> List[Article]:
        """Remove duplicates by stable ID, DOI, and normalized title.

        External free sources use different identifiers for the same paper, so
        PMID-only deduplication is not sufficient once they are combined.
        """
        if not articles:
            return []

        def title_key(value: str) -> str:
            return "".join(ch for ch in value.lower() if ch.isalnum())[:180]

        new_ids = set(self.filter_new_pmids([a.pmid for a in articles]))
        with self._conn() as conn:
            rows = conn.execute("SELECT doi, title FROM articles").fetchall()
        existing_dois = {str(row["doi"] or "").strip().lower() for row in rows if row["doi"]}
        existing_titles = {title_key(str(row["title"] or "")) for row in rows if row["title"]}
        accepted: List[Article] = []
        seen_dois: set[str] = set()
        seen_titles: set[str] = set()
        for article in articles:
            doi = str(article.doi or "").strip().lower()
            title = title_key(article.title)
            if article.pmid not in new_ids or (doi and (doi in existing_dois or doi in seen_dois)) or (title and (title in existing_titles or title in seen_titles)):
                continue
            accepted.append(article)
            if doi:
                seen_dois.add(doi)
            if title:
                seen_titles.add(title)
        return accepted

    # ---------------- 写入 ----------------
    def save_articles(self, articles: Iterable[Article]) -> Tuple[int, int]:
        """
        批量入库。已存在的 PMID 不会重复插入（返回 skipped 计数）。
        返回 (新增数, 跳过数)
        """
        inserted = skipped = 0
        now = _now()
        with self._conn() as conn:
            for art in articles:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO articles (
                        pmid, doi, title, title_zh, abstract, abstract_zh, authors,
                        journal, journal_abbr, pub_date, entrez_date, publication_types,
                        keywords, affiliation, language, translate_provider, url, source,
                        pushed, pushed_at, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        art.pmid, art.doi, art.title, art.title_zh, art.abstract,
                        art.abstract_zh, json.dumps(art.authors, ensure_ascii=False),
                        art.journal, art.journal_abbr, art.pub_date, art.entrez_date,
                        json.dumps(art.publication_types, ensure_ascii=False),
                        json.dumps(art.keywords, ensure_ascii=False),
                        art.affiliation, art.language, art.translate_provider,
                        art.pubmed_url, art.source, 0, "", now, now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        log.info("入库完成：新增 %d 篇，重复跳过 %d 篇", inserted, skipped)
        return inserted, skipped

    def upsert_articles(self, articles: Iterable[Article]) -> Tuple[int, int]:
        """
        写入或更新文献。已存在的 PMID 用新数据覆盖（含中文译文），
        用于客户端按中央 feed 同步内容。返回 (新增数, 更新数)。
        """
        inserted = updated = 0
        now = _now()
        with self._conn() as conn:
            for art in articles:
                cur = conn.execute(
                    """
                    INSERT INTO articles (
                        pmid, doi, title, title_zh, abstract, abstract_zh, authors,
                        journal, journal_abbr, pub_date, entrez_date, publication_types,
                        keywords, affiliation, language, translate_provider, url, source,
                        pushed, pushed_at, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'',?,?)
                    ON CONFLICT(pmid) DO UPDATE SET
                        doi=excluded.doi, title=excluded.title, title_zh=excluded.title_zh,
                        abstract=excluded.abstract, abstract_zh=excluded.abstract_zh,
                        authors=excluded.authors, journal=excluded.journal,
                        journal_abbr=excluded.journal_abbr, pub_date=excluded.pub_date,
                        entrez_date=excluded.entrez_date,
                        publication_types=excluded.publication_types,
                        keywords=excluded.keywords, affiliation=excluded.affiliation,
                        language=excluded.language, translate_provider=excluded.translate_provider,
                        url=excluded.url, source=excluded.source, updated_at=excluded.updated_at
                    """,
                    (
                        art.pmid, art.doi, art.title, art.title_zh, art.abstract,
                        art.abstract_zh, json.dumps(art.authors, ensure_ascii=False),
                        art.journal, art.journal_abbr, art.pub_date, art.entrez_date,
                        json.dumps(art.publication_types, ensure_ascii=False),
                        json.dumps(art.keywords, ensure_ascii=False),
                        art.affiliation, art.language, art.translate_provider,
                        art.pubmed_url, art.source, now, now,
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    updated += 1
        log.info("upsert 完成：新增 %d 篇，更新 %d 篇", inserted, updated)
        return inserted, updated

    def is_pushed(self, pmid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT pushed FROM articles WHERE pmid=?", (str(pmid),)
            ).fetchone()
        return bool(row and row["pushed"])

    def update_translation(self, pmid: str, title_zh: str, abstract_zh: str, provider: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE articles
                   SET title_zh=?, abstract_zh=?, translate_provider=?, updated_at=?
                   WHERE pmid=?""",
                (title_zh, abstract_zh, provider, _now(), str(pmid)),
            )

    # ---------------- 查询 ----------------
    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> Article:
        def _loads(value: str) -> List[str]:
            try:
                data = json.loads(value or "[]")
                return data if isinstance(data, list) else []
            except (ValueError, TypeError):
                return []

        return Article(
            pmid=row["pmid"],
            title=row["title"] or "",
            abstract=row["abstract"] or "",
            authors=_loads(row["authors"]),
            journal=row["journal"] or "",
            journal_abbr=row["journal_abbr"] or "",
            pub_date=row["pub_date"] or "",
            entrez_date=row["entrez_date"] or "",
            doi=row["doi"] or "",
            publication_types=_loads(row["publication_types"]),
            keywords=_loads(row["keywords"]),
            affiliation=row["affiliation"] or "",
            language=row["language"] or "",
            title_zh=row["title_zh"] or "",
            abstract_zh=row["abstract_zh"] or "",
            translate_provider=row["translate_provider"] or "",
            source=row["source"] or "PubMed",
            source_url=row["url"] or "",
            pushed_at=row["pushed_at"] or "",
        )

    def get_unpushed(self, limit: int = 100) -> List[Article]:
        """取尚未推送的文献，按入库时间倒序。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE pushed=0 ORDER BY entrez_date DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_article(r) for r in rows]

    def get_pushed_history(self, limit: int = 300) -> List[Article]:
        """Return successfully delivered papers, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE pushed=1 ORDER BY pushed_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_article(row) for row in rows]

    def get_by_pmids(self, pmids: Sequence[str]) -> List[Article]:
        if not pmids:
            return []
        placeholder = ",".join("?" * len(pmids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM articles WHERE pmid IN ({placeholder})", [str(p) for p in pmids]
            ).fetchall()
        return [self._row_to_article(r) for r in rows]

    def recent(self, days: int = 7, limit: int = 200) -> List[Article]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE created_at>=? ORDER BY id DESC LIMIT ?",
                (since, int(limit)),
            ).fetchall()
        return [self._row_to_article(r) for r in rows]

    # ---------------- 推送状态 ----------------
    def mark_pushed(self, pmids: Sequence[str]) -> int:
        if not pmids:
            return 0
        now = _now()
        with self._conn() as conn:
            cur = conn.executemany(
                "UPDATE articles SET pushed=1, pushed_at=?, updated_at=? WHERE pmid=?",
                [(now, now, str(p)) for p in pmids],
            )
        log.info("已标记 %d 篇为已推送", len(pmids))
        return cur.rowcount if cur else len(pmids)

    def log_push(self, channel: str, item_count: int, status: str, message: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO push_log (run_date, channel, item_count, status, message, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (datetime.now().strftime("%Y-%m-%d"), channel, int(item_count),
                 status, message[:1000], _now()),
            )

    # ---------------- 翻译缓存 ----------------
    def get_translation(self, source_text: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT target_text FROM translation_cache WHERE hash=?", (_md5(source_text),)
            ).fetchone()
        return row["target_text"] if row else None

    def save_translation(self, source_text: str, target_text: str, provider: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO translation_cache
                   (hash, source_text, target_text, provider, created_at) VALUES (?,?,?,?,?)""",
                (_md5(source_text), source_text[:2000], target_text, provider, _now()),
            )

    # ---------------- 运维 ----------------
    def stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
            pushed = conn.execute("SELECT COUNT(*) c FROM articles WHERE pushed=1").fetchone()["c"]
            translated = conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE abstract_zh<>''"
            ).fetchone()["c"]
            cache = conn.execute("SELECT COUNT(*) c FROM translation_cache").fetchone()["c"]
            last = conn.execute(
                "SELECT run_date, channel, item_count, status FROM push_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "文献总数": total,
            "已推送": pushed,
            "待推送": total - pushed,
            "已翻译": translated,
            "翻译缓存条数": cache,
            "最近一次推送": dict(last) if last else None,
            "数据库文件": str(self.path),
            "文件大小": f"{self.path.stat().st_size / 1024:.1f} KB" if self.path.exists() else "0",
        }

    def cleanup(self, keep_days: int = 0) -> int:
        """删除超过 keep_days 天的历史记录；keep_days<=0 表示不清理。"""
        if keep_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d 00:00:00")
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM articles WHERE created_at < ?", (cutoff,))
            deleted = cur.rowcount
            conn.execute("DELETE FROM push_log WHERE created_at < ?", (cutoff,))
        if deleted:
            log.info("清理 %d 天前的历史文献 %d 条", keep_days, deleted)
        return deleted


def get_database(cfg) -> Database:
    """按配置创建 Database 实例。"""
    return Database(cfg.path("database.path", "data/literature.db"))


if __name__ == "__main__":  # 手动自检： python database.py
    from config import load_config

    db = get_database(load_config())
    for k, v in db.stats().items():
        print(f"{k}: {v}")
