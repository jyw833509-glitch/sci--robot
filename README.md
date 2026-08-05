# SciRobot — 抗体纯化文献自动推送机器人

每天定时检索 PubMed 上新发表的**单抗纯化 / 层析 / 下游工艺**相关论文，自动翻译成中文摘要，生成日报并以**桌面弹窗**（或邮件 / 群机器人）推送到你面前。历史文献写入 SQLite，**同一篇永远不会推送第二次**。

```
PubMed 检索  →  相关度打分降噪  →  SQLite 去重  →  中文翻译  →  生成日报  →  桌面弹窗 / 邮件 / 群机器人推送
```

---

## 1. 功能一览

| 能力 | 说明 |
| --- | --- |
| 文献来源 | PubMed 官方 E-utilities API（esearch + efetch），支持 NCBI API Key 提速 |
| 检索式 | `(monoclonal antibody OR therapeutic antibody OR mAb) AND (purification OR chromatography OR downstream processing)`，可在配置里自由改 |
| 抓取字段 | 标题、作者、期刊、发表时间、DOI、英文摘要、关键词、PMID、单位 |
| 降噪 | 本地相关度打分，过滤「命中关键词但与纯化工艺无关」的临床论文 |
| 翻译 | 5 种后端可插拔，自动降级：大模型 / 百度 / 有道 / Google / MyMemory |
| 翻译缓存 | 同一段英文只翻译一次，写入数据库，重跑不额外花钱 |
| 日报 | HTML（邮件正文）+ Markdown（归档/附件），自动落盘到 `reports/` |
| 推送 | 桌面弹窗（tkinter 独立窗口，展示完整中文摘要，默认启用）、SMTP 邮件（多收件人 + 抄送 + 附件）、可选企业微信 / 钉钉 / 飞书群机器人 |
| 去重 | SQLite 以 PMID 唯一约束，天然幂等；推送失败的文献下次自动重试 |
| 定时 | 内置常驻调度器，也可交给 Windows 计划任务 / cron / systemd / Docker |

---

## 2. 项目结构

```
scirobot/
├── main.py                 命令行入口（init / check / run / schedule / test-mail / stats / backfill）
├── config.py               配置加载：YAML + 默认值 + 环境变量覆盖
├── logger.py               统一日志（控制台 + logs/bot.log 按天切分）
├── search.py               ★ 文献搜索：PubMed API 调用 + XML 解析 + 相关度打分
├── translate.py            ★ 摘要翻译：多后端降级链 + 长文本分块 + 缓存
├── database.py             ★ 数据管理：SQLite 建表 / 去重 / 推送状态 / 翻译缓存
├── report.py               日报渲染：HTML / Markdown / 纯文本
├── notifier.py             ★ 推送模块：SMTP 邮件 + Webhook 群机器人 + 桌面弹窗
├── desktop_notify.py       桌面弹窗 UI（tkinter，被 notifier 以子进程唤起）
├── scheduler.py            ★ 定时任务：完整流水线 + 每日调度
│
├── config.example.yaml     配置模板（含详细中文注释）
├── requirements.txt        依赖清单
├── run_daily.bat           Windows 单次运行脚本（配合计划任务）
├── run_scheduler.bat       Windows 常驻运行脚本
├── install_task.ps1        一键注册 Windows 计划任务
├── Dockerfile              容器化部署
├── docker-compose.yml      容器编排
├── deploy/
│   └── scirobot.service  Linux systemd 服务单元
│
├── data/literature.db      SQLite 数据库（自动生成）
├── reports/YYYY-MM-DD.html 每日日报（自动生成）
└── logs/bot.log            运行日志（自动生成）
```

---

## 3. 五分钟上手

### 3.1 安装

```bash
cd scirobot

# 建虚拟环境（推荐）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
```

### 3.2 初始化

```bash
python main.py init
```

会生成 `config.yaml`、建好数据库、创建 `logs/`、`reports/` 目录。

### 3.3 填配置

打开 `config.yaml`，**至少**要改这几项：

