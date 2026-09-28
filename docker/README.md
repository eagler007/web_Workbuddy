# WorkBuddy 每日助手（Docker）

一个**自包含**的小容器，把 WorkBuddy（腾讯 CodeBuddy 桌面端）的日常自动化装进去：

- **每日签到** —— 到点自动领积分
- **派猫猫旅行** —— 先领奖励、再派新一趟（顺序有讲究，见下）
- **连登奖励兑换** —— 自动兑换已达档位（入门 7 天 / 进阶 14 天 / 巅峰 28 天）
- **Token / 积分消耗统计** —— 今日消耗、每日走势、每账号明细
- **Web 控制台** —— 网页上加账号、测活、手动跑、看报告和日志

不依赖 QD、不依赖青龙、不依赖任何外部数据库。纯 Python 标准库，一个容器搞定。

> ⚠️ **连登这件事，脚本不能全自动。** 连登判定是「连续登录**且使用**」，
> 「使用」= 你本人发一次对话。脚本负责签到、领积分、兑换奖励、统计消耗、并在
> 报告里标出「今天是否已计入」——但**发起对话这个动作必须你自己做**。
> 详见「九、成长中心『连续登录』怎么自动化的」。

---

## 一、30 秒上手

**镜像已经在 GitHub 上构建好了**（由 GitHub Actions 自动构建推送），飞牛只需要拉取，**不用本地 build，也不用装 Docker 开发环境**。

```bash
# 1. 在飞牛上建部署目录
mkdir -p /vol1/docker/workbuddy
cd /vol1/docker/workbuddy

# 2. 把 docker-compose.yml 放进来
#    方式 A：飞牛 Web → Docker → Compose → 新增项目
#            路径填 /vol1/docker/workbuddy，来源选「创建 docker-compose.yml」，
#            把本目录 docker-compose.yml 的内容粘进去，勾选「创建后立即启动」
#    方式 B：命令行
#            curl -fsSL -o docker-compose.yml \
#              https://raw.githubusercontent.com/eagler007/web_Workbuddy/main/docker/docker-compose.yml

# 3. 准备环境变量
cp .env.example .env
python3 -c "import secrets;print(secrets.token_hex(32))"   # 复制输出
vi .env        # 填 WEB_PASSWORD 和 WEB_SECRET
chmod 600 .env

# 4. 数据目录 + 权限（容器内跑的是 uid 1000）
mkdir -p /vol1/docker/workbuddy/data
chown -R 1000:1000 /vol1/docker/workbuddy/data

# 5. 拉镜像并启动
docker compose pull
docker compose up -d
docker compose logs -f --tail=50
```

打开 **http://<NAS-IP>:18080/** → 输 `WEB_PASSWORD` → 「账号」页加账号。

> 要换端口就改 `docker-compose.yml` 里的 `ports`。

---

## 二、容器里有什么

```
workbuddy 容器
├── tini (PID 1)              信号转发、收僵尸
├── supercronic               按 .env 的 CRON_SCHEDULE 跑任务
│   └── deploy/run_daily.sh   签到 + 派猫 + 归档 + 出报告（flock 防重叠）
└── app.py (前台)             Web 控制台 :8080
    ├── /            总览，含「立即运行」按钮和最近记录
    ├── /accounts    账号增删改 + 测活
    ├── /report      历史报告（每次刷新实时渲染）
    ├── /logs        最近一次运行的输出
    └── /healthz     健康检查
```

调度和 Web 是**两个独立进程**：签到挂了不影响网页，网页重启不影响定时。

---

## 三、怎么拿到 Token 和 UID

账号就两个字段：`token`（accessToken）和 `uid`。

**Token** 存在本机 WorkBuddy 桌面端的数据里：

