"""SciRobot 本地用户偏好设置。"""
from __future__ import annotations

import json
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, Frame, Label, Entry, Spinbox
from tkinter import BOTH, LEFT, RIGHT, X
from tkinter import messagebox
from tkinter import ttk

from config import BASE_DIR

PREFERENCES_FILE = BASE_DIR / "data" / "user_preferences.json"

TOPICS = [
    ("抗体纯化", ["antibody purification", "protein a", "downstream processing"]),
    ("层析与介质", ["chromatography", "resin", "ion exchange"]),
    ("连续生产", ["continuous manufacturing", "perfusion", "continuous chromatography"]),
    ("质量分析", ["host cell protein", "aggregate", "charge variant"]),
    ("ADC 与偶联药物", ["antibody drug conjugate", "ADC", "conjugation"]),
    ("制剂与稳定性", ["formulation", "stability", "aggregation"]),
    ("抗体发现与工程", ["antibody engineering", "bispecific antibody", "nanobody", "Fc engineering", "affinity maturation"]),
    ("细胞株与上游工艺", ["CHO cell", "cell culture", "fed-batch", "upstream process", "perfusion"]),
    ("生物分析与 CMC", ["analytical method development", "comparability", "process characterization", "critical quality attribute", "CMC"]),
    ("AI 与计算生物学", ["protein design", "machine learning", "structure prediction", "computational biology"]),
    ("免疫治疗与临床转化", ["CAR-T", "immune checkpoint", "immunotherapy", "tumor immunology", "cell therapy"]),
    ("生物制品法规与产业化", ["biologics regulation", "regulatory science", "technology transfer", "GMP", "biopharmaceutical manufacturing"]),
]


def load_preferences() -> dict:
    default = {"topics": [], "include_terms": [], "exclude_terms": [], "daily_limit": 1, "lookback_days": 7}
    try:
        data = json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
        return {**default, **(data if isinstance(data, dict) else {})}
    except (OSError, json.JSONDecodeError):
        return default


def preferences_configured() -> bool:
    return PREFERENCES_FILE.exists()


def personal_mode_active() -> bool:
    """Whether a saved profile should override the shared feed mode."""
    return preferences_configured() and load_preferences().get("delivery_mode", "personal") == "personal"


def preference_terms(data: dict | None = None) -> list[str]:
    """Return the topic and custom terms selected for local ranking."""
    data = data or load_preferences()
    selected = set(data.get("topics") or [])
    terms: list[str] = []
    for label, topic_terms in TOPICS:
        if label in selected:
            terms.extend(topic_terms)
    terms.extend(data.get("include_terms") or [])

    # Keep the first spelling of each term, but match case-insensitively later.
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = str(term).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result


def _terms(value: str) -> list[str]:
    return [x.strip() for x in value.replace("；", ",").replace(";", ",").split(",") if x.strip()]


def show_preferences() -> None:
    data = load_preferences()
    root = Tk()
    root.title("SciRobot · 文献偏好设置")
    root.geometry("600x600")
    root.minsize(540, 560)
    root.configure(bg="#f8fafc")

    outer = Frame(root, bg="#f8fafc", padx=28, pady=24)
    outer.pack(fill=BOTH, expand=True)
    Label(outer, text="你的文献偏好", bg="#f8fafc", fg="#0f172a", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    Label(outer, text="设置后会保存在这台电脑；后续将用于个性化筛选和排序。", bg="#f8fafc", fg="#64748b", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 18))

    Label(outer, text="研究主题", bg="#f8fafc", fg="#334155", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
    topics_box = Frame(outer, bg="#f8fafc")
    topics_box.pack(fill=X, pady=(5, 14))
    selected = set(data.get("topics") or [])
    topic_vars: dict[str, BooleanVar] = {}
    for index, (label, _) in enumerate(TOPICS):
        var = BooleanVar(value=label in selected)
        topic_vars[label] = var
        ttk.Checkbutton(topics_box, text=label, variable=var).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 30), pady=3)

    include_var = StringVar(value=", ".join(data.get("include_terms") or []))
    exclude_var = StringVar(value=", ".join(data.get("exclude_terms") or []))
    limit_var = StringVar(value=str(data.get("daily_limit") or 1))
    for label, variable, hint in [
        ("额外关注词", include_var, "用逗号分隔，例如：mixed-mode, viral clearance"),
        ("不感兴趣词", exclude_var, "用逗号分隔，例如：clinical trial, diagnosis"),
    ]:
        Label(outer, text=label, bg="#f8fafc", fg="#334155", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        Entry(outer, textvariable=variable, font=("Microsoft YaHei UI", 10)).pack(fill=X, pady=(5, 2))
        Label(outer, text=hint, bg="#f8fafc", fg="#94a3b8", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(0, 10))

    row = Frame(outer, bg="#f8fafc")
    row.pack(fill=X, pady=(2, 18))
    Label(row, text="每日推送篇数", bg="#f8fafc", fg="#334155", font=("Microsoft YaHei UI", 9, "bold")).pack(side=LEFT)
    Spinbox(row, from_=1, to=10, width=5, textvariable=limit_var, font=("Microsoft YaHei UI", 10)).pack(side=LEFT, padx=10)

    def save() -> None:
        chosen = [label for label, var in topic_vars.items() if var.get()]
        try:
            daily_limit = max(1, min(10, int(limit_var.get())))
        except ValueError:
            daily_limit = 1
        result = {
            "topics": chosen,
            "include_terms": _terms(include_var.get()),
            "exclude_terms": _terms(exclude_var.get()),
            "daily_limit": daily_limit,
            # 保留已设定的回溯窗口；它不再在偏好界面中展示。
            "lookback_days": int(data.get("lookback_days") or 7),
        }
        PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = PREFERENCES_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(PREFERENCES_FILE)
        messagebox.showinfo("SciRobot", "偏好已保存。下次推送会使用新的设置。")
        root.destroy()

    # Keep the actions immediately after the final field.  Using a bottom-side
    # pack here can push them outside the window under high-DPI scaling.
    buttons = Frame(outer, bg="#f8fafc")
    buttons.pack(fill=X, pady=(0, 4))
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side=RIGHT)
    ttk.Button(buttons, text="保存偏好", command=save).pack(side=RIGHT, padx=(0, 8))
    root.bind("<Control-Return>", lambda _event: save())
    root.mainloop()


if __name__ == "__main__":
    show_preferences()
