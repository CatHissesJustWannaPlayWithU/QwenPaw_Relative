# Base images — override with --build-arg for environments without ACR access.
ARG NODE_IMAGE=agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/node:slim
ARG UV_IMAGE=agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/uv:latest

# -----------------------------------------------------------------------------
# Stage 1: alias the uv binary so COPY --from can reference a build-arg image.
FROM ${UV_IMAGE} AS uv-src

# -----------------------------------------------------------------------------
# 阶段 2：轻量级生产运行环境。
#
# QwenPaw 网页控制台和小红书热点插件不依赖容器内桌面、Xvfb 或 Chromium。
# 移除浏览器自动化依赖后，2 GiB ECS 才能稳定完成构建并运行。
# 以后确实需要浏览器自动化时，再为该能力单独部署一个带浏览器依赖的工作节点。
# -----------------------------------------------------------------------------
FROM ${NODE_IMAGE}

# ENV variables
ENV NODE_ENV=production
ENV WORKSPACE_DIR=/app
ENV QWENPAW_WORKING_DIR=/app/working
ENV QWENPAW_SECRET_DIR=/app/working.secret
ENV QWENPAW_BACKUP_DIR=/app/working.backups

# Channel filtering: use QWENPAW_DISABLED_CHANNELS (exclusion, recommended)
# or QWENPAW_ENABLED_CHANNELS (whitelist). Override at runtime with -e.
ARG QWENPAW_DISABLED_CHANNELS="imessage"
ENV QWENPAW_DISABLED_CHANNELS=${QWENPAW_DISABLED_CHANNELS}
ARG QWENPAW_ENABLED_CHANNELS=""
ENV QWENPAW_ENABLED_CHANNELS=${QWENPAW_ENABLED_CHANNELS}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends --fix-missing \
    curl  \
    python3  \
    python3-pip  \
    python3-venv \
    build-essential  \
    libssl-dev  \
    git  \
    supervisor  \
    gettext-base \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get clean
WORKDIR ${WORKSPACE_DIR}

RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"

COPY pyproject.toml setup.py README.md ./
# 已在开发机验证的前端静态文件随 src 一起复制，避免小规格服务器编译 Vite 前端。
COPY src ./src
COPY website/public/docs/ ./src/qwenpaw/docs/
# Speed up Python package installation with uv.
COPY --from=uv-src /uv /bin/uv
RUN uv pip install --no-cache-dir . && rm -rf ./build


# QwenPaw app port (default 8088). Override at runtime with -e QWENPAW_PORT=3000.
ENV QWENPAW_PORT=8088

COPY deploy/config/supervisord.conf.template /etc/supervisor/conf.d/supervisord.conf.template
COPY --chmod=755 deploy/entrypoint.sh /entrypoint.sh

EXPOSE 8088

CMD ["/entrypoint.sh"]
