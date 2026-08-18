#!/bin/zsh
# 让 QwenPaw 在登录后自动启动并保持运行，供每日热点 Cron 使用。
set -euo pipefail

LABEL="ai.qwenpaw.xhs-hotspot-collector"
USER_ID="$(id -u)"
PROJECT_DIR="/Users/xinyijiang/QwenPaw"
SOURCE_PLIST="${PROJECT_DIR}/deploy/launchd/${LABEL}.plist"
TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/.qwenpaw/logs"
cp "${SOURCE_PLIST}" "${TARGET_PLIST}"
launchctl bootout "gui/${USER_ID}" "${TARGET_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/${USER_ID}" "${TARGET_PLIST}"
launchctl kickstart -k "gui/${USER_ID}/${LABEL}"
launchctl print "gui/${USER_ID}/${LABEL}"