```yaml
pubmed:
  email: "你的邮箱@example.com"      # NCBI 要求标识调用方，强烈建议填

notifier:
  email:
    smtp_host: "smtp.qq.com"
    smtp_port: 465
    username: "你的QQ号@qq.com"
    password: "xxxxxxxxxxxxxxxx"     # ← QQ/163 填「授权码」，不是登录密码！
    to:
      - "收件人@example.com"
```

**（强烈推荐）配置大模型翻译**，术语准确度远高于通用翻译接口：

```yaml
translate:
  providers: ["llm", "google", "mymemory"]
  llm:
    base_url: "https://api.deepseek.com/v1"
    api_key: "sk-xxxxxxxx"
    model: "deepseek-chat"
```

> 不填 `llm.api_key` 也能跑，会自动降级到 Google / MyMemory 免费接口（Google 在国内需要代理，可在 `translate.proxy` 填 `http://127.0.0.1:7890`）。

### 3.4 验证

```bash
python main.py check        # 检查配置完整性、打印最终检索式
python main.py test-mail    # 发一封测试邮件，确认能收到
python main.py search       # 只检索不入库，看看今天有什么文献
```

### 3.5 正式跑

```bash
python main.py run --dry-run   # 生成日报但不发邮件，先看看效果（reports/ 目录）
python main.py run             # 完整执行：检索 → 翻译 → 日报 → 邮件
python main.py schedule        # 常驻，每天 08:30 自动执行
```

---

## 4. 命令速查

| 命令 | 作用 |
| --- | --- |
| `python main.py init` | 生成配置文件、初始化数据库和目录 |
| `python main.py check` | 检查配置完整性，打印检索式与可用翻译后端 |
| `python main.py search --days 30 --limit 20` | 只检索并打印，不入库不推送 |
| `python main.py run` | 执行一次完整流水线 |
| `python main.py run --days 30` | 临时把回溯窗口改成 30 天 |
| `python main.py run --dry-run` | 生成日报但不推送 |
| `python main.py show` | 弹窗预览今日待推送文献（不推送、不标记） |
| `python main.py schedule` | 常驻进程，按配置时间每天执行 |
| `python main.py test-mail` | 发送测试消息验证 SMTP / Webhook |
| `python main.py stats` | 查看数据库统计（总数 / 已推送 / 缓存等） |
| `python main.py backfill --start 2026-01-01 --end 2026-06-30` | 历史回填，只入库并标记为已推送，避免首次推送刷屏 |
| `python main.py publish --days 30 --max-articles 20` | **发布者用**：检索+翻译并导出全局内容日历 `data/feed.json`（配合 feed 模式让所有人看到相同内容，详见 5.8） |

各模块也能单独自检：`python search.py`、`python translate.py`、`python database.py`、`python notifier.py`。

---

## 5. 配置详解（config.yaml）

### 5.1 检索

```yaml
pubmed:
  keyword_groups:                       # 组内 OR，组间 AND
    - ["monoclonal antibody", "therapeutic antibody", "mAb"]
    - ["purification", "chromatography", "downstream processing"]
  field_tag: "Title/Abstract"           # 限定字段；留空=全字段（噪音大）
  lookback_days: 7                      # 回溯天数
  date_type: "edat"                     # edat=进入PubMed日期 pdat=出版日期
  max_results: 100
  query: ""                             # 填了就直接用这条检索式，忽略上面的关键词组
```

> **为什么 `lookback_days` 默认是 7 而不是 1？**
> 因为数据库按 PMID 去重，窗口开大不会重复推送，反而能补上「延迟上架」的文献。这条检索式属于窄领域，实测约 **15~20 篇/月**，用 7 天窗口既不漏也不吵。

想扩大召回，可以在第一组加 `"antibody"` / `"bispecific antibody"` / `"ADC"`，第二组加 `"Protein A"` / `"affinity chromatography"` / `"polishing"`，同时把 `relevance.min_score` 调高到 5~6 抵消噪音。

### 5.2 相关度降噪

PubMed 只能做布尔匹配，「抗体 + 层析」也会命中大量临床检验类论文。这里加了一层本地打分：

