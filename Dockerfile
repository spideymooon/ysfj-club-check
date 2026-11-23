# 有赞每日自动签到 - 运行镜像
# 基础镜像自带 Python；Chromium 由 playwright install --with-deps 安装（版本与 pip 包严格匹配）。
# 额外提供 Xvfb + x11vnc + noVNC，用于 NAS 上通过浏览器网页完成人工重新登录。
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data \
    HEADLESS=true

# 系统依赖：时区、中文字体（登录页为中文）、VNC 远程登录组件
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata curl procps \
        xvfb x11vnc xauth novnc websockify \
        fonts-noto-cjk \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && playwright install --with-deps chromium

COPY app /app/app
COPY scripts /app/scripts

RUN chmod +x /app/scripts/*.sh \
    && mkdir -p /app/data/logs /app/data/browser_profile

# 运行期数据（SQLite / 日志 / 浏览器 Profile / 抓包模板）全部落在这个卷
VOLUME ["/app/data"]
EXPOSE 6080

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["serve"]
