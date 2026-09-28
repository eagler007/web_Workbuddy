# WorkBuddy 每日助手

把 WorkBuddy（腾讯 CodeBuddy 桌面端）的日常自动化装进一个**自包含 Docker 容器**：

- **每日签到** —— 到点自动领积分
- **派猫猫旅行** —— 先领奖励、再派新一趟
- **Web 控制台** —— 网页上加账号、测活、手动跑、看报告和日志

纯 Python 标准库，一个容器搞定。不依赖 QD / 青龙 / 外部数据库。

---

## 快速开始

```bash
cd docker
cp .env.example .env
python3 -c "import secrets;print(secrets.token_hex(32))"   # 生成 WEB_SECRET
vi .env                     # 填 WEB_PASSWORD 和 WEB_SECRET
docker build -t wb-daily:latest .
docker compose up -d
```

打开 `http://<主机IP>:18080/` → 登录 → 「账号」页添加你的 WorkBuddy 账号。

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docker/README.md`](docker/README.md) | 完整说明：功能、凭据怎么拿、Web 界面用法、数据存在哪、运维命令、踩坑记录 |
| [`飞牛部署.md`](飞牛部署.md) | 飞牛 fnOS 部署 runbook：从上传到验证的完整步骤 |

---

## 结构

```
.
├── docker/
│   ├── Dockerfile              镜像定义（python:3.12-alpine + tini + tzdata + supercronic）
│   ├── docker-compose.yml      单服务编排（18080 → 8080，data 卷）
│   ├── .env.example            环境变量模板
│   ├── .dockerignore           构建排除（含 .env，绝不进镜像）
│   ├── app.py                  Web 控制台（标准库 ThreadingHTTPServer）
│   ├── wb_daily.py             签到 + 派猫 + 归档（cron 调用）
│   ├── wb_report.py            聚合报告渲染
│   ├── deploy/
│   │   ├── entrypoint.sh       起 supercronic + 前台 Web
│   │   ├── run_daily.sh        签到+派猫+报告（flock 防重叠）
│   │   └── run_report.sh       只渲染报告
│   └── static/style.css
└── 飞牛部署.md
```

---

## 三个要点

1. **账号在网页上加**，存在容器卷 `/data/wb_accounts.json`，Token 全程只显示掩码。
2. **调度和 Web 是两个独立进程**（`supercronic` + `app.py`），签到挂了不影响网页。
3. **数据是挂载卷**，重建镜像/重启容器都不会丢账号和历史。

---

## 安全

- `.env`（含 Web 密码和会话密钥）已在 `.gitignore` 与 `.dockerignore` 中排除，不进仓库也不进镜像
- 容器内 `/data/wb_accounts.json` 含明文 token，权限 600，别外传
- 报告和历史归档里**从不写 token**，只有 UID 掩码
