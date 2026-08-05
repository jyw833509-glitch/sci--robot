# 部署说明

四种部署方式，按「电脑天天开机」还是「要 7x24 稳定」来选：

| 方式 | 适用场景 | 稳定性 | 推荐度 |
| --- | --- | --- | --- |
| A. Windows 计划任务 | 办公电脑，工作日基本都开机 | 电脑关机就不跑 | ★★★★☆ 本地首选 |
| B. Windows 常驻脚本 | 临时试用、调试 | 关窗口即停 | ★★☆☆☆ |
| C. Linux cron / systemd | 有云服务器 / NAS | 7x24 | ★★★★★ 生产首选 |
| D. Docker | 有容器环境，想干净隔离 | 7x24 | ★★★★★ |

> 部署前请先在本地跑通：`python main.py check` → `python main.py test-mail` → `python main.py run --dry-run`。

---

## 通用前置步骤

```bash
# 1. 取到代码
cd scirobot

# 2. 建虚拟环境
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # Linux / macOS

# 3. 装依赖
pip install -r requirements.txt

# 4. 初始化
python main.py init

# 5. 编辑 config.yaml，填写 SMTP 账号 / 授权码 / 收件人 /（推荐）LLM Key

# 6. 自检
python main.py check
python main.py test-mail
python main.py run --dry-run
```

**首次部署强烈建议先回填历史**，否则第一封邮件会把库里所有存量文献一次性推给你：

```bash
python main.py backfill --start 2026-01-01 --end 2026-08-01
```

回填的文献会入库并直接标记为「已推送」，之后只推真正的新文献。

---

## A. Windows 计划任务（推荐本地使用）

### A1. 一键注册

在项目目录下打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
# 自定义时间：
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time "07:30"
```

脚本会注册一个名为 `SciRobot` 的每日任务，调用 `run_daily.bat`。

### A2. 验证与管理

```powershell
Start-ScheduledTask   -TaskName SciRobot   # 立即执行一次
Get-ScheduledTaskInfo -TaskName SciRobot   # 查看上次运行结果
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall  # 卸载
```

`LastTaskResult = 0` 表示执行成功。详细日志看 `logs\bot.log`。

### A3. 手动方式（不想用脚本）

1. `Win + R` → `taskschd.msc` 打开任务计划程序
2. 右侧「创建基本任务」→ 名称随便填 → 触发器选「每天」→ 时间 08:30
3. 操作选「启动程序」，程序填 `D:\company\scirobot\run_daily.bat`，**起始于**填 `D:\company\scirobot`（这一项必须填，否则相对路径会出错）
4. 完成后在任务属性里勾选「**如果错过计划开始时间，请尽快启动任务**」——这样早上开机晚了也能补跑

### A4. 注意

- 计划任务只在**电脑开机且已登录**时触发（勾选「不管用户是否登录都要运行」可以后台跑，但需要保存账户密码）。
- 笔记本请在任务属性「条件」页取消勾选「只有在计算机使用交流电源时才启动此任务」。

---

## B. Windows 常驻脚本（调试用）

双击 `run_scheduler.bat`，进程会一直挂着，按 `config.yaml` 里 `scheduler.run_at` 的时间每天触发。关闭窗口即停止。

想开机自启：`Win + R` → `shell:startup` → 把 `run_scheduler.bat` 的快捷方式丢进去。

---

## C. Linux 服务器（生产推荐）

### C1. 部署代码

```bash
sudo mkdir -p /opt/scirobot && sudo chown $USER:$USER /opt/scirobot
cd /opt/scirobot
# 上传或 git clone 代码到这里

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python main.py init
vim config.yaml          # 填配置
./.venv/bin/python main.py check
./.venv/bin/python main.py test-mail
```

确认时区是北京时间：

```bash
sudo timedatectl set-timezone Asia/Shanghai
date
```

### C2. 方式一：cron（最简单）

```bash
crontab -e
```

加一行（每天 08:30 执行）：

```cron
30 8 * * * cd /opt/scirobot && ./.venv/bin/python main.py run >> logs/cron.log 2>&1
```

多个时间点：

```cron
30 8,18 * * * cd /opt/scirobot && ./.venv/bin/python main.py run >> logs/cron.log 2>&1
```

检查是否生效：`crontab -l`，日志看 `logs/bot.log`。

### C3. 方式二：systemd（进程常驻，自动拉起）

```bash
sudo cp deploy/scirobot.service /etc/systemd/system/
sudo vim /etc/systemd/system/scirobot.service   # 改 User / WorkingDirectory / ExecStart 路径
sudo systemctl daemon-reload
sudo systemctl enable --now scirobot
```

常用命令：

```bash
systemctl status  scirobot     # 查看状态
journalctl -u scirobot -f      # 实时日志
sudo systemctl restart scirobot
```

> cron 和 systemd 二选一即可，别同时开，否则一天会推两次。
> cron 更省资源（跑完就退出），systemd 更好观测（一直在，日志集中）。

---

## D. Docker 部署

### D1. 准备

```bash
cp config.example.yaml config.yaml
vim config.yaml         # 填配置（密钥也可以留空，用环境变量注入）
mkdir -p data logs reports
```

### D2. 启动

```bash
docker compose up -d --build
docker compose logs -f
```

### D3. 用环境变量注入密钥（推荐，不把密码写进文件）

编辑 `docker-compose.yml`：

```yaml
environment:
  TZ: Asia/Shanghai
  LLM_API_KEY: "sk-xxxx"
  SMTP_PASSWORD: "授权码"
  MAIL_TO: "a@example.com,b@example.com"