- **Windows**：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
- **macOS**：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`

文件里 `auth.accessToken` 是 token，`account.uid` 是 uid。

> 新版本把这个文件加密了（值形如 `{"$wbEncrypted":1,"envelope":"..."}`），
> 浏览器 F12 → Network → 任一接口请求头里的 `Authorization: Bearer ...` 和 `X-User-Id`。

Token 有效期约 60 天，过期后签到会返回 401 —— 到「账号」页用同一个 UID 重新提交一次新 token 即可（不用删了重建）。

---

## 四、Web 控制台怎么用

| 页面 | 能干什么 |
|---|---|
| 总览 | 看账号数 / 最近一次 / 归档量；点「全部账号跑一次」或「只跑这一个」 |
| 账号 | 加账号（名称 + UID + Token）、测活、删除；Token 永远只显示掩码 |
| 报告 | 账号概览 + 连登状态 + Token/积分消耗 + 「日期 × 账号」签到矩阵 + 双轴走势 + 明细 |
| 日志 | 最近一次运行的完整输出（已二次脱敏） |

**测活**：保存账号时会自动调一次查余额接口。提示 `有效 · 有效积分合计 N` 就说明 token 没问题；`401 未授权` 就是 token 过期或写错了。

**手动跑**是异步的：点完按钮页面会轮询进度，跑完自动提示，不会卡住浏览器。

---

## 五、数据在哪

宿主目录 `/vol1/docker/workbuddy/data` ↔ 容器内 `/data`：

| 文件 | 作用 |
|---|---|
| `wb_accounts.json` | 账号（**含明文 token**，权限 600，别外传） |
| `wb_history.json` | 历史归档，报告的数据源 |
| `report.html` | 聚合报告（多账号 × 多日期） |
| `report_single.html` | 最近一次运行的快照报告 |
| `wb_daily_raw.log` | 原始接口响应（debug 用） |
| `last_run.log` | 最近一次运行的 stdout |

**换机器/重装**：把 `data/` 整个目录拷过去就行，历史和账号都在。

---

## 六、常用运维

```bash
docker compose logs -f --tail=100          # 看实时日志（含 cron 触发记录）
docker compose restart                     # 重启
docker compose exec workbuddy date         # 【验证时区，必须显示 CST +0800】
docker compose exec workbuddy /app/deploy/run_daily.sh   # 不等定时，手动触发一次
docker compose exec workbuddy /app/deploy/run_report.sh  # 只重渲染报告
docker compose pull && docker compose up -d              # 拉最新镜像并重启
```

**改定时**：编辑 `.env` 里的 `CRON_SCHEDULE`（cron 五段式，如 `0 8,20 * * *`），然后 `docker compose up -d` 重启生效。

**改代码/更新版本**：本地改完 `git push` → GitHub Actions 自动构建新镜像（约 2–4 分钟）→ 飞牛上 `docker compose pull && docker compose up -d`。

---

## 七、踩过的坑（照做能省时间）

1. **时区必须对**。容器时间错 8 小时，cron 就会在错误的时间点跑。部署后务必 `docker compose exec workbuddy date` 确认是 `CST +0800`。
2. **`.env` 里不要设 `WB_ACCOUNTS_JSON`**。它的优先级高于账号文件，一旦设置会把 Web 加的账号全遮蔽掉。
3. **报告文件分了两个**（`report.html` 聚合 / `report_single.html` 单次），别混用，否则互相覆盖。
4. **卷权限**。容器以 uid 1000 跑，宿主 `data/` 目录要 `chown 1000:1000`，否则写报告会失败。
5. **镜像走 GHCR，飞牛不用 build**。飞牛 Web 导入 compose 时**不执行 `build:` 段**，所以本项目把构建放到 GitHub Actions（见 `.github/workflows/docker-publish.yml`），compose 里只写 `image:`。要改代码就 push，等 Actions 构建完再 `docker compose pull`。
6. **签到失败先看响应体**。接口返回 400 且 body 是 `{"code":10001,"msg":"今天已签到，请明天再来"}` 属于**正常**（今天已经签过了），不是故障。
7. **派猫顺序是「先领后派」**。脚本已经按这个顺序写死：`arrived` 才领，领完重读状态，`idle` 且没到当日上限才派。别改成先派。
8. **猫猫出错不影响签到结论**。整条猫链路包在 try/except 里，退出码只看签到。

---

## 八、接口速查

| 用途 | 请求 |
|---|---|
| 查余额 / 积分 | `POST https://www.codebuddy.cn/v2/billing/meter/get-user-resource` |
| 签到 | `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |
| 连登状态 | `GET  https://www.workbuddy.cn/v2/activity/growth/streak` |
| 兑换汇总 | `GET  https://www.workbuddy.cn/v2/activity/growth/redeem/summary` |
| 兑换连登奖励 | `POST https://www.workbuddy.cn/v2/activity/growth/redeem`，body `{"tier":"…","client_token":"…"}` |
| 用补登卡 | `POST https://www.workbuddy.cn/v2/activity/growth/makeup-cards/use`，body `{"target_date":"YYYY-MM-DD"}` |
| **每日用量** | `POST https://www.codebuddy.cn/v2/billing/meter/get-user-daily-usage`，body `{"startTime":"YYYY-MM-DD 00:00:00","endTime":"YYYY-MM-DD 23:59:59","pageNum":1,"pageSize":100}` |
| 请求级明细 | `POST https://www.codebuddy.cn/v2/billing/meter/get-user-request-usage`，参数同上 |
| 猫猫状态 | `GET  https://www.workbuddy.cn/v2/activity/growth/buddy/travel/status` |
| 领奖励 | `POST https://www.workbuddy.cn/v2/activity/growth/buddy/travel/claim` |
| 派出去 | `POST https://www.workbuddy.cn/v2/activity/growth/buddy/travel/depart` |

认证头：`Authorization: Bearer <token>` + `X-User-Id: <uid>`。

> 这些接口名不是猜的，是从产品自己的前端包里挖出来的：
> - 连登/派猫 → `growthSpace-CCYzF8bt.js`（仅 3.3 KB，整组 API 常量写死）
> - 每日用量 → `index-BTO2lsRd.js`（786 KB 主包，用量页是**内联**的，顺着「用量明细」
>   文案挖到 `get-user-daily-usage`）
>
> 以后再要找新接口，照这个套路：入口页 → 主包列 chunk → 找几 KB 的常量包；
> 找不到就回主包里搜**中文 UI 文案**，顺着文案找它旁边的请求调用。

