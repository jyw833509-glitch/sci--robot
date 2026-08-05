"""
translate.py —— 摘要翻译模块

设计要点：
    1. 多后端可插拔：llm / baidu / youdao / google / mymemory / none
    2. 按 providers 顺序降级：前一个不可用或失败，自动切下一个
    3. 数据库翻译缓存：同一段英文只翻译一次，省钱又提速
    4. 长文本自动分块：适配各家接口的长度限制，再拼回完整译文

对外接口：
    Translator(cfg, db=None)
        .translate_text(text)          -> (译文, 后端名)
        .translate_article(article)    -> Article（原地填充 title_zh / abstract_zh）
        .translate_articles(articles)  -> list[Article]
        .available_providers()         -> list[str]
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from logger import get_logger
from search import Article

log = get_logger("translate")


# --------------------------------------------------------------------------
# 文本分块工具
# --------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+")


def split_text(text: str, max_len: int) -> List[str]:
    """按句子边界把长文本切成不超过 max_len 的块（尽量不破句）。"""
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []

    chunks: List[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_len:
            chunks.append(paragraph)
            continue
        buf = ""
        for sentence in _SENT_SPLIT.split(paragraph):
            if not sentence:
                continue
            if len(sentence) > max_len:  # 单句超长：硬切
                if buf:
                    chunks.append(buf)
                    buf = ""
                for i in range(0, len(sentence), max_len):
                    chunks.append(sentence[i : i + max_len])
                continue
            if len(buf) + len(sentence) + 1 <= max_len:
                buf = f"{buf} {sentence}".strip()
            else:
                chunks.append(buf)
                buf = sentence
        if buf:
            chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------
# 后端基类
# --------------------------------------------------------------------------
class BaseTranslator(ABC):
    name = "base"
    # 单次请求最大字符数
    chunk_size = 2000

    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        proxy = (cfg.get("translate.proxy") or "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    @abstractmethod
    def available(self) -> bool:
        """配置是否完整、后端是否可用。"""

    @abstractmethod
    def _translate_chunk(self, text: str) -> str:
        """翻译单个文本块，失败抛异常。"""

    def translate(self, text: str) -> str:
        """翻译任意长度文本（内部自动分块）。"""
        text = (text or "").strip()
        if not text:
            return ""
        chunks = split_text(text, self.chunk_size)
        results = [self._translate_chunk(c) for c in chunks]
        return "\n".join(r.strip() for r in results if r and r.strip())


# --------------------------------------------------------------------------
# 后端 A：大模型（OpenAI 兼容接口）—— 推荐，术语准确度最高
# --------------------------------------------------------------------------
class LLMTranslator(BaseTranslator):
    name = "llm"
    chunk_size = 6000

    def __init__(self, cfg):
        super().__init__(cfg)
        self.base_url = (cfg.get("translate.llm.base_url") or "").rstrip("/")
        self.api_key = (cfg.get("translate.llm.api_key") or "").strip()
        self.model = cfg.get("translate.llm.model") or "gpt-4o-mini"
        self.temperature = float(cfg.get("translate.llm.temperature", 0.2))
        self.timeout = int(cfg.get("translate.llm.timeout", 120))
        self.system_prompt = cfg.get("translate.llm.system_prompt") or (
            "你是专业的生物制药领域学术翻译，请把英文摘要忠实翻译成简体中文，只输出译文。"
        )

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _translate_chunk(self, text: str) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
            "stream": False,
        }
        resp = self.session.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LLM 返回结构异常：{str(data)[:200]}") from exc
        return str(content).strip()


# --------------------------------------------------------------------------
# 后端 B：百度翻译开放平台
# --------------------------------------------------------------------------
class BaiduTranslator(BaseTranslator):
    name = "baidu"
    chunk_size = 1800  # 官方限制 6000 字节，中英混排保守取值
    endpoint = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.app_id = (cfg.get("translate.baidu.app_id") or "").strip()
        self.app_key = (cfg.get("translate.baidu.app_key") or "").strip()

    def available(self) -> bool:
        return bool(self.app_id and self.app_key)

    def _translate_chunk(self, text: str) -> str:
        salt = str(random.randint(10000, 99999999))
        sign = hashlib.md5(
            (self.app_id + text + salt + self.app_key).encode("utf-8")
        ).hexdigest()
        resp = self.session.post(
            self.endpoint,
            data={
                "q": text, "from": "en", "to": "zh",
                "appid": self.app_id, "salt": salt, "sign": sign,
            },
            timeout=30,
        )
        data = resp.json()
        if "error_code" in data:
            raise RuntimeError(f"百度翻译错误 {data.get('error_code')}: {data.get('error_msg')}")
        return "\n".join(item.get("dst", "") for item in data.get("trans_result", []))


# --------------------------------------------------------------------------
# 后端 C：有道智云
# --------------------------------------------------------------------------
class YoudaoTranslator(BaseTranslator):
    name = "youdao"
    chunk_size = 1800
    endpoint = "https://openapi.youdao.com/api"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.app_key = (cfg.get("translate.youdao.app_key") or "").strip()
        self.app_secret = (cfg.get("translate.youdao.app_secret") or "").strip()

    def available(self) -> bool:
        return bool(self.app_key and self.app_secret)

    @staticmethod
    def _truncate(q: str) -> str:
        size = len(q)
        return q if size <= 20 else q[:10] + str(size) + q[-10:]

    def _translate_chunk(self, text: str) -> str:
        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))
        raw = self.app_key + self._truncate(text) + salt + curtime + self.app_secret
        sign = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        resp = self.session.post(
            self.endpoint,
            data={
                "q": text, "from": "en", "to": "zh-CHS",
                "appKey": self.app_key, "salt": salt, "sign": sign,
                "signType": "v3", "curtime": curtime,
            },
            timeout=30,
        )
        data = resp.json()
        if str(data.get("errorCode")) != "0":
            raise RuntimeError(f"有道翻译错误 errorCode={data.get('errorCode')}")
        return "\n".join(data.get("translation", []))


# --------------------------------------------------------------------------
# 后端 D：Google 免费接口（国内需代理）
# --------------------------------------------------------------------------
class GoogleTranslator(BaseTranslator):
    name = "google"
    chunk_size = 1200  # GET 请求，URL 长度受限

    def __init__(self, cfg):
        super().__init__(cfg)
        self.endpoint = (
            cfg.get("translate.google.endpoint")
            or "https://translate.googleapis.com/translate_a/single"
        )
        self.timeout = int(cfg.get("translate.google.timeout", 20))

    def available(self) -> bool:
        return True  # 无需 Key，但可能被墙，失败后自动降级

    def _translate_chunk(self, text: str) -> str:
        resp = self.session.get(
            self.endpoint,
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Google HTTP {resp.status_code}")
        data = resp.json()
        if not data or not isinstance(data, list) or not data[0]:
            raise RuntimeError("Google 返回结构异常")
        return "".join(seg[0] for seg in data[0] if seg and seg[0])


# --------------------------------------------------------------------------
# 后端 E：MyMemory 免费接口（无需 Key，兜底）
# --------------------------------------------------------------------------
class MyMemoryTranslator(BaseTranslator):
    name = "mymemory"
    chunk_size = 450  # 官方限制单次 500 字节

    def __init__(self, cfg):
        super().__init__(cfg)
        self.endpoint = (
            cfg.get("translate.mymemory.endpoint")
            or "https://api.mymemory.translated.net/get"
        )
        self.timeout = int(cfg.get("translate.mymemory.timeout", 20))
        self.email = (cfg.get("pubmed.email") or "").strip()

    def available(self) -> bool:
        return True

    def _translate_chunk(self, text: str) -> str:
        params: Dict[str, Any] = {"q": text, "langpair": "en|zh-CN"}
        if self.email:
            params["de"] = self.email  # 提供邮箱可提高每日免费额度
        resp = self.session.get(self.endpoint, params=params, timeout=self.timeout)
        data = resp.json()
        if str(data.get("responseStatus")) not in ("200", "OK"):
            raise RuntimeError(f"MyMemory 错误：{data.get('responseDetails')}")
        result = (data.get("responseData") or {}).get("translatedText", "")
        time.sleep(0.6)  # 免费接口限流，主动放慢
        return html.unescape(str(result))


# --------------------------------------------------------------------------
# 后端 F：不翻译
# --------------------------------------------------------------------------
class NoopTranslator(BaseTranslator):
    name = "none"

    def available(self) -> bool:
        return True

    def _translate_chunk(self, text: str) -> str:
        return ""

    def translate(self, text: str) -> str:
        return ""


PROVIDER_REGISTRY = {
    "llm": LLMTranslator,
    "baidu": BaiduTranslator,
    "youdao": YoudaoTranslator,
    "google": GoogleTranslator,
    "mymemory": MyMemoryTranslator,
    "none": NoopTranslator,
}


# --------------------------------------------------------------------------
# 调度器：降级链 + 缓存
# --------------------------------------------------------------------------
class Translator:
    """翻译总入口，按配置顺序尝试各后端。"""

    def __init__(self, cfg, db=None):
        self.cfg = cfg
        self.db = db
        self.enabled = bool(cfg.get("translate.enabled", True))
        self.max_chars = int(cfg.get("translate.max_chars", 4000))
        self.use_cache = bool(cfg.get("translate.cache", True)) and db is not None
        self.interval = float(cfg.get("translate.interval", 0.5))
        self.translate_title = bool(cfg.get("translate.translate_title", True))

        self.providers: List[BaseTranslator] = []
        for name in cfg.get("translate.providers") or []:
            key = str(name).strip().lower()
            cls = PROVIDER_REGISTRY.get(key)
            if cls is None:
                log.warning("未知翻译后端：%s（已忽略）", name)
                continue
            try:
                inst = cls(cfg)
            except Exception as exc:  # pragma: no cover
                log.warning("翻译后端 %s 初始化失败：%s", key, exc)
                continue
            if inst.available():
                self.providers.append(inst)
            else:
                log.info("翻译后端 %s 未配置完整，跳过", key)

        # 运行期被判定为不可用的后端（连续失败），本次运行内不再尝试
        self._disabled: set[str] = set()

        if self.enabled and not self.providers:
            log.warning("未找到任何可用翻译后端，本次将只输出英文原文")

    # ---------------- 对外 ----------------
    def available_providers(self) -> List[str]:
        return [p.name for p in self.providers]

    def translate_text(self, text: str) -> Tuple[str, str]:
        """
        翻译一段文本。
        返回 (译文, 后端名)；全部失败返回 ("", "")
        """
        text = (text or "").strip()
        if not self.enabled or not text:
            return "", ""

        if len(text) > self.max_chars:
            log.debug("文本超长（%d>%d），已截断", len(text), self.max_chars)
            text = text[: self.max_chars] + " ..."

        # 缓存命中
        if self.use_cache:
            cached = self.db.get_translation(text)
            if cached:
                return cached, "cache"

        for provider in self.providers:
            if provider.name in self._disabled:
                continue
            try:
                result = provider.translate(text)
                if result and result.strip():
                    if self.use_cache:
                        self.db.save_translation(text, result, provider.name)
                    if self.interval:
                        time.sleep(self.interval)
                    return result.strip(), provider.name
                log.warning("翻译后端 %s 返回空结果，尝试下一个", provider.name)
            except Exception as exc:
                log.warning("翻译后端 %s 失败：%s，尝试下一个", provider.name, exc)
                self._disabled.add(provider.name)

        log.error("所有翻译后端均失败，返回空译文")
        return "", ""

    def translate_article(self, article: Article) -> Article:
        """原地翻译一篇文献的标题与摘要。"""
        if not self.enabled:
            return article

        if self.translate_title and article.title and not article.title_zh:
            zh, provider = self.translate_text(article.title)
            article.title_zh = zh
            if provider and provider != "cache":
                article.translate_provider = provider

        if article.abstract and not article.abstract_zh:
            zh, provider = self.translate_text(article.abstract)
            article.abstract_zh = zh
            if provider and provider != "cache":
                article.translate_provider = provider

        return article

    def translate_articles(self, articles: Sequence[Article]) -> List[Article]:
        """批量翻译，带进度日志；单篇失败不影响整体。"""
        total = len(articles)
        if not self.enabled or total == 0:
            return list(articles)

        log.info("开始翻译 %d 篇文献，后端优先级：%s", total, " -> ".join(self.available_providers()) or "无")
        ok = 0
        for idx, art in enumerate(articles, 1):
            try:
                self.translate_article(art)
                if art.abstract_zh:
                    ok += 1
                log.info("翻译进度 %d/%d：PMID=%s %s", idx, total, art.pmid,
                         "成功" if art.abstract_zh else "失败")
            except Exception as exc:  # pragma: no cover
                log.error("翻译 PMID=%s 出错：%s", art.pmid, exc)
        log.info("翻译完成：%d/%d 篇成功", ok, total)
        return list(articles)


if __name__ == "__main__":  # 手动自检： python translate.py
    from config import load_config

    conf = load_config()
    tr = Translator(conf)
    print("可用后端：", tr.available_providers() or "（无）")
    demo = (
        "Protein A affinity chromatography remains the workhorse capture step for "
        "monoclonal antibody purification, but host cell protein clearance and resin "
        "cost remain key challenges in downstream processing."
    )
    out, used = tr.translate_text(demo)
    print(f"\n[{used or '失败'}] {out or '（无译文）'}")
