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
MAX_LOOKBACK_DAYS = 365 * 5
SOURCE_QUALITY = {"PubMed": 5, "Europe PMC": 4, "Crossref": 2, "OpenAlex": 2, "bioRxiv": 1}


def _clean(value: object) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(str(value or ""))).split())


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", title.lower())[:180]


def _normalise_search_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", (value or "").lower()).split())


def _term_matches(text: str, term: str) -> bool:
    """Phrase-aware matching with conservative plural/word-form tolerance."""
    haystack, needle = _normalise_search_text(text), _normalise_search_text(term)
    if not needle:
        return False
    if f" {needle} " in f" {haystack} ":
        return True
    wanted, available = needle.split(), haystack.split()
    if not wanted or not available:
        return False
    for start in range(len(available) - len(wanted) + 1):
        window = available[start:start + len(wanted)]
        if all(actual == expected or (len(expected) >= 5 and actual.startswith(expected))
               for actual, expected in zip(window, wanted)):
            return True
    return False


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
        return self._search(self._terms(), days, use_preferences=True)

    def search_keywords(self, keywords: str, days: int = 30, *, strict: bool = True) -> list[Article]:
        """Run an on-demand multi-source search without changing saved preferences."""
        terms = [part.strip() for part in keywords.replace("，", ",").replace("；", ",").split(",") if part.strip()]
        if not terms:
            return []
        return self._search(terms, max(1, min(MAX_LOOKBACK_DAYS, int(days))), use_preferences=False, strict=strict)

    def search_chinese(self, chinese_keywords: str, english_keywords: str, days: int = 30, *, strict: bool = True) -> list[Article]:
        """Search only papers whose original publication language is Chinese."""
        zh_terms = [part.strip() for part in chinese_keywords.replace("，", ",").replace("；", ",").split(",") if part.strip()]
        en_terms = [part.strip() for part in english_keywords.replace("，", ",").replace("；", ",").split(",") if part.strip()]
        if not zh_terms:
            return []
        days = max(1, min(MAX_LOOKBACK_DAYS, int(days)))
        since = (date.today() - timedelta(days=days)).isoformat()
        results: list[Article] = []
        enabled = {str(name).strip().lower() for name in (self.cfg.get("search_sources.enabled") or [])}
        providers = [
            ("PubMed", "pubmed", lambda: self._pubmed_chinese(en_terms, days, strict)),
            ("Europe PMC", "europe_pmc", lambda: self._europe_pmc_chinese(en_terms, since, strict)),
            ("Crossref", "crossref", lambda: self._crossref(zh_terms, since)),
            ("OpenAlex", "openalex", lambda: self._openalex_chinese(zh_terms, since)),
        ]
        for name, provider_key, fetch in providers:
            if provider_key not in enabled:
                continue
            try:
                articles = [article for article in fetch() if self._is_chinese_article(article)]
                results.extend(articles)
                log.info("%s 中文文献检索到 %d 篇", name, len(articles))
            except Exception as exc:
                log.warning("%s 中文文献检索失败，已跳过：%s", name, exc)
        deduplicated = self._deduplicate(results)
        matched = [article for article in deduplicated
                   if self._is_valid_article(article) and self._matches_bilingual(article, zh_terms, en_terms, strict)]
        for article in matched:
            article.score = self._query_score(article, [*zh_terms, *en_terms])
        matched.sort(key=lambda article: (article.score, article.pub_date), reverse=True)
        return matched[: self.limit]

    def _search(self, terms: list[str], days: int, *, use_preferences: bool, strict: bool = False) -> list[Article]:
        since = (date.today() - timedelta(days=days)).isoformat()
        results: list[Article] = []

        # Each provider is isolated: a temporary outage must not stop the day’s push.
        enabled = {str(name).strip().lower() for name in (self.cfg.get("search_sources.enabled") or [])}
        providers = [
            ("PubMed", lambda: PubMedClient(self.cfg).search_recent(days=days) if use_preferences else self._pubmed(terms, days, strict)),
            ("Europe PMC", lambda: self._europe_pmc(terms, since, strict)),
            ("Crossref", lambda: self._crossref(terms, since)),
            ("OpenAlex", lambda: self._openalex(terms, since)),
            ("bioRxiv", lambda: self._biorxiv(terms, since, strict)),
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
        deduplicated = [article for article in self._deduplicate(results) if self._is_valid_article(article)]
        if not use_preferences:
            deduplicated = [article for article in deduplicated if self._matches_terms(article, terms, strict)]
        if not use_preferences:
            for article in deduplicated:
                article.score = self._query_score(article, terms)
            deduplicated.sort(key=lambda article: (article.score, article.pub_date), reverse=True)
            return deduplicated
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

    @staticmethod
    def _matches_all(article: Article, terms: list[str]) -> bool:
        text = " ".join([article.title, article.abstract, " ".join(article.keywords)])
        return all(_term_matches(text, term) for term in terms)

    @staticmethod
    def _matches_terms(article: Article, terms: list[str], strict: bool) -> bool:
        text = " ".join([article.title, article.abstract, " ".join(article.keywords)])
        matches = [_term_matches(text, term) for term in terms]
        if strict:
            return all(matches)
        # Broad mode still requires a topic hit in the title/keywords, or at
        # least two independent terms in the searchable record.
        title_keywords = " ".join([article.title, " ".join(article.keywords)])
        return any(_term_matches(title_keywords, term) for term in terms) or sum(matches) >= min(2, len(terms))

    @staticmethod
    def _is_valid_article(article: Article) -> bool:
        text = f"{article.title} {' '.join(article.publication_types)}".lower()
        if any(marker in text for marker in ("retracted publication", "retraction of", "withdrawn")):
            return False
        if article.source == "Crossref" and article.publication_types:
            return any(kind in {"journal-article", "proceedings-article"} for kind in article.publication_types)
        return bool(article.title.strip())

    @staticmethod
    def _query_score(article: Article, terms: list[str]) -> int:
        unique_terms = list(dict.fromkeys(term for term in terms if _normalise_search_text(term)))
        title_hits = sum(_term_matches(article.title_zh or article.title, term) for term in unique_terms)
        keyword_hits = sum(_term_matches(" ".join(article.keywords), term) for term in unique_terms)
        abstract_hits = sum(_term_matches(article.abstract_zh or article.abstract, term) for term in unique_terms)
        complete_title = bool(unique_terms) and title_hits == len(unique_terms)
        return (title_hits * 8 + keyword_hits * 5 + abstract_hits * 2
                + (8 if complete_title else 0) + SOURCE_QUALITY.get(article.source, 0))

    @staticmethod
    def _is_chinese_article(article: Article) -> bool:
        language = article.language.strip().lower().replace("_", "-")
        if language in {"chi", "zho", "zh", "zh-cn", "chinese"} or language.startswith("zh-"):
            return True
        return bool(re.search(r"[\u3400-\u9fff]", article.title_zh or article.title))

    @staticmethod
    def _matches_bilingual(article: Article, zh_terms: list[str], en_terms: list[str], strict: bool) -> bool:
        text = " ".join([article.title, article.title_zh, article.abstract, article.abstract_zh, " ".join(article.keywords)])
        term_groups = list(zip(zh_terms, en_terms)) if len(zh_terms) == len(en_terms) else [(term, "") for term in zh_terms]
        matches = [_term_matches(text, zh) or bool(en and _term_matches(text, en)) for zh, en in term_groups]
        return all(matches) if strict else any(matches)

    def _pubmed(self, terms: list[str], days: int, strict: bool) -> list[Article]:
        client = PubMedClient(self.cfg)
        tag = (self.cfg.get("pubmed.field_tag") or "Title/Abstract").strip()
        suffix = f"[{tag}]" if tag else ""
        query = "(" + (" AND " if strict else " OR ").join(f'"{term}"{suffix}' for term in terms) + ")"
        today = date.today()
        pmids = client.search(query, mindate=(today - timedelta(days=days)).strftime("%Y/%m/%d"), maxdate=today.strftime("%Y/%m/%d"), retmax=self.limit)
        return client.fetch(pmids)

    def _pubmed_chinese(self, terms: list[str], days: int, strict: bool) -> list[Article]:
        if not terms:
            return []
        client = PubMedClient(self.cfg)
        tag = (self.cfg.get("pubmed.field_tag") or "Title/Abstract").strip()
        suffix = f"[{tag}]" if tag else ""
        topic = (" AND " if strict else " OR ").join(f'"{term}"{suffix}' for term in terms)
        query = f"({topic}) AND chinese[lang]"
        today = date.today()
        pmids = client.search(query, mindate=(today - timedelta(days=days)).strftime("%Y/%m/%d"), maxdate=today.strftime("%Y/%m/%d"), retmax=self.limit)
        return client.fetch(pmids)

    def _europe_pmc(self, terms: list[str], since: str, strict: bool = False) -> list[Article]:
        query = (" AND " if strict else " OR ").join(f'"{term}"' for term in terms)
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

    def _europe_pmc_chinese(self, terms: list[str], since: str, strict: bool = False) -> list[Article]:
        if not terms:
            return []
        query = (" AND " if strict else " OR ").join(f'"{term}"' for term in terms)
        data = self._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": f"({query}) AND LANG:chi AND FIRST_PDATE:[{since} TO *]", "format": "json",
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
                keywords=[_clean(x) for x in item.get("keywordList", {}).get("keyword", [])], language=str(item.get("language") or "chi"),
                source="Europe PMC", source_url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}"))
        return out

    def _crossref(self, terms: list[str], since: str) -> list[Article]:
        data = self._get("https://api.crossref.org/works", {
            "query.bibliographic": " ".join(terms), "filter": f"from-pub-date:{since},type:journal-article",
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
                publication_types=[str(item.get("type") or "")], language=str(item.get("language") or ""),
                source="Crossref", source_url=f"https://doi.org/{doi}"))
        return out

    def _openalex(self, terms: list[str], since: str) -> list[Article]:
        data = self._get("https://api.openalex.org/works", {
            "search": " ".join(terms), "filter": f"from_publication_date:{since},type:article",
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
                pub_date=str(item.get("publication_date") or ""), language=str(item.get("language") or ""),
                source="OpenAlex", source_url=str(item.get("id") or "")))
        return out

    def _openalex_chinese(self, terms: list[str], since: str) -> list[Article]:
        data = self._get("https://api.openalex.org/works", {
            "search": " ".join(terms), "filter": f"from_publication_date:{since},language:zh",
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
                pub_date=str(item.get("publication_date") or ""), language=str(item.get("language") or "zh"),
                source="OpenAlex", source_url=str(item.get("id") or "")))
        return out

    def _biorxiv(self, terms: list[str], since: str, strict: bool = False) -> list[Article]:
        data = self._get(f"https://api.biorxiv.org/details/biorxiv/{since}/{date.today().isoformat()}/0", {})
        out = []
        lower_terms = [term.lower() for term in terms]
        for item in data.get("collection", [])[: self.limit * 3]:
            title, abstract = _clean(item.get("title")), _clean(item.get("abstract"))
            matched = all(term in f"{title} {abstract}".lower() for term in lower_terms) if strict else any(term in f"{title} {abstract}".lower() for term in lower_terms)
            if lower_terms and not matched:
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
