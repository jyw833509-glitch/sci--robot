"""
notifier.py —— 推送模块

支持渠道：
    email    SMTP 邮件（HTML 正文 + 纯文本备选，可附带 Markdown 附件）
    webhook  企业微信 / 钉钉 / 飞书 群机器人（推送标题清单，正文看邮件）

对外接口：
    Notifier(cfg).send(report)      -> dict {渠道: 是否成功}
    Notifier(cfg).send_test()       -> dict  发送测试消息，用于验证配置
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from logger import get_logger
from report import Report

SCRIPT_DIR = Path(__file__).resolve().parent

log = get_logger("notifier")


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------
class EmailNotifier:
    name = "email"

    def __init__(self, cfg):
        self.cfg = cfg
        e = cfg.section("notifier").get("email", {})
        self.enabled = bool(e.get("enabled", True))
        self.host = str(e.get("smtp_host", "")).strip()
        self.port = int(e.get("smtp_port", 465))
        self.use_ssl = bool(e.get("use_ssl", True))
        self.use_starttls = bool(e.get("use_starttls", False))
        self.username = str(e.get("username", "")).strip()
        self.password = str(e.get("password", "")).strip()
        self.from_addr = str(e.get("from_addr", "") or self.username).strip()
        self.sender_name = str(e.get("sender_name", "文献机器人"))
        self.to: List[str] = [str(x).strip() for x in (e.get("to") or []) if str(x).strip()]
        self.cc: List[str] = [str(x).strip() for x in (e.get("cc") or []) if str(x).strip()]
        self.attach_markdown = bool(e.get("attach_markdown", False))
        self.send_when_empty = bool(e.get("send_when_empty", False))
        self.timeout = int(e.get("timeout", 30))

    # ---------- 配置检查 ----------
    def ready(self) -> bool:
        missing = []
        if not self.host:
            missing.append("smtp_host")
        if not self.username:
            missing.append("username")
        if not self.password:
            missing.append("password")
        if not self.to:
            missing.append("to")
        if missing:
            log.error("邮件配置不完整，缺少：%s", "、".join(missing))
            return False
        return True

    # ---------- 组装 ----------
    def _build_message(self, subject: str, html_body: str, text_body: str,
                       attachment: Optional[tuple[str, str]] = None):
        outer = MIMEMultipart("mixed") if attachment else None
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text_body or " ", "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))

        msg = outer if outer is not None else alt
        if outer is not None:
            outer.attach(alt)
            filename, content = attachment  # type: ignore[misc]
            part = MIMEApplication(content.encode("utf-8"), _subtype="octet-stream")
            part.add_header(
                "Content-Disposition", "attachment",
                filename=("utf-8", "", filename),
            )
            outer.attach(part)

        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(self.sender_name, "utf-8")), self.from_addr))
        msg["To"] = ", ".join(self.to)
        if self.cc:
            msg["Cc"] = ", ".join(self.cc)
        msg["Date"] = formatdate(localtime=True)
        return msg

    # ---------- 发送 ----------
    def _send_raw(self, msg) -> bool:
        recipients = self.to + self.cc
        try:
            if self.use_ssl:
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.use_starttls:
                    server.starttls(context=ssl.create_default_context())
            with server:
                server.login(self.username, self.password)
                server.sendmail(self.from_addr, recipients, msg.as_string())
            log.info("邮件发送成功 -> %s", ", ".join(recipients))
            return True
        except smtplib.SMTPAuthenticationError as exc:
            log.error("SMTP 认证失败（QQ/163 请确认密码填的是「授权码」而非登录密码）：%s", exc)
        except smtplib.SMTPException as exc:
            log.error("SMTP 发送失败：%s", exc)
        except OSError as exc:
            log.error("SMTP 网络连接失败（检查 host/port/防火墙）：%s", exc)
        return False

    def send(self, report: Report) -> bool:
        if not self.enabled:
            log.info("邮件推送未启用，跳过")
            return False
        if report.is_empty and not self.send_when_empty:
            log.info("本期无新文献且 send_when_empty=false，跳过邮件推送")
            return False
        if not self.ready():
            return False

        attachment = None
        if self.attach_markdown:
            attachment = (f"{report.report_date}-文献日报.md", report.markdown)

        msg = self._build_message(report.subject, report.html, report.text, attachment)
        return self._send_raw(msg)

    def send_test(self) -> bool:
        if not self.ready():
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html_body = f"""
        <div style="font-family:'PingFang SC','Microsoft YaHei',sans-serif;max-width:600px;margin:0 auto;">
          <div style="background:linear-gradient(135deg,#155e75,#0891b2);color:#fff;padding:22px;border-radius:12px;">
            <div style="font-size:20px;font-weight:700;">SMTP 配置测试成功</div>
            <div style="font-size:13px;margin-top:8px;opacity:.9;">抗体纯化文献自动订阅推送机器人</div>
          </div>
          <div style="padding:18px 4px;color:#334155;font-size:14px;line-height:1.9;">
            如果你收到这封邮件，说明邮件推送链路已经打通。<br>
            发信服务器：{self.host}:{self.port}<br>
            发件人：{self.from_addr}<br>
            收件人：{', '.join(self.to)}<br>
            测试时间：{now}
          </div>
        </div>"""
        msg = self._build_message(
            "[文献机器人] SMTP 配置测试", html_body,
            f"SMTP 配置测试成功，时间 {now}", None,
        )
        return self._send_raw(msg)


# --------------------------------------------------------------------------
# Webhook（企业微信 / 钉钉 / 飞书）
# --------------------------------------------------------------------------
class WebhookNotifier:
    name = "webhook"

    def __init__(self, cfg):
        self.cfg = cfg
        w = cfg.section("notifier").get("webhook", {})
        self.enabled = bool(w.get("enabled", False))
        self.type = str(w.get("type", "wecom")).lower()
        self.url = str(w.get("url", "")).strip()
        self.secret = str(w.get("secret", "")).strip()
        self.max_items = int(w.get("max_items", 10))

    def ready(self) -> bool:
        if not self.url:
            log.error("Webhook 未配置 url")
            return False
        return True

    # ---------- 正文 ----------
    def _build_markdown(self, report: Report) -> str:
        title = self.cfg.get("report.title", "抗体纯化文献日报")
        if report.is_empty:
            return f"**{title} · {report.report_date}**\n\n今日没有检索到符合条件的新文献。"

        lines = [f"**{title} · {report.report_date}**", f"本期 {report.count} 篇新文献", ""]
        for i, pmid in enumerate(report.pmids[: self.max_items], 1):
            lines.append(f"{i}. [PMID {pmid}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)")
        if report.count > self.max_items:
            lines.append(f"...另有 {report.count - self.max_items} 篇，详见邮件日报")
        return "\n".join(lines)

    # ---------- 钉钉加签 ----------
    def _dingtalk_url(self) -> str:
        if not self.secret:
            return self.url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}timestamp={timestamp}&sign={sign}"

    def send(self, report: Report) -> bool:
        if not self.enabled:
            return False
        if not self.ready():
            return False

        content = self._build_markdown(report)
        title = f"{self.cfg.get('report.title', '文献日报')} {report.report_date}"

        url = self.url
        if self.type == "wecom":
            payload: Dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": content}}
        elif self.type == "dingtalk":
            url = self._dingtalk_url()
            payload = {"msgtype": "markdown", "markdown": {"title": title, "text": content}}
        elif self.type == "feishu":
            payload = {"msg_type": "text", "content": {"text": content}}
        else:
            log.error("未知 Webhook 类型：%s", self.type)
            return False

        try:
            resp = requests.post(url, json=payload, timeout=20)
            data = resp.json() if resp.content else {}
            code = data.get("errcode", data.get("code", 0))
            if resp.status_code == 200 and str(code) in ("0", "None", ""):
                log.info("Webhook（%s）推送成功", self.type)
                return True
            log.error("Webhook（%s）推送失败：HTTP %s %s", self.type, resp.status_code, str(data)[:200])
        except requests.RequestException as exc:
            log.error("Webhook 网络异常：%s", exc)
        return False

    def send_test(self) -> bool:
        fake = Report(
            report_date=datetime.now().strftime("%Y-%m-%d"),
            subject="测试", html="", markdown="", text="",
            count=0, pmids=[],
        )
        return self.send(fake)


# --------------------------------------------------------------------------
# 桌面弹窗（tkinter 独立窗口 / Windows Toast）
# --------------------------------------------------------------------------
def _article_to_dict(a) -> Dict[str, Any]:
    """把 Article 转换成弹窗所需的精简 dict（兼容 dataclass 字段）。"""
    return {
        "pmid": getattr(a, "pmid", ""),
        "title": getattr(a, "title", "") or "",
        "title_zh": getattr(a, "title_zh", "") or "",
        "authors": getattr(a, "authors_str", "") or "",
        "journal": (getattr(a, "journal_abbr", "") or getattr(a, "journal", "")) or "",
        "pub_date": (getattr(a, "pub_date", "") or getattr(a, "entrez_date", "")) or "",
        "doi": getattr(a, "doi", "") or "",
        "pubmed_url": getattr(a, "pubmed_url", "") or "",
        "doi_url": getattr(a, "doi_url", "") or "",
        "abstract": getattr(a, "abstract", "") or "",
        "abstract_zh": getattr(a, "abstract_zh", "") or "",
        "keywords": getattr(a, "keywords", []) or [],
    }


def _launch_desktop_window(payload: Dict[str, Any]) -> bool:
    """把 payload 写入临时 JSON，以子进程唤起 desktop_notify.py 弹窗（不阻塞主流程）。"""
    script = SCRIPT_DIR / "desktop_notify.py"
    if not script.exists():
        log.error("找不到 desktop_notify.py，无法弹出窗口")
        return False
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="litbot_popup_")
        with os.fdopen(fd, "wb") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        proc = subprocess.Popen(
            [sys.executable, str(script), tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("已唤起桌面弹窗进程（pid=%s）", proc.pid)
        return True
    except Exception as exc:
        log.exception("唤起桌面弹窗失败：%s", exc)
        return False


def _launch_toast(payload: Dict[str, Any], timeout: int = 10) -> bool:
    """Windows 系统通知（Toast）。未安装 win10toast 时回退到窗口弹窗。"""
    try:
        from win10toast import ToastNotifier
    except ImportError:
        log.warning("未安装 win10toast，回退到窗口弹窗模式")
        return _launch_desktop_window(payload)
    arts = payload.get("articles", [])
    if arts:
        text = (arts[0].get("title_zh") or arts[0].get("title") or "")[:200]
    else:
        text = "今日没有检索到符合条件的新文献"
    try:
        ToastNotifier().show_toast(payload.get("title", "文献日报"), text, duration=timeout or 10)
        return True
    except Exception as exc:
        log.error("系统通知弹窗失败（%s），回退窗口弹窗", exc)
        return _launch_desktop_window(payload)


class DesktopNotifier:
    name = "desktop"

    def __init__(self, cfg):
        self.cfg = cfg
        d = cfg.section("notifier").get("desktop", {})
        self.enabled = bool(d.get("enabled", True))
        self.mode = str(d.get("mode", "window")).lower()
        self.timeout = int(d.get("timeout", 0) or 0)
        self.title = cfg.get("report.title", "抗体纯化文献日报")

    def ready(self) -> bool:
        return True

    def _payload(self, articles) -> Dict[str, Any]:
        return {
            "title": self.title,
            "articles": [_article_to_dict(a) for a in (articles or [])],
        }

    def send(self, report: "Report") -> bool:
        if not self.enabled:
            log.info("桌面弹窗未启用，跳过")
            return False
        articles = getattr(report, "articles", None) or []
        if not articles:
            log.info("本期无文献，跳过桌面弹窗")
            return False
        payload = self._payload(articles)
        if self.mode == "toast":
            return _launch_toast(payload, self.timeout)
        return _launch_desktop_window(payload)

    def send_test(self) -> bool:
        sample = {
            "pmid": "00000000",
            "title": "A novel Protein A affinity chromatography platform for high-titer monoclonal antibody purification",
            "title_zh": "一种面向高滴度单克隆抗体纯化的新型 Protein A 亲和层析平台",
            "authors": "Zhang Y, Li M, Wang H, et al.",
            "journal": "J Chromatogr B",
            "pub_date": "2026-07-15",
            "doi": "10.1016/j.jchromb.2026.123456",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/00000000/",
            "doi_url": "https://doi.org/10.1016/j.jchromb.2026.123456",
            "abstract": "This study developed a new Protein A resin with improved dynamic binding capacity and reduced ligand leakage for monoclonal antibody downstream processing.",
            "abstract_zh": "本研究开发了一种新型 Protein A 层析介质，动态结合载量显著提升、配基脱落降低，适用于单克隆抗体下游工艺。",
            "keywords": ["Protein A", "affinity chromatography", "mAb", "downstream processing"],
        }
        payload = {"title": self.title, "articles": [sample]}
        if self.mode == "toast":
            return _launch_toast(payload, self.timeout)
        return _launch_desktop_window(payload)


# --------------------------------------------------------------------------
# 总调度
# --------------------------------------------------------------------------
class Notifier:
    """按 notifier.channels 分发到各推送渠道。"""

    def __init__(self, cfg, db=None):
        self.cfg = cfg
        self.db = db
        self.channels = [str(c).lower() for c in (cfg.get("notifier.channels") or [])]
        self._impl = {
            "email": EmailNotifier(cfg),
            "webhook": WebhookNotifier(cfg),
            "desktop": DesktopNotifier(cfg),
        }

    def send(self, report: Report) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        if not self.channels:
            log.warning("未启用任何推送渠道（notifier.channels 为空）")
            return results

        for channel in self.channels:
            impl = self._impl.get(channel)
            if impl is None:
                log.warning("未知推送渠道：%s", channel)
                continue
            try:
                ok = impl.send(report)
            except Exception as exc:  # pragma: no cover
                log.exception("推送渠道 %s 异常：%s", channel, exc)
                ok = False
            results[channel] = ok
            if self.db is not None:
                self.db.log_push(
                    channel=channel,
                    item_count=report.count,
                    status="success" if ok else "failed",
                    message=report.subject,
                )
        return results

    def send_test(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for channel in self.channels or ["email"]:
            impl = self._impl.get(channel)
            if impl is None:
                continue
            try:
                results[channel] = impl.send_test()
            except Exception as exc:  # pragma: no cover
                log.exception("测试推送 %s 异常：%s", channel, exc)
                results[channel] = False
        return results


if __name__ == "__main__":  # 手动自检： python notifier.py
    from config import load_config

    conf = load_config()
    print("发送测试消息...")
    for ch, ok in Notifier(conf).send_test().items():
        print(f"  {ch}: {'成功' if ok else '失败（详见日志）'}")
