# WorkBuddy 每日助手（Docker）

一个**自包含**的小容器，把 WorkBuddy（腾讯 CodeBuddy 桌面端）的日常自动化装进去：

- **每日签到** —— 到点自动领积分
- **派猫猫旅行** —— 先领奖励、再派新一趟（顺序有讲究，见下）
- **Web 控制台** —— 网页上加账号、测活、手动跑、看报告和日志

不依赖 QD、不依赖青龙、不依赖任何外部数据库。纯 Python 标准库，一个容器搞定。

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
| 报告 | 账号概览 + 「日期 × 账号」签到矩阵 + 双轴走势 + 明细 |
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
| 猫猫状态 | `GET  https://www.workbuddy.cn/v2/activity/growth/buddy/travel/status` |
| 领奖励 | `POST https://www.workbuddy.cn/v2/activity/growth/buddy/travel/claim` |
| 派出去 | `POST https://www.workbuddy.cn/v2/activity/growth/buddy/travel/depart` |

认证头：`Authorization: Bearer <token>` + `X-User-Id: <uid>`。

> 这些接口名不是猜的，是从产品自己的前端包 `growthSpace-CCYzF8bt.js`（仅 3.3 KB）
> 里逐字抄下来的 —— 那是个把整组 API 常量写死的小 chunk。
> 以后再要找新接口，照这个套路：入口页 → 主包列 chunk → 找几 KB 的常量包。

---

## 九、成长中心「连续登录」怎么自动化的

**结论：主要动作能自动，只有补登卡默认不动。**

| 动作 | 是否自动 | 说明 |
|---|---|---|
| 每天「登录」 | ✅ 已被签到覆盖 | 连登定义为「连续登录**且使用** WorkBuddy 的天数」。每天的签到请求带真实 Bearer token，等价于登录并使用 —— **不需要开桌面端**。 |
| 连登奖励兑换 | ✅ 默认自动 | 入门档 7 天 / 进阶档 14 天 / 巅峰档 28 天，**可多档累计，每档每月限兑 1 次**。脚本先读 `redeem/summary` 确认本月没兑过，再对每个已达档位兑一次。 |
| 断登补签 | ⛔ 默认关闭 | 补登卡上限 4 张、永久持有、**不可逆**。默认只在报告里显示余额，不写入。要开就设 `WB_STREAK_MAKEUP=1`。 |
| 任务系统 | ⛔ 未实现 | 「完成任务」多半依赖真实使用行为（如对话 N 次），脚本无法伪造；接受一个做不完的任务无收益。 |

开关都在 `.env`：`WB_GROWTH`（总闸）/ `WB_STREAK_REDEEM` / `WB_STREAK_MAKEUP`。

报告页有「连登状态」区块，显示每个账号的：当前连登天数 / 下一档位 / 补登卡余额 / 本月各档已兑次数。

---

## 十、目录结构

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
