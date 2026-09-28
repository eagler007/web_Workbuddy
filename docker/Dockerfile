# WorkBuddy 每日助手 —— 自包含容器（签到 + 派猫 + Web 控制台）
# 纯标准库 Python，不需要 pip install；镜像小、构建快、多架构通吃。
FROM python:3.12-alpine

# tini        —— PID 1，正确转发信号、回收僵尸进程
# tzdata      —— 时区（cron 时间点依赖 TZ）
# supercronic —— 容器友好的 cron（单二进制、日志进 stdout、默认禁重叠）
RUN apk add --no-cache tini tzdata supercronic \
 && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone

ENV TZ=Asia/Shanghai \
    DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 只拷贝运行需要的文件（.dockerignore 已排除 .env / 开发残留）
COPY wb_daily.py wb_report.py notify.py usage.py collect_usage.py app.py /app/
COPY deploy/ /app/deploy/
COPY static/ /app/static/

# 把构建时的 commit 写进镜像 —— 飞牛镜像管理只比 tag（我们全用 :latest），
# 不会提示更新；控制台「更新」页靠这个文件和 GitHub HEAD 比对来判断。
ARG WB_REVISION=dev
RUN echo "$WB_REVISION" > /app/WB_REVISION

RUN chmod +x /app/deploy/*.sh \
 && addgroup -g 1000 -S wb \
 && adduser -u 1000 -S -G wb -h /app wb \
 && mkdir -p /data \
 && chown -R 1000:1000 /app /data

USER 1000:1000

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('WEB_PORT','8080')+'/healthz',timeout=4).status==200 else 1)" \
    || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/app/deploy/entrypoint.sh"]