```

### D4. 手动跑一次（不等定时）

```bash
docker compose exec scirobot python main.py run
docker compose exec scirobot python main.py stats
```

### D5. 数据持久化

`data/`（数据库）、`logs/`、`reports/` 已通过 volume 挂载到宿主机，重建容器不会丢历史，去重记录也不会失效。

---

## 运维清单

### 日常检查

```bash
python main.py stats        # 文献总数 / 已推送 / 待推送 / 缓存条数 / 最近一次推送
tail -n 100 logs/bot.log    # 最近日志
ls -lt reports/ | head      # 最近生成的日报
```

数据库可以用 [DB Browser for SQLite](https://sqlitebrowser.org/) 直接打开 `data/literature.db` 查看。

### 备份

只需要备份两样东西：

```bash
cp config.yaml            /backup/          # 配置（含密钥）
cp data/literature.db     /backup/          # 历史文献 + 去重记录 + 翻译缓存
```

数据库开启了 WAL 模式，建议停机或在任务空档期复制；`reports/` 可随时按日报重新生成，不备份也行。

### 升级代码

```bash
git pull                                   # 或覆盖代码文件
pip install -r requirements.txt            # 依赖有变动时
python main.py check                       # 确认配置仍然有效
# Docker: docker compose up -d --build
# systemd: sudo systemctl restart scirobot
```

`config.yaml` 和 `data/` 都在 `.gitignore` 里，升级不会被覆盖。

---

## 故障排查

| 现象 | 排查方向 |
| --- | --- |
| 定时没触发 | 服务器时区是否 `Asia/Shanghai`；Windows 计划任务是否勾了「错过就尽快启动」；`crontab -l` 是否存在 |
| `SMTPAuthenticationError` | 密码必须是**授权码**不是登录密码；QQ 邮箱需先在设置里开启 SMTP 服务 |
| `Connection refused` / 超时 | 云服务器常封禁 25 端口，请用 465(SSL) 或 587(STARTTLS)；检查安全组出站规则 |
| 翻译全失败 | `python main.py check` 看可用后端；国内环境请配 `translate.proxy` 或改用大模型 / 百度后端 |
| 一直「命中 0 条」 | 正常现象（窄领域）。用 `python main.py search --days 30` 验证链路；确认服务器能访问 `eutils.ncbi.nlm.nih.gov` |
| 收到重复文献 | 检查 `data/literature.db` 是否被误删或未持久化（Docker 未挂载 volume 是最常见原因） |
| PubMed 429 报错 | 降低 `pubmed.max_results`，或申请 NCBI API Key 填进 `pubmed.api_key`（限速从 3/s 提到 10/s） |
| 中文日志乱码 | Windows 控制台执行 `chcp 65001`；批处理脚本已内置该命令 |

### 快速自检脚本

```bash
python main.py check                 # 配置
python search.py                     # PubMed 连通性 + 解析
python translate.py                  # 翻译后端连通性
python database.py                   # 数据库读写
python main.py test-mail             # 推送链路
python main.py run --dry-run         # 全链路（不发邮件）
```

六步全绿即可上线。
