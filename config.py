"""
config.py —— 配置加载模块

职责：
    1. 读取 config.yaml（不存在时自动回退到 config.example.yaml 并提示）
    2. 与内置默认值做深度合并，保证任何键都能取到值
    3. 应用环境变量覆盖（便于 Docker / CI / 服务器部署时注入密钥）
    4. 提供 cfg.get("a.b.c", default) 形式的点号取值

对外接口：
    load_config(path=None) -> Config
    Config.get(dotted_key, default=None)
    Config.section(name) -> dict
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# PyInstaller 打包后 __file__ 指向临时 _MEIPASS，需重定向
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG_FILE = BASE_DIR / "config.yaml"
EXAMPLE_CONFIG_FILE = BASE_DIR / "config.example.yaml"


# --------------------------------------------------------------------------
# 内置默认配置：即使 config.yaml 缺项也能正常运行
# --------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "app": {
        "name": "SciRobot",
        "log_level": "INFO",
        "log_dir": "logs",
        "report_dir": "reports",
    },
    "pubmed": {
        "api_key": "",
        "email": "",
        "tool": "scirobot",
        "query": "",
        "keyword_groups": [
            ["monoclonal antibody", "therapeutic antibody", "mAb"],
            ["purification", "chromatography", "downstream processing"],
        ],
        "field_tag": "Title/Abstract",
        "extra_filters": [],
        "lookback_days": 7,
        "date_type": "edat",
        "max_results": 100,
        "require_abstract": True,
        "exclude_publication_types": ["Retracted Publication"],
        "timeout": 30,
        "max_retries": 3,
        "retry_backoff": 2.0,
    },
    "relevance": {
        "enabled": True,
        "min_score": 4,
        "title_weight": 3,
        "abstract_weight": 1,
        "bonus_terms": [
            "protein a", "downstream process", "chromatography", "purification",
            "affinity", "cation exchange", "anion exchange", "hydrophobic interaction",
            "mixed-mode", "multimodal", "host cell protein", "hcp", "aggregate",
            "elution", "resin", "membrane chromatography", "ultrafiltration",
            "diafiltration", "viral clearance", "continuous manufacturing",
            "perfusion", "yield", "impurit", "polishing", "capture step", "drug substance",
        ],
        "bonus_cap": 5,
        "penalty_terms": [
            "patients", "clinical trial", "seroprevalence",
            "immunohistochemistry", "diagnosis", "questionnaire",
        ],
        "penalty_cap": 4,
    },
    "translate": {
        "enabled": True,
        "providers": ["llm", "google", "mymemory"],
        "max_chars": 4000,
        "translate_title": True,
        "cache": True,
        "interval": 0.5,
        "proxy": "",
        "llm": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0.2,
            "timeout": 120,
            "system_prompt": (
                "你是生物制药下游工艺（抗体纯化、层析）领域的资深科研翻译。"
                "请将英文学术摘要忠实翻译为简体中文，术语使用国内通用译法，"
                "只输出译文正文，不要任何前后缀。"
            ),
        },
        "baidu": {"app_id": "", "app_key": ""},
        "youdao": {"app_key": "", "app_secret": ""},
        "google": {
            "endpoint": "https://translate.googleapis.com/translate_a/single",
            "timeout": 20,
        },
        "mymemory": {
            "endpoint": "https://api.mymemory.translated.net/get",
            "timeout": 20,
        },
    },
    "database": {"path": "data/literature.db", "keep_days": 0},
    "pipeline": {
        # 每天推送文献数量上限：1 = 每天一篇；0 = 不限制（全部待推送文献都推）
        "daily_limit": 1,
    },
    "content": {
        # 内容来源模式：
        #   local = 各客户端自行检索 PubMed（默认，内容随安装时间不同）
        #   feed  = 所有客户端从同一份「内容日历」(feed.json) 取内容（全局同步、人人一致）
        "mode": "local",
        # feed 模式下的内容日历地址：http(s) URL 或本地文件路径（相对项目根或绝对）
        "feed_url": "",
        # 拉取失败时的本地缓存位置
        "feed_cache": "data/feed_cache.json",
        # 日历使用的时区说明（客户端用各自本地日期匹配，建议所有用户同处一个时区）
        "timezone": "Asia/Shanghai",
        # publish 命令生成 feed.json 的本地输出位置
        "feed_output": "data/feed.json",
        # publish 完成后可选执行的同步命令（把 feed.json 传到你的托管地址），
        # 例如：coscli cp data/feed.json cos://my-bucket/feed.json
        # 留空则只生成在本地，需你手动托管
        "feed_upload_cmd": "",
    },
    "search_sources": {
        # All sources are free and queried independently; unavailable sources
        # are skipped without blocking the daily push.
        "enabled": ["pubmed", "europe_pmc", "crossref", "openalex", "biorxiv", "chinaxiv"],
    },
    "report": {
        "title": "SciRobot 文献日报",
        "max_items": 30,
        "show_english_abstract": True,
        "fold_english_abstract": True,
        "save_to_disk": True,
        "formats": ["html", "markdown"],
    },
    "notifier": {
        "channels": ["desktop"],
        "desktop": {
            "enabled": True,
            # window = 独立 tkinter 窗口（推荐，可看完整中文摘要）
            # toast  = Windows 系统通知（需 pip install win10toast，几秒后自动消失）
            "mode": "window",
            # window 模式下窗口停留秒数，0 = 需手动关闭
            "timeout": 0,
        },
        "email": {
            "enabled": True,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "use_ssl": True,
            "use_starttls": False,
            "username": "",
            "password": "",
            "from_addr": "",
            "sender_name": "SciRobot",
            "to": [],
            "cc": [],
            "attach_markdown": False,
            "send_when_empty": False,
            "timeout": 30,
        },
        "webhook": {
            "enabled": False,
            "type": "wecom",
            "url": "",
            "secret": "",
            "max_items": 10,
        },
    },
    "scheduler": {
        "run_at": ["08:30"],
        "run_on_start": False,
        "workday": {
            # 是否跳过周末（周六、周日）。true = 周末不推送
            "skip_weekends": True,
            # 额外跳过的日期（法定节假日等），格式 "YYYY-MM-DD"
            "holidays": [],
            # 周末补班日（这些日期即使是周末也照常推送），格式 "YYYY-MM-DD"
            "makeup_workdays": [],
            # 是否使用 python 包 chinese_calendar 自动识别中国法定节假日（含调休）。
            # 启用需先安装： pip install chinese_calendar，再把此项设为 true
            "use_chinese_calendar": False,
        },
    },
}


# 环境变量 -> 配置项 的映射表
ENV_MAPPING: Dict[str, str] = {
    "PUBMED_API_KEY": "pubmed.api_key",
    "PUBMED_EMAIL": "pubmed.email",
    "LLM_BASE_URL": "translate.llm.base_url",
    "LLM_API_KEY": "translate.llm.api_key",
    "LLM_MODEL": "translate.llm.model",
    "BAIDU_APP_ID": "translate.baidu.app_id",
    "BAIDU_APP_KEY": "translate.baidu.app_key",
    "YOUDAO_APP_KEY": "translate.youdao.app_key",
    "YOUDAO_APP_SECRET": "translate.youdao.app_secret",
    "HTTP_PROXY_URL": "translate.proxy",
    "SMTP_HOST": "notifier.email.smtp_host",
    "SMTP_PORT": "notifier.email.smtp_port",
    "SMTP_USERNAME": "notifier.email.username",
    "SMTP_PASSWORD": "notifier.email.password",
    "MAIL_TO": "notifier.email.to",
    "WEBHOOK_URL": "notifier.webhook.url",
    "DB_PATH": "database.path",
    "CONTENT_MODE": "content.mode",
    "CONTENT_FEED_URL": "content.feed_url",
}

# 这些配置项需要做类型转换
_INT_KEYS = {"notifier.email.smtp_port"}
_LIST_KEYS = {"notifier.email.to", "notifier.email.cc"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 深度合并进 base（不修改入参）。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖 PyYAML，请先执行： pip install -r requirements.txt"
        ) from exc
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误（顶层必须是字典）：{path}")
    return data


class Config:
    """配置对象，支持点号路径取值。"""

    def __init__(self, data: Dict[str, Any], source: Path | None = None):
        self._data = data
        self.source = source
        self.base_dir = BASE_DIR

    # ---------------- 取值 ----------------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node if node is not None else default

    def section(self, name: str) -> Dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def set(self, dotted_key: str, value: Any) -> None:
        parts = dotted_key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    # ---------------- 路径工具 ----------------
    def path(self, dotted_key: str, default: str = "") -> Path:
        """把配置里的相对路径转成基于项目根目录的绝对路径。"""
        raw = str(self.get(dotted_key, default) or default)
        p = Path(raw)
        return p if p.is_absolute() else (self.base_dir / p)

    # ---------------- 校验 ----------------
    def validate(self) -> List[str]:
        """返回配置问题清单（警告性质，不抛异常）。"""
        problems: List[str] = []

        if not self.get("pubmed.query") and not self.get("pubmed.keyword_groups"):
            problems.append("pubmed.query 与 pubmed.keyword_groups 不能同时为空")

        if self.get("translate.enabled"):
            providers = self.get("translate.providers", [])
            if not providers:
                problems.append("translate.enabled=true 但未配置任何 translate.providers")
            if "llm" in providers and not self.get("translate.llm.api_key"):
                problems.append(
                    "翻译后端包含 llm 但未填写 translate.llm.api_key，将自动降级到下一个后端"
                )

        if "email" in (self.get("notifier.channels") or []) and self.get(
            "notifier.email.enabled"
        ):
            if not self.get("notifier.email.username"):
                problems.append("notifier.email.username 未填写")
            if not self.get("notifier.email.password"):
                problems.append("notifier.email.password 未填写（QQ/163 邮箱请填授权码）")
            if not self.get("notifier.email.to"):
                problems.append("notifier.email.to 收件人为空")

        if "webhook" in (self.get("notifier.channels") or []) and self.get(
            "notifier.webhook.enabled"
        ):
            if not self.get("notifier.webhook.url"):
                problems.append("notifier.webhook.url 未填写")

        return problems

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Config source={self.source}>"


def _apply_env(data: Dict[str, Any]) -> Dict[str, Any]:
    """应用环境变量覆盖。"""
    cfg = Config(data)
    for env_name, dotted_key in ENV_MAPPING.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        value: Any = raw
        if dotted_key in _INT_KEYS:
            try:
                value = int(raw)
            except ValueError:
                continue
        elif dotted_key in _LIST_KEYS:
            value = [x.strip() for x in raw.replace("；", ";").replace(";", ",").split(",") if x.strip()]
        cfg.set(dotted_key, value)
    return cfg.as_dict()


def _find_bundled_config() -> Path | None:
    """PyInstaller 打包后，在 _MEIPASS 中查找内置配置文件。"""
    if not getattr(sys, "frozen", False):
        return None
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    if not meipass.exists():
        return None
    # 优先 matching config.colleague.yaml -> config.yaml
    for name in ("config.colleague.yaml", "config.yaml", "config.example.yaml"):
        candidate = meipass / name
        if candidate.exists():
            return candidate
    return None


def _copy_bundled_config(src: Path, dst: Path) -> None:
    """把内置配置拷贝到 exe 所在目录。"""
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def load_config(path: str | Path | None = None) -> Config:
    """
    加载配置。
    优先级：环境变量 > config.yaml > config.example.yaml > PyInstaller 内置 colleague 配置 > 内置默认值
    """
    if path:
        cfg_path = Path(path)
        if not cfg_path.is_absolute():
            cfg_path = BASE_DIR / cfg_path
    else:
        cfg_path = DEFAULT_CONFIG_FILE

    user_data: Dict[str, Any] = {}
    used: Path | None = None

    if cfg_path.exists():
        if cfg_path.suffix.lower() == ".json":
            user_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            user_data = _read_yaml(cfg_path)
        used = cfg_path
    else:
        # PyInstaller 打包后，尝试从内置资源中恢复配置
        bundled = _find_bundled_config()
        if bundled:
            # 把同事版配置拷贝到 exe 旁边，后续运行直接用
            try:
                _copy_bundled_config(bundled, cfg_path)
                user_data = _read_yaml(cfg_path)
                used = cfg_path
            except Exception:
                user_data = _read_yaml(bundled)
                used = bundled
        elif EXAMPLE_CONFIG_FILE.exists():
            user_data = _read_yaml(EXAMPLE_CONFIG_FILE)
            used = EXAMPLE_CONFIG_FILE
            print(
                f"[配置] 未找到 {cfg_path.name}，已临时使用 {EXAMPLE_CONFIG_FILE.name}。"
                f"\n[配置] 请复制一份为 config.yaml 并填写邮箱 / API Key 后再正式使用。"
            )

    merged = _deep_merge(DEFAULTS, user_data)
    merged = _apply_env(merged)
    return Config(merged, source=used)


if __name__ == "__main__":  # 手动自检： python config.py
    c = load_config()
    print("配置来源：", c.source)
    print("检索关键词组：", c.get("pubmed.keyword_groups"))
    print("数据库路径：", c.path("database.path"))
    issues = c.validate()
    print("配置检查：", "全部通过" if not issues else "")
    for i in issues:
        print("  -", i)
