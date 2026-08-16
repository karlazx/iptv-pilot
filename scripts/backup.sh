#!/bin/sh
set -eu

backup_root="${IPTV_DATA_ROOT:-/nas/iptv}/backup"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
timestamp="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_root"

mongodump \
  --uri "$MONGODB_URI" \
  --db "${MONGODB_DATABASE:-iptv_manager}" \
  --archive="$backup_root/iptv_${timestamp}.archive.gz" \
  --gzip

find "$backup_root" -type f -name 'iptv_*.archive.gz' -mtime "+$retention_days" -delete
printf 'backup complete: %s\n' "$backup_root/iptv_${timestamp}.archive.gz"