| 规则 | 分值 |
| --- | --- |
| 标题命中关键词组 | +3 / 组 |
| 仅摘要命中关键词组 | +1 / 组 |
| 命中下游工艺强相关词（Protein A、HCP、elution、resin、viral clearance…） | 每个 +1，上限 +5 |
| 命中临床噪音词（patients、clinical trial、immunohistochemistry…） | 每个 -2，上限 -4 |

低于 `relevance.min_score`（默认 4）的直接丢弃。设成 `0` 可关闭过滤。

### 5.3 翻译后端

| 后端 | 需要的配置 | 特点 |
| --- | --- | --- |
| `llm` | `base_url` + `api_key` + `model` | **推荐**，术语最准，支持 DeepSeek / 智谱 / 通义 / Kimi / OpenAI 等任意 OpenAI 兼容接口 |
| `baidu` | `app_id` + `app_key` | 国内直连稳定，有免费额度 |
| `youdao` | `app_key` + `app_secret` | 同上 |
| `google` | 无需 Key | 免费，国内需代理 |
| `mymemory` | 无需 Key | 免费兜底，有频率限制 |
| `none` | — | 不翻译，只推英文 |

按 `providers` 列表顺序尝试，某个后端失败会自动降级到下一个，并在本次运行内不再重试该后端。

`llm.system_prompt` 里内置了抗体纯化领域的术语表（Protein A 亲和层析、HCP 宿主细胞蛋白、CEX/AEX、UF/DF、CQA…），可以按团队习惯自行修改。

### 5.4 推送

```yaml
notifier:
  channels: ["desktop"]        # 可同时开 ["desktop", "email", "webhook"]
  desktop:                     # 桌面弹窗（详见 5.7 节）
    enabled: true
    mode: "window"             # window=独立窗口（推荐） / toast=系统通知
    timeout: 0                 # window 模式停留秒数，0=需手动关闭
  email:
    to: ["a@x.com", "b@x.com"] # 多收件人
    cc: []
    attach_markdown: false     # true 则把 .md 日报作为附件
    send_when_empty: false     # 无新文献时是否也发一封
  webhook:
    enabled: false
    type: "wecom"              # wecom / dingtalk / feishu
    url: "https://qyapi.weixin.qq.com/..."
```

常用 SMTP 参数：

| 邮箱 | host | port | 说明 |
| --- | --- | --- | --- |
| QQ 邮箱 | smtp.qq.com | 465 | 密码填**授权码**（设置→账户→POP3/SMTP→生成授权码） |
| 163 邮箱 | smtp.163.com | 465 | 密码填**授权码** |
| 腾讯企业邮 | smtp.exmail.qq.com | 465 | 密码填客户端专用密码 |
| Gmail | smtp.gmail.com | 465 | 密码填应用专用密码 |
| Outlook | smtp.office365.com | 587 | 改成 `use_ssl: false` + `use_starttls: true` |

### 5.5 环境变量覆盖

所有密钥都可以用环境变量注入，优先级高于 `config.yaml`，适合 Docker / CI / 服务器：

```
PUBMED_API_KEY  LLM_API_KEY  LLM_BASE_URL  LLM_MODEL
BAIDU_APP_ID    BAIDU_APP_KEY   YOUDAO_APP_KEY  YOUDAO_APP_SECRET
SMTP_HOST  SMTP_PORT  SMTP_USERNAME  SMTP_PASSWORD  MAIL_TO
WEBHOOK_URL  DB_PATH  HTTP_PROXY_URL
```

`MAIL_TO` 支持逗号分隔多个收件人。

### 5.6 推送节奏：每天一篇 + 仅工作日

默认配置就是「**工作日每天一篇**」，正好满足「每天推送一篇即可、休息日不推送」的需求：

```yaml
pipeline:
  daily_limit: 1        # 每天最多推送 1 篇；0 = 不限制（全部待推送都推）

scheduler:
  run_at: ["08:30"]     # 每天触发时间（24 小时制，可写多个）
  workday:
    skip_weekends: true # 周六、周日不推送
    holidays: []        # 额外跳过的日期（法定节假日等），格式 "YYYY-MM-DD"
    makeup_workdays: [] # 周末补班日：填了这些日期，即使是周末也照常推送
    use_chinese_calendar: false  # true 可自动识别中国法定节假日与调休（需先 pip install chinese_calendar）
```

