"""
report.py —— 日报生成模块

把 Article 列表渲染成三种格式：
    html      邮件正文（内联样式，兼容 QQ / 163 / Outlook / Gmail）
    markdown  归档 / 附件 / 知识库
    text      纯文本（邮件的 plain 备选正文、Webhook 摘要）

对外接口：
    build_report(articles, cfg, report_date=None) -> Report
    save_report(report, cfg)                      -> list[Path]
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence

from logger import get_logger
from search import Article

log = get_logger("report")

# 配色（浅色主题，邮件客户端友好）
C_PRIMARY = "#0e7490"
C_PRIMARY_DARK = "#155e75"
C_ACCENT = "#0891b2"
C_BG = "#f4f6f8"
C_CARD = "#ffffff"
C_BORDER = "#e2e8f0"
C_TEXT = "#1f2937"
C_MUTED = "#64748b"
C_LIGHT = "#94a3b8"


@dataclass
class Report:
    """一期日报。"""

    report_date: str
    subject: str
    html: str
    markdown: str
    text: str
    count: int
    pmids: List[str] = field(default_factory=list)
    # 原文 Article 列表（由 build_report 填充），供桌面弹窗等渠道取用完整字段
    articles: List[Article] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.count == 0


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _md_escape(text: str) -> str:
    """转义 Markdown 里会破坏排版的字符。"""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _journal_of(a: Article) -> str:
    return a.journal_abbr or a.journal or "—"


def _date_of(a: Article) -> str:
    return a.pub_date or a.entrez_date or "—"


def _title_zh_or_en(a: Article) -> str:
    return a.title_zh or a.title or f"PMID {a.pmid}"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
def _render_article_html(index: int, a: Article, cfg) -> str:
    show_en = bool(cfg.get("report.show_english_abstract", True))
    fold_en = bool(cfg.get("report.fold_english_abstract", True))

    links = [
        f'<a href="{_esc(a.pubmed_url)}" style="color:{C_ACCENT};text-decoration:none;">PubMed</a>'
    ]
    if a.doi:
        links.append(
            f'<a href="{_esc(a.doi_url)}" style="color:{C_ACCENT};text-decoration:none;">DOI: {_esc(a.doi)}</a>'
        )

    abstract_zh_html = ""
    if a.abstract_zh:
        paragraphs = "".join(
            f'<p style="margin:0 0 8px;line-height:1.75;color:{C_TEXT};font-size:14px;">{_esc(p)}</p>'
            for p in a.abstract_zh.split("\n") if p.strip()
        )
        abstract_zh_html = f"""
        <div style="margin-top:12px;padding:12px 14px;background:#f0fdfa;border-left:3px solid {C_ACCENT};border-radius:0 6px 6px 0;">
          <div style="font-size:12px;font-weight:700;color:{C_PRIMARY};margin-bottom:6px;letter-spacing:.5px;">中文摘要</div>
          {paragraphs}
        </div>"""
    else:
        abstract_zh_html = f"""
        <div style="margin-top:12px;padding:10px 14px;background:#fff7ed;border-left:3px solid #fb923c;border-radius:0 6px 6px 0;font-size:13px;color:#9a3412;">
          未获取到中文翻译，请查看下方英文原文
        </div>"""

    abstract_en_html = ""
    if show_en and a.abstract:
        en_body = "".join(
            f'<p style="margin:0 0 8px;line-height:1.7;color:{C_MUTED};font-size:13px;">{_esc(p)}</p>'
            for p in a.abstract.split("\n") if p.strip()
        )
        if fold_en:
            abstract_en_html = f"""
        <details style="margin-top:10px;">
          <summary style="cursor:pointer;font-size:12px;color:{C_LIGHT};outline:none;">展开英文原文摘要</summary>
          <div style="margin-top:8px;padding-left:10px;border-left:2px solid {C_BORDER};">{en_body}</div>
        </details>"""
        else:
            abstract_en_html = f"""
        <div style="margin-top:10px;padding-left:10px;border-left:2px solid {C_BORDER};">
          <div style="font-size:12px;font-weight:700;color:{C_LIGHT};margin-bottom:6px;">ABSTRACT</div>
          {en_body}
        </div>"""

    en_title_html = ""
    if a.title_zh and a.title:
        en_title_html = (
            f'<div style="font-size:13px;color:{C_MUTED};margin:4px 0 0;line-height:1.5;">{_esc(a.title)}</div>'
        )

    keywords_html = ""
    if a.keywords:
        chips = "".join(
            f'<span style="display:inline-block;background:#ecfeff;color:{C_PRIMARY};'
            f'font-size:11px;padding:2px 8px;border-radius:10px;margin:0 6px 4px 0;">{_esc(k)}</span>'
            for k in a.keywords[:8]
        )
        keywords_html = f'<div style="margin-top:10px;">{chips}</div>'

    return f"""
    <div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:10px;padding:18px 20px;margin-bottom:14px;">
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <td style="vertical-align:top;width:30px;">
            <div style="width:24px;height:24px;line-height:24px;text-align:center;background:{C_PRIMARY};
                        color:#fff;border-radius:6px;font-size:12px;font-weight:700;">{index}</div>
          </td>
          <td style="vertical-align:top;padding-left:10px;">
            <div style="font-size:16px;font-weight:700;color:{C_TEXT};line-height:1.5;">{_esc(_title_zh_or_en(a))}</div>
            {en_title_html}
          </td>
        </tr>
      </table>

      <div style="margin-top:10px;font-size:12px;color:{C_MUTED};line-height:1.8;">
        <div><span style="color:{C_LIGHT};">期刊：</span>{_esc(_journal_of(a))}
             &nbsp;·&nbsp;<span style="color:{C_LIGHT};">发表：</span>{_esc(_date_of(a))}
             &nbsp;·&nbsp;<span style="color:{C_LIGHT};">PMID：</span>{_esc(a.pmid)}</div>
        <div><span style="color:{C_LIGHT};">作者：</span>{_esc(a.authors_str)}</div>
        <div>{" &nbsp;|&nbsp; ".join(links)}</div>
      </div>

      {abstract_zh_html}
      {abstract_en_html}
      {keywords_html}
    </div>"""


def _render_html(articles: Sequence[Article], cfg, report_date: str, total_found: int) -> str:
    title = cfg.get("report.title", "抗体纯化文献日报")
    cards = "".join(_render_article_html(i, a, cfg) for i, a in enumerate(articles, 1))

    if not articles:
        cards = f"""
      <div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:10px;padding:40px 20px;text-align:center;">
        <div style="font-size:15px;color:{C_MUTED};">今日没有检索到符合条件的新文献</div>
        <div style="font-size:12px;color:{C_LIGHT};margin-top:8px;">机器人运行正常，明天继续为你盯着 PubMed</div>
      </div>"""

    providers = sorted({a.translate_provider for a in articles if a.translate_provider})
    provider_note = f"　翻译引擎：{_esc('/'.join(providers))}" if providers else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · {_esc(report_date)}</title>
</head>
<body style="margin:0;padding:0;background:{C_BG};">
<div style="background:{C_BG};padding:24px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:720px;margin:0 auto;">

    <div style="background:linear-gradient(135deg,{C_PRIMARY_DARK} 0%,{C_ACCENT} 100%);
                border-radius:12px;padding:26px 24px;color:#fff;">
      <div style="font-size:12px;letter-spacing:2px;opacity:.85;">ANTIBODY PURIFICATION LITERATURE</div>
      <div style="font-size:24px;font-weight:700;margin-top:6px;">{_esc(title)}</div>
      <div style="font-size:13px;margin-top:10px;opacity:.9;">
        {_esc(report_date)}　·　本期 {len(articles)} 篇　·　检索命中 {total_found} 篇
      </div>
    </div>

    <div style="height:16px;"></div>
    {cards}

    <div style="text-align:center;color:{C_LIGHT};font-size:11px;line-height:1.9;padding:18px 10px 4px;">
      数据来源：PubMed (NCBI E-utilities){provider_note}<br>
      由「抗体纯化文献自动订阅推送机器人」生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
      如需调整关键词、推送时间或收件人，请修改 config.yaml
    </div>

  </div>
</div>
</body>
</html>"""


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def _render_markdown(articles: Sequence[Article], cfg, report_date: str, total_found: int) -> str:
    title = cfg.get("report.title", "抗体纯化文献日报")
    show_en = bool(cfg.get("report.show_english_abstract", True))

    lines: List[str] = [
        f"# {title}",
        "",
        f"> 日期：{report_date}　|　本期 {len(articles)} 篇　|　检索命中 {total_found} 篇　|　来源：PubMed",
        "",
    ]

    if not articles:
        lines += ["今日没有检索到符合条件的新文献。", ""]
        return "\n".join(lines)

    lines += ["## 本期目录", ""]
    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. [{_md_escape(_title_zh_or_en(a))}](#{i}) —— *{_md_escape(_journal_of(a))}*")
    lines += ["", "---", ""]

    for i, a in enumerate(articles, 1):
        lines += [
            f'<a id="{i}"></a>',
            "",
            f"## {i}. {_title_zh_or_en(a)}",
            "",
        ]
        if a.title_zh and a.title:
            lines += [f"*{a.title}*", ""]
        lines += [
            "| 项目 | 内容 |",
            "| --- | --- |",
            f"| 期刊 | {_md_escape(_journal_of(a))} |",
            f"| 发表时间 | {_md_escape(_date_of(a))} |",
            f"| 作者 | {_md_escape(a.authors_str)} |",
            f"| DOI | {f'[{a.doi}]({a.doi_url})' if a.doi else '—'} |",
            f"| PMID | [{a.pmid}]({a.pubmed_url}) |",
            "",
        ]
        if a.abstract_zh:
            lines += ["**中文摘要**", ""]
            lines += [p for p in a.abstract_zh.split("\n") if p.strip()]
            lines.append("")
        if show_en and a.abstract:
            lines += ["<details>", "<summary>英文原文摘要</summary>", ""]
            lines += [p for p in a.abstract.split("\n") if p.strip()]
            lines += ["", "</details>", ""]
        if a.keywords:
            lines += [f"`{'` `'.join(a.keywords[:10])}`", ""]
        lines += ["---", ""]

    lines += [
        f"*本报告由「抗体纯化文献自动订阅推送机器人」生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 纯文本
# --------------------------------------------------------------------------
def _render_text(articles: Sequence[Article], cfg, report_date: str, total_found: int) -> str:
    title = cfg.get("report.title", "抗体纯化文献日报")
    lines = [f"{title}  {report_date}", f"本期 {len(articles)} 篇 / 检索命中 {total_found} 篇", "=" * 46, ""]
    if not articles:
        lines.append("今日没有检索到符合条件的新文献。")
        return "\n".join(lines)
    for i, a in enumerate(articles, 1):
        lines += [
            f"{i}. {_title_zh_or_en(a)}",
            f"   {_journal_of(a)} | {_date_of(a)} | PMID {a.pmid}",
            f"   {a.pubmed_url}",
        ]
        summary = (a.abstract_zh or a.abstract or "").replace("\n", " ")
        if summary:
            lines.append(f"   摘要：{summary[:180]}{'...' if len(summary) > 180 else ''}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_report(
    articles: Sequence[Article],
    cfg,
    report_date: Optional[str] = None,
    total_found: Optional[int] = None,
) -> Report:
    """把文献列表渲染成一期日报。"""
    report_date = report_date or date.today().strftime("%Y-%m-%d")
    max_items = int(cfg.get("report.max_items", 30))

    ordered = sorted(articles, key=lambda a: (a.score, a.entrez_date or a.pub_date), reverse=True)
    shown = ordered[:max_items] if max_items > 0 else ordered
    total = total_found if total_found is not None else len(articles)

    title = cfg.get("report.title", "抗体纯化文献日报")
    subject = (
        f"[{title}] {report_date}　{len(shown)} 篇新文献"
        if shown else f"[{title}] {report_date}　今日无新文献"
    )

    report = Report(
        report_date=report_date,
        subject=subject,
        html=_render_html(shown, cfg, report_date, total),
        markdown=_render_markdown(shown, cfg, report_date, total),
        text=_render_text(shown, cfg, report_date, total),
        count=len(shown),
        pmids=[a.pmid for a in shown],
        articles=list(shown),
    )
    log.info("日报生成完成：%s，共 %d 篇", report_date, report.count)
    return report


def save_report(report: Report, cfg) -> List[Path]:
    """把日报写入 reports/ 目录，返回生成的文件路径列表。"""
    if not cfg.get("report.save_to_disk", True):
        return []

    out_dir = cfg.path("app.report_dir", "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = [str(f).lower() for f in (cfg.get("report.formats") or ["html", "markdown"])]

    saved: List[Path] = []
    if "html" in formats:
        p = out_dir / f"{report.report_date}.html"
        p.write_text(report.html, encoding="utf-8")
        saved.append(p)
    if "markdown" in formats or "md" in formats:
        p = out_dir / f"{report.report_date}.md"
        p.write_text(report.markdown, encoding="utf-8")
        saved.append(p)
    if "text" in formats or "txt" in formats:
        p = out_dir / f"{report.report_date}.txt"
        p.write_text(report.text, encoding="utf-8")
        saved.append(p)

    for p in saved:
        log.info("日报已保存：%s", p)
    return saved
