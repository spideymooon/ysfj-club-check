#!/bin/sh
# 容器入口：准备运行期目录后交给 CLI
set -eu

: "${DATA_DIR:=/app/data}"
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/browser_profile"

echo "[entrypoint] TZ=${TZ:-Asia/Shanghai}  DATA_DIR=$DATA_DIR  CMD=$*"
exec python -m app.main "$@"