- **`daily_limit`**：把回溯窗口内未推送的文献按「最新优先」逐日消化。假设今天有 5 篇待推，先推最新的 1 篇，剩下 4 篇在后续工作日每天推 1 篇——既不会一次刷屏，也不会漏推。
- **`skip_weekends`**：调度器在周六/周日自动跳过，不检索、不推送。**手动 `python main.py run` 不受此限制**，随时可手动补跑。
- 手动一次性多推几篇（补量）：`python main.py run --limit 5`（0 = 全部推送）。
- **`use_chinese_calendar`**：先 `pip install chinese_calendar`，设为 `true` 后自动按官方节假日和调休判断，连 `holidays` / `makeup_workdays` 都不用自己维护。

### 5.7 桌面弹窗（默认推送方式）

除了邮件，文献还能以**桌面弹窗**的形式直接弹到你面前——一个窗口里展示标题、作者、期刊、发表时间、中文摘要、可折叠的英文原文，以及点击即开的 PubMed / DOI 链接。

```yaml
notifier:
  channels: ["desktop"]     # 只弹窗；想同时收邮件就写成 ["desktop", "email"]
  desktop:
    enabled: true
    mode: "window"          # window = 独立窗口（推荐，信息完整，需手动关闭）
                            # toast  = Windows 系统通知（需 pip install win10toast，几秒后自动消失）
    timeout: 0              # window 模式下窗口停留秒数；0 = 一直显示到手动关闭
```

几点重要说明：

- **弹窗需要在「你已登录的桌面会话」里运行。** 机器人进程必须跑在你本人登录的 Windows 上（前台、后台、或「只在用户登录时运行」的计划任务均可），弹窗才会出现。若部署在无图形界面的服务器 / 纯后台服务，弹窗无法显示，此时应改用 `email` 或 `webhook` 渠道。
- **窗口不会阻塞流水线。** 弹窗由独立子进程唤起，主程序立刻继续；关闭窗口与否不影响去重与标记——弹出即视为已通知，窗口里的文献会被标记 `pushed=1`。
- **只弹窗不推送时**，可用预览命令先看效果：`python main.py show`（或 `show --limit 3`）。
- 想单独验证弹窗本身（不连 PubMed）：`python main.py test-mail` 会顺带测试 desktop 渠道，弹出一个示例文献窗口。

### 5.8 全局同步模式（让所有人看到相同的文献）

默认 `content.mode: local` 下，每个客户端**各自**去 PubMed 拉「最近几天」的新论文，再对比自己的本地数据库去重推送。这意味着：**两个人安装日期不同，收到的内容一定不同**（A 周一装推周一的新文，B 下月装推下月的新文）。

如果你希望「**不论谁安装，大家每天看到的文献都一模一样**」，改用 `feed` 模式——由你（发布者）统一决定内容，所有人只是被动播放同一份日历。

**原理**：发布者把检索 + 翻译好的文献，按「工作日」铺成一张内容日历（每天一篇），导出成 `feed.json`（已含完整中文译文）托管到一个所有人可访问的 URL。客户端不再各自联网检索，而是每天去拉这份日历，播放「今天」那一条。因为所有人读的是同一份文件，内容必然完全一致，且会随你更新日历而同步。

```yaml
content:
  mode: "feed"            # local = 各端独立检索（默认）；feed = 全局同步
  feed_url: "https://example.com/feed.json"   # 日历托管地址（URL 或本地路径）
  feed_cache: "data/feed_cache.json"          # 拉取失败时的本地缓存
  timezone: "Asia/Shanghai"                    # 用哪个时区的「今天」匹配日历
```

**发布者三步（你来做一次，之后定期更新即可）**：

