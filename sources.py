"""Free multi-source literature retrieval with graceful source-level fallback."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Iterable

import requests

from logger import get_logger
from preferences import preference_terms
from search import Article, PubMedClient, score_article

log = get_logger("sources")


def _clean(value: object) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(str(value or ""))).split())


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:180]


def _doi(value: object) -> str:
    return str(value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def _crossref_date(item: dict) -> str:
    """Use the most specific publisher date available from a Crossref record."""
    for key in ("published-online", "published-print", "published", "issued"):
        parts = (item.get(key) or {}).get("date-parts", [[]])[0]
        if parts:
            return "-".join(str(value).zfill(2) if index else str(value) for index, value in enumerate(parts))
    return ""


class MultiSourceClient:
    """Retrieve recent papers from PubMed plus four complementary free APIs."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = int(cfg.get("pubmed.timeout", 30) or 30)
        self.limit = min(int(cfg.get("pubmed.max_results", 100) or 100), 100)

    def _terms(self) -> list[str]:
        terms = preference_terms()
        if terms:
            return terms
        return [str(t) for group in (self.cfg.get("pubmed.keyword_groups") or []) for t in group]

    def _get(self, url: str, params: dict) -> dict:
        response = requests.get(url, params=params, timeout=self.timeout, headers={"User-Agent": "SciRobot/1.0"})
        response.raise_for_status()
        return response.json()

    def search_recent(self, days: int | None = None) -> list[Article]:
        days = int(days or self.cfg.get("pubmed.lookback_days", 7) or 7)
        since = (date.today() - timedelta(days=days)).isoformat()
        terms = self._terms()
        results: list[Article] = []

        # Each provider is isolated: a temporary outage must not stop the day’s push.
        enabled = {str(name).strip().lower() for name in (self.cfg.get("search_sources.enabled") or [])}
        providers = [
            ("PubMed", lambda: PubMedClient(self.cfg).search_recent(days=days)),
            ("Europe PMC", lambda: self._europe_pmc(terms, since)),
            ("Crossref", lambda: self._crossref(terms, since)),
            ("OpenAlex", lambda: self._openalex(terms, since)),
            ("bioRxiv", lambda: self._biorxiv(terms, since)),
        ]
        for name, fetch in providers:
            provider_key = name.lower().replace(" ", "_")
            if provider_key not in enabled:
                continue
            try:
                articles = fetch()
                results.extend(articles)
                log.info("%s 检索到 %d 篇", name, len(articles))
            except Exception as exc:
                log.warning("%s 检索失败，已跳过：%s", name, exc)
        deduplicated = self._deduplicate(results)
        if not self.cfg.get("relevance.enabled", True):
            return deduplicated
        min_score = int(self.cfg.get("relevance.min_score", 0) or 0)
        kept: list[Article] = []
        for article in deduplicated:
            article.score = score_article(article, self.cfg)
            if article.score >= min_score:
                kept.append(article)
        kept.sort(key=lambda article: (article.score, article.pub_date), reverse=True)
        log.info("多源合并去重后 %d 篇，相关性筛选保留 %d 篇", len(deduplicated), len(kept))
        return kept

    def _europe_pmc(self, terms: list[str], since: str) -> list[Article]:
        query = " OR ".join(f'"{term}"' for term in terms)
        data = self._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": f"({query}) AND FIRST_PDATE:[{since} TO *]", "format": "json",
            "resultType": "core", "pageSize": self.limit,
        })
        out = []
        for item in data.get("resultList", {}).get("result", []):
            doi = _doi(item.get("doi"))
            pmid = str(item.get("pmid") or (f"doi:{doi}" if doi else f"eupmc:{item.get('id', '')}"))
            if not pmid:
                continue
            out.append(Article(pmid=pmid, doi=doi, title=_clean(item.get("title")),
                abstract=_clean(item.get("abstractText")), authors=_clean(item.get("authorString")).split(", "),
                journal=_clean(item.get("journalTitle")), pub_date=str(item.get("firstPublicationDate") or ""),
                keywords=[_clean(x) for x in item.get("keywordList", {}).get("keyword", [])], language=str(item.get("language") or ""),
                source="Europe PMC", source_url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}"))
        return out

    def _crossref(self, terms: list[str], since: str) -> list[Article]:
        data = self._get("https://api.crossref.org/works", {
            "query": " ".join(terms), "filter": f"from-pub-date:{since}",
            "sort": "published", "order": "desc", "rows": self.limit,
        })
        out = []
        for item in data.get("message", {}).get("items", []):
            doi = _doi(item.get("DOI"))
            title = _clean((item.get("title") or [""])[0])
            if not doi or not title:
                continue
            published = _crossref_date(item)
            # Crossref can return recently deposited metadata for old articles;
            # retain only records whose publication date is inside the window.
            if not published or published < since or published > date.today().isoformat():
                continue
            authors = [" ".join(filter(None, [a.get("given", ""), a.get("family", "")])) for a in item.get("author", [])]
            out.append(Article(pmid=f"doi:{doi}", doi=doi, title=title, abstract=_clean(item.get("abstract")),
                authors=authors, journal=_clean((item.get("container-title") or [""])[0]), pub_date=published,
                publication_types=[str(item.get("type") or "")], source="Crossref", source_url=f"https://doi.org/{doi}"))
        return out

    def _openalex(self, terms: list[str], since: str) -> list[Article]:
        data = self._get("https://api.openalex.org/works", {
            "search": " ".join(terms), "filter": f"from_publication_date:{since}",
            "per-page": self.limit, "sort": "publication_date:desc",
        })
        out = []
        for item in data.get("results", []):
            doi = _doi(item.get("doi"))
            openalex_id = str(item.get("id") or "").rsplit("/", 1)[-1]
            title = _clean(item.get("title"))
            if not title or not (doi or openalex_id):
                continue
            inverted = item.get("abstract_inverted_index") or {}
            words = sorted(((pos, word) for word, positions in inverted.items() for pos in positions))
            abstract = " ".join(word for _, word in words)
            authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])]
            venue = item.get("primary_location", {}).get("source", {}) or {}
            out.append(Article(pmid=f"doi:{doi}" if doi else f"openalex:{openalex_id}", doi=doi, title=title,
                abstract=abstract, authors=authors, journal=_clean(venue.get("display_name")),
                pub_date=str(item.get("publication_date") or ""), source="OpenAlex", source_url=str(item.get("id") or "")))
        return out

    def _biorxiv(self, terms: list[str], since: str) -> list[Article]:
        data = self._get(f"https://api.biorxiv.org/details/biorxiv/{since}/{date.today().isoformat()}/0", {})
        out = []
        lower_terms = [term.lower() for term in terms]
        for item in data.get("collection", [])[: self.limit * 3]:
            title, abstract = _clean(item.get("title")), _clean(item.get("abstract"))
            if lower_terms and not any(term in f"{title} {abstract}".lower() for term in lower_terms):
                continue
            doi = _doi(item.get("doi"))
            if not doi:
                continue
            out.append(Article(pmid=f"doi:{doi}", doi=doi, title=title, abstract=abstract,
                authors=_clean(item.get("authors")).split("; "), journal="bioRxiv", pub_date=str(item.get("date") or ""),
                source="bioRxiv", source_url=f"https://www.biorxiv.org/content/{doi}v1"))
        return out

    @staticmethod
    def _deduplicate(articles: Iterable[Article]) -> list[Article]:
        result: list[Article] = []
        ids: set[str] = set()
        dois: set[str] = set()
        titles: set[str] = set()
        for article in articles:
            key_id, key_doi, key_title = article.pmid.lower(), _doi(article.doi), _title_key(article.title)
            if key_id in ids or (key_doi and key_doi in dois) or (key_title and key_title in titles):
                continue
            ids.add(key_id)
            if key_doi:
                dois.add(key_doi)
            if key_title:
                titles.add(key_title)
            result.append(article)
        return result
