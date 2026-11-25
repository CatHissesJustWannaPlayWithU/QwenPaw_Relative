#!/bin/zsh
# 让 QwenPaw 在登录后自动启动并保持运行，供每日热点 Cron 使用。
set -euo pipefail

LABEL="ai.qwenpaw.xhs-hotspot-collector"
USER_ID="$(id -u)"
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
SOURCE_PLIST="${PROJECT_DIR}/deploy/launchd/${LABEL}.plist"
TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/.qwenpaw/logs"
# Render absolute paths for the current checkout and account. plistlib handles
# characters such as spaces and ampersands without producing invalid XML.
python3 - "${SOURCE_PLIST}" "${TARGET_PLIST}" "${PROJECT_DIR}" "${HOME}" <<'PY'
import plistlib
import sys

source, target, project_dir, user_home = sys.argv[1:]
with open(source, "rb") as handle:
    payload = plistlib.load(handle)

def render(value):
    if isinstance(value, str):
        return value.replace("__PROJECT_DIR__", project_dir).replace("__USER_HOME__", user_home)
    if isinstance(value, list):
        return [render(item) for item in value]
    if isinstance(value, dict):
        return {key: render(item) for key, item in value.items()}
    return value

with open(target, "wb") as handle:
    plistlib.dump(render(payload), handle)
PY
launchctl bootout "gui/${USER_ID}" "${TARGET_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/${USER_ID}" "${TARGET_PLIST}"
launchctl kickstart -k "gui/${USER_ID}/${LABEL}"
launchctl print "gui/${USER_ID}/${LABEL}"