```bash
# 1) 检索 + 翻译 + 导出内容日历（默认输出到 content.feed_output）
#    --start 建议固定为「上线日」，这样重复 publish 时日历不会前后错位
python main.py publish --days 30 --max-articles 20 --start 2026-08-04

# 2) 把生成的 data/feed.json 托管到一个可访问地址
#    方式任选其一：
#      - GitHub：推到仓库后用 https://raw.githubusercontent.com/<你>/<仓>/main/feed.json
#      - 对象存储：coscli cp data/feed.json cos://my-bucket/feed.json
#      - 内网静态文件 / 共享盘路径
#    也可以直接在 content.feed_upload_cmd 里写好上传命令，publish 会自动执行

# 3) 把 content.mode 改为 feed、填上 feed_url，把这个 config.yaml 分发给同事
```

**同事（安装者端）**：只需拿到你发的 `config.yaml`（已设好 `mode: feed` 和 `feed_url`），直接 `main.py schedule` 即可。他们**不需要翻译 key、也不需要各自联网搜 PubMed**，纯播放，最简单也最一致。

**工作机制（广播模型）**：所有客户端都按「今天这个日历日期」取同一篇文献，因此在任意给定真实日期，每个客户端推送的内容**完全一致**。某台机器某天关机漏推，当天那篇就跳过（不会在之后补播成旧内容），以保证「同一天人人看到同一篇」——这是「实时全局同步」的必然取舍。若想让同事能补看错过的，可让他们手动 `python main.py show --date 2026-08-05` 之类的回看（暂未内置，可按需扩展）。

**一致性验证**：日历里每个工作日绑定一篇确定的 PMID；所有客户端用同一份日历、同一时区，因此「今天」必然命中同一篇。已实测——两份独立数据库各跑一次 feed 模式，推送的 PMID 完全相同。

> **注意**：`feed` 模式下客户端不再检索 PubMed，所以 `pubmed` / `translate` 配置对客户端无用（发布者端才需要）。若 `feed_url` 暂时不可达，会用上次缓存的 `feed_cache.json` 兜底，不会当天断更（但缓存也会随时间变旧，需保证托管地址持续可用）。

### 5.9 全自动托管（GitHub Actions，发布者零日常参与）

上面的 `publish` 命令需要你手动跑。若你想**彻底不依赖自己的电脑**——同事每天照样收到同样的推送——把 `feed.json` 的「生产」也交给 GitHub 服务器：

本仓库已内置 `.github/workflows/publish.yml`，它每天 UTC 22:00（北京 06:00，早于默认推送时间 08:30）自动：

1. 检索 PubMed 最近 180 天的新论文；
2. 翻译成中文（用 DeepSeek 等大模型，质量好又快；也可不配密钥自动降级免费后端）；
3. 重新生成 `data/feed.json` 内容日历；
4. 提交并推回本仓库。

**你只需做一次（之后完全不用管）：**

```bash
# 1) 在 GitHub 新建一个【公开】仓库，把本目录推上去
git init && git add -A && git commit -m "init"
git remote add origin https://github.com/<你的名>/<仓库名>.git
git push -u origin main

# 2)（强烈建议）在 仓库 Settings → Secrets 里添加 LLM_API_KEY
#    值填 DeepSeek / OpenAI 兼容的 API Key，翻译质量与速度都远胜免费后端
#    不填也能跑，但会自动降级 google/mymemory，可能限流

# 3) 在 仓库 Settings → Actions → General 里，确认 "Workflow permissions" = Read and write
#    这样 Actions 才能把生成的 feed.json 推回仓库

# 4) 到 Actions 页面，手动 Run 一次 "每日生成文献内容日历" 工作流，立即生成首份 feed
```

跑起来后，把下面这个 raw 链接填进 `config.yaml` 的 `content.feed_url`，连同配置一起发给同事即可：

```
https://raw.githubusercontent.com/<你的名>/<仓库名>/main/data/feed.json
```

**此后**：GitHub 服务器每天自动更新 `feed.json`，同事的 app 每天拉同一份 → 人人看到同一篇，你的电脑可以一直关机。

> 想一次性把存量的 43 篇文献先铺进日历（同事立刻有内容），可先在本地跑一次
> `python main.py publish --days 180 --max-articles 80 --start 2026-08-04`，
> 再 `git push`。之后交给 Actions 每日增量刷新。

