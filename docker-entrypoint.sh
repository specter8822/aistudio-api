#!/bin/sh
set -e

DATA="${AISTUDIO_DATA_DIR:-/app/data}"
CONFIG_FILE="${AISTUDIO_CONFIG_FILE:-$DATA/config.yaml}"
REMOTE="r2:${R2_BUCKET}/aistudio"
EXCL="--exclude accounts/*/profile/**"

mkdir -p "$DATA"

# 1) 启动时从 R2 恢复
if [ -n "$RCLONE_CONFIG_R2_TYPE" ]; then
  rclone copy "$REMOTE" "$DATA" $EXCL || echo "[backup] restore failed"
fi

# 2) 没有配置则用模板
if [ ! -f "$CONFIG_FILE" ] && [ -f /app/defaults/config.yaml ]; then
  cp /app/defaults/config.yaml "$CONFIG_FILE"
fi

# 3) 每 60 秒备份一次
if [ -n "$RCLONE_CONFIG_R2_TYPE" ]; then
  (
    while true; do
      sleep 60
      rclone copy "$DATA" "$REMOTE" $EXCL || true
    done
  ) &
fi

exec "$@"