---

## 九、成长中心「连续登录」怎么自动化的

**结论：主要动作能自动，只有补登卡默认不动。**

| 动作 | 是否自动 | 说明 |
|---|---|---|
| 每天「登录」 | ⚠️ 需要你发一次对话 | 连登定义为「连续登录**且使用** WorkBuddy 的天数」。脚本能自动签到领积分，但**「使用」这个动作只有本人能做**——脚本不会替你伪造对话。 |
| 连登奖励兑换 | ✅ 默认自动 | 入门档 7 天 / 进阶档 14 天 / 巅峰档 28 天，**可多档累计，每档每月限兑 1 次**。脚本先读 `redeem/summary` 确认本月没兑过，再对每个已达档位兑一次（`client_token` 用 uuid4 保证幂等）。 |
| 断登补签 | ⛔ 默认关闭 | 补登卡上限 4 张、永久持有、**不可逆**。默认只在报告里显示余额，不写入。要开就设 `WB_STREAK_MAKEUP=1`。 |
| 任务系统 | ⛔ 未实现 | 「完成任务」多半依赖真实使用行为（如对话 N 次），脚本无法伪造；接受一个做不完的任务无收益。 |

### 兑换逻辑（阶段 B）

```
① GET  streak          -> 拿 days / next_tier（判断已达哪些档）
② GET  redeem/summary  -> 拿本月每档已兑次数
③ 对每一档（7d/14d/28d）：已达档 && 本月该档未兑 -> POST redeem {tier, client_token}
④ 不重试：一个档位一次调用，失败就记进报告，下轮/明天再说
```

两条硬纪律：

- **遍历所有已达档位**，不是只兑最高档（多档累计是规则允许的）。
- **读不通就不兑**：`streak` 或 `redeem/summary` 任一读不到，本轮直接跳过兑换 ——
  宁可少兑一次，也不要盲写。

报告页有「连登状态」区块，除天数/档位/补登卡/本月已兑外，还多两列：

- **今日是否计入** —— 跟历史对比连登天数，没涨就是「尚未计入」（这个信号告诉你今天该发对话了）
- **本次兑换** —— 这一轮自动兑换的结果（已兑 / 失败 / 跳过 及其原因）

开关都在 `.env`：`WB_GROWTH`（总闸）/ `WB_STREAK_REDEEM` / `WB_STREAK_MAKEUP`。

---

## 十、Token / 积分消耗统计

报告页有「Token / 积分消耗」区块：今日消耗、窗口合计、日均、每日走势柱状图，
外加每账号明细（今日 / 合计 / 有数据天数 / 迷你走势）。

**口径要说清楚**：CodeBuddy 采用**积分计费**，模型调用按系数扣积分 ——
所以这里统计的是**积分消耗**，不是原始 token 数。接口字段就叫 `credit`。

数据来自 `POST /v2/billing/meter/get-user-daily-usage`，
由 `wb_daily.py` 的 `usage_flow()` 采集，写进归档的 `usage_today` / `usage_days` /
`usage_sum` / `usage_range` 字段，报告侧纯离线渲染（不联网）。

相关的两个坑：

1. **「用量数据存在 2-3 小时的数据延迟」**（官方文案原文）。当天数字偏小或为空属正常，
   脚本不会把「今天为 0」当错误 —— 只有接口整体读不通才算失败。
2. **日期区间最大 31 天**，这是前端硬限制，脚本也按 31 天封顶。

开关：`WB_USAGE=1`（总闸）、`WB_USAGE_DAYS=7`（窗口天数，1~31）。

> 想看**本机**（而不是账号级）的详细用量，另有 `token-dashboard` 技能：
> 它读本机 `~/.workbuddy/projects/**/*.jsonl` 的请求级真实 usage，
> 出的是另一张看板（含模型分布、工作空间下钻、金额估算）。
> 两者互补：本页看**账号级积分**，看板看**本机请求级明细**。

---

## 十一、目录结构

```
docker/
├── Dockerfile                 镜像定义（python:3.12-alpine + tini + tzdata + supercronic）
├── docker-compose.yml         单服务编排（18080 → 8080，data 卷，image 走 GHCR）
├── .env.example               环境变量模板
├── .dockerignore              构建排除（含 .env，绝不进镜像）
├── app.py                     Web 控制台（标准库 ThreadingHTTPServer）
├── wb_daily.py                签到 + 派猫 + 连登采集 + 归档（cron 调用）
├── wb_report.py               聚合报告渲染
├── deploy/
│   ├── entrypoint.sh          起 supercronic + 前台 Web
│   ├── run_daily.sh           签到+派猫+报告（flock 防重叠）
│   └── run_report.sh          只渲染报告
└── static/
    └── style.css              预留（样式目前内联在 app.py）

.github/
└── workflows/
    └── docker-publish.yml     push 后自动构建并推送到 ghcr.io
```