---

## 6. 数据库结构

`data/literature.db`（SQLite，可用 DB Browser for SQLite 直接打开）

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `articles` | 文献主表 | `pmid`（**UNIQUE，去重靠它**）、`title`、`title_zh`、`abstract`、`abstract_zh`、`authors`、`journal`、`pub_date`、`doi`、`pushed`、`pushed_at` |
| `translation_cache` | 翻译缓存 | `hash`（原文 MD5）、`target_text`、`provider` |
| `push_log` | 推送流水 | `run_date`、`channel`、`item_count`、`status` |

**去重与重试逻辑**：
1. 检索到的 PMID 先与 `articles` 比对，已存在的直接丢弃；
2. 新文献入库，`pushed = 0`；
3. 日报只取 `pushed = 0` 的文献；
4. **推送成功后**才把这些 PMID 置为 `pushed = 1`；
5. 推送失败（比如 SMTP 挂了）不标记，下次运行自动重试，不会漏。

---

## 7. 常见问题

**Q：跑起来提示「PubMed 命中 0 条」？**
这条检索式很窄（约 15~20 篇/月），某几天没有新文献是正常的。可以 `python main.py search --days 30` 验证链路是否正常。

**Q：翻译全部失败？**
`python main.py check` 看「可用翻译后端」。若只剩 google/mymemory 且在国内网络，需要在 `translate.proxy` 填代理，或改用大模型 / 百度后端。

**Q：邮件报 `SMTPAuthenticationError`？**
99% 是密码填成了登录密码。QQ/163 必须用**授权码**。

**Q：想改成每天两次推送？**
```yaml
scheduler:
  run_at: ["08:30", "18:00"]
```

**Q：只要每天一篇、周末不推？**
已经是默认行为了：`pipeline.daily_limit: 1` + `scheduler.workday.skip_weekends: true`。如果把 `daily_limit` 调成 0 就会一天内把待推送文献全部推完。

**Q：法定节假日也想自动跳过？**
最简单：把 `scheduler.workday.use_chinese_calendar` 设为 `true`（需先 `pip install chinese_calendar`），自动按官方节假日和调休判断。或者手动把日期写进 `holidays:` 列表。

**Q：首次运行不想被历史文献刷屏？**
先 `python main.py backfill --start 2026-01-01 --end 2026-08-01`，把历史文献入库并直接标记为已推送，之后只推真正的新文献。

**Q：想换个研究方向复用？**
只改 `pubmed.keyword_groups`、`relevance.bonus_terms` 和 `translate.llm.system_prompt` 里的术语表即可，其余逻辑完全通用。

**Q：弹窗没出现？**
最常见原因：机器人进程跑在没有桌面的环境（远程无头服务器、或计划任务勾了「不管用户是否登录都运行」）。把进程放到你登录的会话里运行即可——本地直接 `python main.py schedule`，或计划任务勾选「只在用户登录时运行」。

**Q：只想要弹窗、不要邮件？**
`notifier.channels` 只保留 `["desktop"]` 即可（现已是默认）。若反过来想退回纯邮件，改成 `["email"]`。

**Q：弹窗窗口太大/太小、或想自动关闭？**
在 `notifier.desktop.timeout` 设秒数（如 30），窗口会在 30 秒后自动关闭；`mode` 改成 `toast` 则走系统通知（更轻量，需 `pip install win10toast`）。

---

## 8. 部署

见 **[DEPLOY.md](DEPLOY.md)**，涵盖 Windows 计划任务、Linux cron / systemd、Docker、云服务器四种方式。

---

## 9. 合规提示

- 本工具仅调用 PubMed 官方公开 API 获取**题录与摘要**，不抓取、不分发出版商的全文 PDF。
- 请遵守 [NCBI E-utilities 使用政策](https://www.ncbi.nlm.nih.gov/books/NBK25497/)：无 API Key 时不超过 3 请求/秒（程序已内置限速），建议在 `pubmed.email` 填写真实邮箱。
- 摘要中文译文由机器翻译生成，仅供快速筛选参考，**引用前请核对英文原文**。
