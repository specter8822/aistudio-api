#!/bin/sh
set -e

DATA="${AISTUDIO_DATA_DIR:-/app/data}"
CONFIG_FILE="${AISTUDIO_CONFIG_FILE:-$DATA/config.yaml}"
REPO_URL="https://oauth2:${GITHUB_TOKEN}@github.com/${BACKUP_REPO}.git"
mkdir -p "$DATA"

BACKUP_ON=0
if [ -n "$BACKUP_REPO" ] && [ -n "$GITHUB_TOKEN" ] && [ -n "$BACKUP_PASSPHRASE" ]; then
  rm -rf /tmp/restore
  if git clone -q --depth 1 "$REPO_URL" /tmp/restore 2>/dev/null; then
    BACKUP_ON=1
    if [ -f /tmp/restore/backup.enc ]; then
      openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE \
        -in /tmp/restore/backup.enc | tar xz -C "$DATA" \
        && echo "[backup] restored" \
        || { echo "[backup] decrypt failed, backup disabled"; BACKUP_ON=0; }
    else
      echo "[backup] no backup yet"
    fi
  else
    echo "[backup] cannot access repo, backup disabled"
  fi
  rm -rf /tmp/restore
fi

if [ ! -f "$CONFIG_FILE" ] && [ -f /app/defaults/config.yaml ]; then
  cp /app/defaults/config.yaml "$CONFIG_FILE"
fi

do_backup() {
  cd "$DATA"
  HASH=$(find . -type f ! -path './accounts/*/profile/*' -exec sha256sum {} + 2>/dev/null | sort -k2 | sha256sum | cut -d' ' -f1)
  [ "$HASH" = "$(cat /tmp/.last_hash 2>/dev/null)" ] && return 0
  rm -rf /tmp/bk && mkdir /tmp/bk
  tar czf - --exclude='./accounts/*/profile' . \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out /tmp/bk/backup.enc
  cd /tmp/bk
  git init -q -b main
  git config user.email backup@local
  git config user.name backup
  git add backup.enc
  git commit -qm backup
  git push -qf "$REPO_URL" main && echo "$HASH" > /tmp/.last_hash && echo "[backup] pushed"
  cd / && rm -rf /tmp/bk
}

if [ "$BACKUP_ON" = "1" ]; then
  (
    while true; do
      sleep 90
      do_backup || echo "[backup] failed"
    done
  ) &
fi

exec "$@"
