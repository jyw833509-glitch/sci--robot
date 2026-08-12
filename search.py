"""
search.py —— PubMed 文献检索模块

基于 NCBI E-utilities（esearch + efetch）：
    esearch: 按检索式 + 日期范围拿到 PMID 列表
    efetch : 按 PMID 批量拉取完整 XML，解析出结构化字段

对外接口：
    build_query(cfg)                    -> str            生成 PubMed 检索式
    PubMedClient(cfg).search(...)       -> list[str]      返回 PMID 列表
    PubMedClient(cfg).fetch(pmids)      -> list[Article]  返回文献详情
    PubMedClient(cfg).search_recent()   -> list[Article]  一步到位：检索最近 N 天
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import requests

from logger import get_logger
from preferences import load_preferences, preference_terms

log = get_logger("search")

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# efetch 单次最多取多少条（NCBI 建议 <= 200）
FETCH_BATCH_SIZE = 100

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class Article:
    """一篇文献的结构化信息。"""

    pmid: str
    title: str = ""
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    journal: str = ""
    journal_abbr: str = ""
    pub_date: str = ""            # 出版日期 YYYY-MM-DD（可能只精确到年/月）
    entrez_date: str = ""         # 进入 PubMed 的日期 YYYY-MM-DD
    doi: str = ""
    publication_types: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    affiliation: str = ""
    language: str = ""

    # 翻译结果（由 translate.py 填充）
    title_zh: str = ""
    abstract_zh: str = ""
    translate_provider: str = ""

    # 相关度得分（本地打分，非 PubMed 提供）
    score: int = 0
    source: str = "PubMed"
    source_url: str = ""
    pushed_at: str = ""

    # ---------- 派生属性 ----------
    @property
    def pubmed_url(self) -> str:
        if self.source_url:
            return self.source_url
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    @property
    def doi_url(self) -> str:
        return f"https://doi.org/{self.doi}" if self.doi else ""

    @property
    def authors_str(self) -> str:
        """作者串：超过 6 人时显示前 3 位 + et al."""
        if not self.authors:
            return "—"
        if len(self.authors) > 6:
            return ", ".join(self.authors[:3]) + f", et al. (共 {len(self.authors)} 位作者)"
        return ", ".join(self.authors)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # @property 不会被 asdict 导出，但 feed/客户端需要这些链接
        d["pubmed_url"] = self.pubmed_url
        d["doi_url"] = self.doi_url
        return d


# --------------------------------------------------------------------------
# 检索式构造
# --------------------------------------------------------------------------
def build_query(cfg) -> str:
    """
    根据配置生成 PubMed 检索式。
    优先使用 pubmed.query；否则用 keyword_groups 拼装：组内 OR、组间 AND。
    """
    manual = (cfg.get("pubmed.query") or "").strip()
    extra: List[str] = [str(x).strip() for x in (cfg.get("pubmed.extra_filters") or []) if str(x).strip()]

    personal_terms = preference_terms()
    if manual:
        query = manual
    elif personal_terms:
        # A saved profile is the user's primary search scope.  The historical
        # default keyword groups remain the fallback for an empty profile.
        field_tag = (cfg.get("pubmed.field_tag") or "").strip()
        tag = f"[{field_tag}]" if field_tag else ""
        parts = [f'"{term}"{tag}' if " " in term else f"{term}{tag}" for term in personal_terms]
        query = "(" + " OR ".join(parts) + ")"
    else:
        groups: List[List[str]] = cfg.get("pubmed.keyword_groups") or []
        field_tag = (cfg.get("pubmed.field_tag") or "").strip()
        tag = f"[{field_tag}]" if field_tag else ""

        group_exprs: List[str] = []
        for group in groups:
            terms = [str(t).strip() for t in group if str(t).strip()]
            if not terms:
                continue
            parts = [f'"{t}"{tag}' if " " in t else f"{t}{tag}" for t in terms]
            group_exprs.append("(" + " OR ".join(parts) + ")")

        if not group_exprs:
            raise ValueError("未配置任何检索关键词（pubmed.keyword_groups 为空）")
        query = " AND ".join(group_exprs)

    if cfg.get("pubmed.require_abstract"):
        extra.append("hasabstract")

    for f in extra:
        query = f"({query}) AND {f}"

    return query


# --------------------------------------------------------------------------
# 相关度打分（本地降噪）
# --------------------------------------------------------------------------
def score_article(article: "Article", cfg) -> int:
    """
    给文献打相关度分：
        标题命中「抗体词」或「纯化词」  -> +title_weight
        摘要命中                        -> +abstract_weight
        命中下游工艺强相关词            -> 每个不同的词 +1（上限 bonus_cap）
        命中临床/诊断类噪音词           -> 每个 -2（上限 penalty_cap）
    """
    title = (article.title or "").lower()
    abstract = (article.abstract or "").lower()
    kw_text = " ".join(article.keywords).lower()
    body = f"{abstract} {kw_text}"

    groups: List[List[str]] = cfg.get("pubmed.keyword_groups") or []
    tw = int(cfg.get("relevance.title_weight", 3))
    aw = int(cfg.get("relevance.abstract_weight", 1))

    score = 0
    for group in groups:
        terms = [str(t).strip().lower() for t in group if str(t).strip()]
        if any(t in title for t in terms):
            score += tw
        elif any(t in body for t in terms):
            score += aw

    bonus_terms = [str(t).lower() for t in (cfg.get("relevance.bonus_terms") or [])]
    bonus_hits = sum(1 for t in bonus_terms if t in title or t in body)
    score += min(bonus_hits, int(cfg.get("relevance.bonus_cap", 5)))

    penalty_terms = [str(t).lower() for t in (cfg.get("relevance.penalty_terms") or [])]
    penalty_hits = sum(1 for t in penalty_terms if t in title or t in body)
    score -= min(penalty_hits * 2, int(cfg.get("relevance.penalty_cap", 4)))

    # Local preferences complement the shared configuration.  They only affect
    # local PubMed ranking; centrally supplied feed content stays identical for
    # every subscriber.
    preferences = load_preferences()
    preferred_terms = [term.lower() for term in preference_terms(preferences)]
    preferred_hits = sum(1 for term in preferred_terms if term in title or term in body)
    score += min(preferred_hits * 4, 16)

    excluded_terms = [str(term).strip().lower() for term in (preferences.get("exclude_terms") or [])]
    excluded_hits = sum(1 for term in excluded_terms if term and (term in title or term in body))
    score -= min(excluded_hits * 4, 12)

    return score


# --------------------------------------------------------------------------
# XML 解析工具
# --------------------------------------------------------------------------
def _text(elem: Optional[ET.Element]) -> str:
    """取元素的全部文本（含子标签，如 <i>、<sup>），并压缩空白。"""
    if elem is None:
        return ""
    return " ".join("".join(elem.itertext()).split())


def _parse_pubdate(article_el: ET.Element) -> str:
    """
    解析出版日期，优先级：ArticleDate（电子版）> Journal/PubDate。
    返回 YYYY-MM-DD / YYYY-MM / YYYY，解析失败返回空串。
    """
    art_date = article_el.find("./ArticleDate")
    if art_date is not None:
        y = _text(art_date.find("Year"))
        m = _text(art_date.find("Month"))
        d = _text(art_date.find("Day"))
        if y:
            return _join_ymd(y, m, d)

    pubdate = article_el.find("./Journal/JournalIssue/PubDate")
    if pubdate is not None:
        y = _text(pubdate.find("Year"))
        m = _text(pubdate.find("Month"))
        d = _text(pubdate.find("Day"))
        if y:
            return _join_ymd(y, m, d)
        medline = _text(pubdate.find("MedlineDate"))  # 例："2024 Nov-Dec"
        if medline:
            tokens = medline.replace("-", " ").split()
            year = next((t for t in tokens if t.isdigit() and len(t) == 4), "")
            month = next((t for t in tokens if t[:3].lower() in _MONTH_MAP), "")
            if year:
                return _join_ymd(year, month, "")
    return ""


def _join_ymd(y: str, m: str, d: str) -> str:
    y = y.strip()
    if not y:
        return ""
    mm = ""
    if m:
        m = m.strip()
        if m.isdigit():
            mm = f"{int(m):02d}"
        elif m[:3].lower() in _MONTH_MAP:
            mm = f"{_MONTH_MAP[m[:3].lower()]:02d}"
    dd = f"{int(d):02d}" if d and d.strip().isdigit() else ""
    if mm and dd:
        return f"{y}-{mm}-{dd}"
    if mm:
        return f"{y}-{mm}"
    return y


def _parse_entrez_date(pubmed_data: Optional[ET.Element]) -> str:
    """从 PubmedData/History 中取 PubStatus='entrez' 或 'pubmed' 的日期。"""
    if pubmed_data is None:
        return ""
    for status in ("entrez", "pubmed", "medline"):
        node = pubmed_data.find(f"./History/PubMedPubDate[@PubStatus='{status}']")
        if node is not None:
            y, m, d = _text(node.find("Year")), _text(node.find("Month")), _text(node.find("Day"))
            if y:
                return _join_ymd(y, m, d)
    return ""


def _parse_abstract(article_el: ET.Element) -> str:
    """
    拼接结构化摘要。带 Label 的分段会加上「LABEL: 」前缀。
    """
    nodes = article_el.findall("./Abstract/AbstractText")
    if not nodes:
        return ""
    parts: List[str] = []
    for node in nodes:
        content = _text(node)
        if not content:
            continue
        label = (node.get("Label") or node.get("NlmCategory") or "").strip()
        if label and label.upper() != "UNLABELLED":
            parts.append(f"{label.upper()}: {content}")
        else:
            parts.append(content)
    return "\n".join(parts)


def _parse_authors(article_el: ET.Element) -> List[str]:
    authors: List[str] = []
    for node in article_el.findall("./AuthorList/Author"):
        collective = _text(node.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = _text(node.find("LastName"))
        fore = _text(node.find("ForeName")) or _text(node.find("Initials"))
        full = " ".join(x for x in (fore, last) if x).strip()
        if full:
            authors.append(full)
    return authors


def _parse_doi(medline_el: ET.Element, pubmed_data: Optional[ET.Element]) -> str:
    node = medline_el.find("./Article/ELocationID[@EIdType='doi']")
    if node is not None and _text(node):
        return _text(node)
    if pubmed_data is not None:
        node = pubmed_data.find("./ArticleIdList/ArticleId[@IdType='doi']")
        if node is not None and _text(node):
            return _text(node)
    return ""


def parse_pubmed_xml(xml_text: str) -> List[Article]:
    """把 efetch 返回的 XML 解析为 Article 列表。"""
    articles: List[Article] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.error("PubMed XML 解析失败：%s", exc)
        return articles

    for entry in root.findall(".//PubmedArticle"):
        medline = entry.find("./MedlineCitation")
        if medline is None:
            continue
        article_el = medline.find("./Article")
        if article_el is None:
            continue
        pubmed_data = entry.find("./PubmedData")

        pmid = _text(medline.find("./PMID"))
        if not pmid:
            continue

        art = Article(
            pmid=pmid,
            title=_text(article_el.find("./ArticleTitle")),
            title_zh=_text(article_el.find("./VernacularTitle")),
            abstract=_parse_abstract(article_el),
            authors=_parse_authors(article_el),
            journal=_text(article_el.find("./Journal/Title")),
            journal_abbr=_text(article_el.find("./Journal/ISOAbbreviation")),
            pub_date=_parse_pubdate(article_el),
            entrez_date=_parse_entrez_date(pubmed_data),
            doi=_parse_doi(medline, pubmed_data),
            publication_types=[
                _text(n) for n in article_el.findall("./PublicationTypeList/PublicationType") if _text(n)
            ],
            keywords=[_text(n) for n in medline.findall("./KeywordList/Keyword") if _text(n)],
            affiliation=_text(
                article_el.find("./AuthorList/Author/AffiliationInfo/Affiliation")
            ),
            language=_text(article_el.find("./Language")),
        )
        articles.append(art)

    return articles


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class PubMedClient:
    """PubMed E-utilities 客户端，内置限速与重试。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_key = (cfg.get("pubmed.api_key") or "").strip()
        self.email = (cfg.get("pubmed.email") or "").strip()
        self.tool = cfg.get("pubmed.tool") or "scirobot"
        self.timeout = int(cfg.get("pubmed.timeout", 30))
        self.max_retries = int(cfg.get("pubmed.max_retries", 3))
        self.backoff = float(cfg.get("pubmed.retry_backoff", 2.0))
        # NCBI 限速：无 Key 3 req/s，有 Key 10 req/s。这里留足余量。
        self._min_interval = 0.12 if self.api_key else 0.40
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": f"{self.tool} (python-requests; contact: {self.email or 'n/a'})"}
        )

    # ---------- 内部工具 ----------
    def _common_params(self) -> Dict[str, str]:
        params = {"db": "pubmed", "tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _request(self, url: str, params: Dict[str, Any]) -> Optional[requests.Response]:
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = self.backoff ** attempt
                    log.warning(
                        "PubMed 返回 %s，%.1fs 后重试（%d/%d）",
                        resp.status_code, wait, attempt, self.max_retries,
                    )
                    time.sleep(wait)
                    continue
                log.error("PubMed 请求失败 HTTP %s：%s", resp.status_code, resp.text[:200])
                return None
            except requests.RequestException as exc:
                wait = self.backoff ** attempt
                log.warning(
                    "PubMed 网络异常（%s），%.1fs 后重试（%d/%d）",
                    exc.__class__.__name__, wait, attempt, self.max_retries,
                )
                time.sleep(wait)
        log.error("PubMed 请求重试 %d 次后仍然失败：%s", self.max_retries, url)
        return None

    # ---------- 检索 ----------
    def search(
        self,
        query: str,
        *,
        mindate: Optional[str] = None,
        maxdate: Optional[str] = None,
        reldate: Optional[int] = None,
        datetype: str = "edat",
        retmax: int = 100,
    ) -> List[str]:
        """执行 esearch，返回 PMID 列表（按时间倒序）。"""
        params: Dict[str, Any] = self._common_params()
        params.update(
            {
                "term": query,
                "retmode": "json",
                "retmax": int(retmax),
                "sort": "date",
                "datetype": datetype,
            }
        )
        if mindate and maxdate:
            params["mindate"] = mindate
            params["maxdate"] = maxdate
        elif reldate:
            params["reldate"] = int(reldate)

        resp = self._request(ESEARCH_URL, params)
        if resp is None:
            return []

        try:
            data = resp.json()
        except ValueError:
            log.error("esearch 返回非 JSON 内容：%s", resp.text[:200])
            return []

        result = data.get("esearchresult", {})
        if "ERROR" in result:
            log.error("PubMed 检索式错误：%s", result["ERROR"])
            return []

        idlist = result.get("idlist", []) or []
        total = result.get("count", "0")
        log.info("PubMed 命中 %s 条，本次取回 %d 个 PMID", total, len(idlist))
        return [str(i) for i in idlist]

    # ---------- 详情 ----------
    def fetch(self, pmids: Iterable[str]) -> List[Article]:
        """按 PMID 批量拉取文献详情。"""
        pmids = [str(p).strip() for p in pmids if str(p).strip()]
        if not pmids:
            return []

        articles: List[Article] = []
        for i in range(0, len(pmids), FETCH_BATCH_SIZE):
            batch = pmids[i : i + FETCH_BATCH_SIZE]
            params = self._common_params()
            params.update({"id": ",".join(batch), "retmode": "xml", "rettype": "abstract"})
            resp = self._request(EFETCH_URL, params)
            if resp is None:
                continue
            parsed = parse_pubmed_xml(resp.text)
            articles.extend(parsed)
            log.info("efetch 批次 %d/%d：解析出 %d 篇",
                     i // FETCH_BATCH_SIZE + 1,
                     (len(pmids) - 1) // FETCH_BATCH_SIZE + 1,
                     len(parsed))
        return articles

    # ---------- 过滤 ----------
    def _filter(self, articles: List[Article]) -> List[Article]:
        require_abstract = bool(self.cfg.get("pubmed.require_abstract", True))
        excluded = {
            str(x).strip().lower()
            for x in (self.cfg.get("pubmed.exclude_publication_types") or [])
        }

        use_relevance = bool(self.cfg.get("relevance.enabled", True))
        min_score = int(self.cfg.get("relevance.min_score", 0))

        kept: List[Article] = []
        dropped_score = 0
        for art in articles:
            if require_abstract and not art.abstract.strip():
                log.debug("跳过无摘要文献 PMID=%s", art.pmid)
                continue
            if excluded and {t.lower() for t in art.publication_types} & excluded:
                log.debug("跳过被排除类型 PMID=%s %s", art.pmid, art.publication_types)
                continue
            art.score = score_article(art, self.cfg)
            if use_relevance and min_score > 0 and art.score < min_score:
                dropped_score += 1
                log.debug("相关度不足（%d<%d）丢弃 PMID=%s：%s",
                          art.score, min_score, art.pmid, art.title[:60])
                continue
            kept.append(art)

        if dropped_score:
            log.info("相关度过滤丢弃 %d 篇（min_score=%d）", dropped_score, min_score)
        kept.sort(key=lambda a: (a.score, a.entrez_date), reverse=True)
        return kept

    # ---------- 一步到位 ----------
    def search_recent(
        self,
        *,
        days: Optional[int] = None,
        max_results: Optional[int] = None,
    ) -> List[Article]:
        """检索最近 N 天新增的文献并返回详情列表。"""
        query = build_query(self.cfg)
        days = int(days if days is not None else self.cfg.get("pubmed.lookback_days", 3))
        retmax = int(max_results if max_results is not None else self.cfg.get("pubmed.max_results", 100))
        datetype = self.cfg.get("pubmed.date_type", "edat")

        today = date.today()
        mindate = (today - timedelta(days=max(days - 1, 0))).strftime("%Y/%m/%d")
        maxdate = today.strftime("%Y/%m/%d")

        log.info("检索式：%s", query)
        log.info("日期范围：%s ~ %s（datetype=%s，回溯 %d 天）", mindate, maxdate, datetype, days)

        pmids = self.search(
            query, mindate=mindate, maxdate=maxdate, datetype=datetype, retmax=retmax
        )
        if not pmids:
            return []

        articles = self.fetch(pmids)
        kept = self._filter(articles)
        log.info("抓取 %d 篇，过滤后保留 %d 篇", len(articles), len(kept))
        return kept

    def search_by_range(
        self, start: str, end: str, *, max_results: int = 500
    ) -> List[Article]:
        """按任意日期区间检索（用于历史回填），日期格式 YYYY-MM-DD 或 YYYY/MM/DD。"""
        query = build_query(self.cfg)
        fmt = lambda s: s.replace("-", "/")  # noqa: E731
        pmids = self.search(
            query,
            mindate=fmt(start),
            maxdate=fmt(end),
            datetype=self.cfg.get("pubmed.date_type", "edat"),
            retmax=max_results,
        )
        return self._filter(self.fetch(pmids)) if pmids else []


if __name__ == "__main__":  # 手动自检： python search.py
    from config import load_config

    conf = load_config()
    client = PubMedClient(conf)
    print("检索式：", build_query(conf))
    items = client.search_recent(days=7, max_results=20)
    print(f"\n共 {len(items)} 篇：\n")
    for a in items:
        print(f"[{a.pmid}] (score={a.score}) {a.title}")
        print(f"   期刊：{a.journal_abbr or a.journal} | 日期：{a.pub_date} | DOI：{a.doi or '—'}")
        print(f"   作者：{a.authors_str}")
        print(f"   摘要：{a.abstract[:120]}...\n")
