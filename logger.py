"""
logger.py —— 统一日志模块

同时输出到控制台和 logs/bot.log（按天切分，保留 30 天）。
Windows 控制台默认 GBK，这里强制 UTF-8 避免中文乱码。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """按天滚动的文件日志，但滚动失败（如 Windows 上文件被其他进程占用、
    无权限重命名）时只记一条警告，绝不中断主流程。"""

    def rotation_filename(self, default_name: str) -> str:
        return default_name

    def rotate(self, source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
            os.rename(source, dest)
        except OSError as exc:  # Windows 下文件被占用 / 无权限都落到这里
            # 放弃本次滚动，保留原文件继续写入，确保不丢日志、不崩进程
            try:
                logging.getLogger("logger").warning(
                    "日志滚动失败（文件可能被占用），本次跳过归档：%s", exc
                )
            except Exception:
                pass

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except Exception as exc:  # 任何意外都兜底，绝不让日志器拖垮主程序
            try:
                logging.getLogger("logger").warning("日志滚动异常，已跳过：%s", exc)
            except Exception:
                pass


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO") -> None:
    """初始化根日志器。重复调用只生效一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 控制台
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件（按天切分，delay=True 延迟到首次写入才打开，降低多进程锁竞争）
    file_handler = _SafeTimedRotatingFileHandler(
        log_path / "bot.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取带名字的 logger。未初始化时先做一次默认初始化。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
