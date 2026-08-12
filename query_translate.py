"""Chinese-to-English query conversion for on-demand literature search."""
from __future__ import annotations

import re

import requests


# Prefer curated biomedical wording for common SciRobot topics.  General-purpose
# machine translation is only used when a phrase is not covered here.
BIOMEDICAL_TERMS = {
    "抗体纯化": "antibody purification",
    "层析与介质": "chromatography",
    "层析介质": "chromatography resin",
    "连续生产": "continuous manufacturing",
    "质量分析": "quality analysis",
    "宿主细胞蛋白": "host cell protein",
    "抗体偶联药物": "antibody-drug conjugate",
    "制剂与稳定性": "formulation stability",
    "制剂稳定性": "formulation stability",
    "抗体工程": "antibody engineering",
    "双特异性抗体": "bispecific antibody",
    "纳米抗体": "nanobody",
    "细胞株开发": "cell line development",
    "细胞株": "cell line",
    "上游工艺": "upstream processing",
    "下游工艺": "downstream processing",
    "中国仓鼠卵巢细胞": "Chinese hamster ovary cell",
    "生物分析": "bioanalysis",
    "计算生物学": "computational biology",
    "蛋白质设计": "protein design",
    "免疫治疗": "immunotherapy",
    "生物制品法规": "biologics regulation",
    "技术转移": "technology transfer",
    "病毒清除": "viral clearance",
    "蛋白聚集": "protein aggregation",
    "亲和层析": "affinity chromatography",
    "离子交换层析": "ion exchange chromatography",
    "疏水作用层析": "hydrophobic interaction chromatography",
}


def _phrases(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，;；\n]+", text or "") if item.strip()]


def _google(cfg, phrase: str) -> str:
    endpoint = cfg.get("translate.google.endpoint") or "https://translate.googleapis.com/translate_a/single"
    timeout = int(cfg.get("translate.google.timeout", 20) or 20)
    response = requests.get(
        endpoint,
        params={"client": "gtx", "sl": "zh-CN", "tl": "en", "dt": "t", "q": phrase},
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 SciRobot/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    if not data or not isinstance(data, list) or not data[0]:
        raise RuntimeError("Google 翻译返回结构异常")
    return "".join(segment[0] for segment in data[0] if segment and segment[0]).strip()


def _mymemory(cfg, phrase: str) -> str:
    endpoint = cfg.get("translate.mymemory.endpoint") or "https://api.mymemory.translated.net/get"
    timeout = int(cfg.get("translate.mymemory.timeout", 20) or 20)
    params = {"q": phrase, "langpair": "zh-CN|en"}
    email = (cfg.get("pubmed.email") or "").strip()
    if email:
        params["de"] = email
    response = requests.get(endpoint, params=params, timeout=timeout, headers={"User-Agent": "SciRobot/1.0"})
    response.raise_for_status()
    data = response.json()
    result = str((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not result:
        raise RuntimeError("MyMemory 未返回译文")
    return result


def translate_chinese_query(text: str, cfg) -> tuple[str, str]:
    """Return an editable English query and a short description of the method."""
    phrases = _phrases(text)
    if not phrases:
        return "", ""

    translated: list[str] = []
    methods: set[str] = set()
    failures: list[str] = []
    for phrase in phrases:
        curated = BIOMEDICAL_TERMS.get(phrase)
        if curated:
            translated.append(curated)
            methods.add("专业术语词典")
            continue

        value = ""
        for name, provider in (("Google", _google), ("MyMemory", _mymemory)):
            try:
                value = provider(cfg, phrase)
                if value:
                    methods.add(name)
                    break
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if not value:
            detail = failures[-1] if failures else "无可用翻译服务"
            raise RuntimeError(f"“{phrase}”转换失败（{detail}）")
        translated.append(value)

    return ", ".join(translated), " + ".join(sorted(methods))
