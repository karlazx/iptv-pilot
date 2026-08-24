#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <nvme-root> <recording-root>" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname -- "$script_dir")
env_file="$project_dir/.env"
nvme_root=${1%/}
recording_root=${2%/}

if [ -s "$env_file" ]; then
  echo "$env_file is not empty; refusing to overwrite existing secrets" >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

db_password=$(openssl rand -hex 24)
mongot_password=$(openssl rand -hex 24)

cp "$project_dir/.env.example" "$env_file"
chmod 600 "$env_file"

sed -i "s|^MONGODB_DATA_PATH=.*|MONGODB_DATA_PATH=$nvme_root/db|" "$env_file"
sed -i "s|^MONGOT_DATA_PATH=.*|MONGOT_DATA_PATH=$nvme_root/db-search|" "$env_file"
sed -i "s|^MODELS_PATH=.*|MODELS_PATH=$nvme_root/models|" "$env_file"
sed -i "s|^RECORDINGS_PATH=.*|RECORDINGS_PATH=$recording_root/recordings|" "$env_file"
sed -i "s|^MEDIA_CACHE_PATH=.*|MEDIA_CACHE_PATH=$recording_root/media-cache|" "$env_file"
sed -i "s|^M3U_DATA_PATH=.*|M3U_DATA_PATH=$recording_root/m3u|" "$env_file"
sed -i "s|^IPTV_API_CONFIG_PATH=.*|IPTV_API_CONFIG_PATH=$recording_root/iptv-api-config|" "$env_file"
sed -i "s|^BACKUP_PATH=.*|BACKUP_PATH=$recording_root/backup|" "$env_file"
sed -i "s|^IPTV_DB_PASSWORD=.*|IPTV_DB_PASSWORD=$db_password|" "$env_file"
sed -i "s|^MONGOT_PASSWORD=.*|MONGOT_PASSWORD=$mongot_password|" "$env_file"
sed -i "s|^MONGODB_URI=.*|MONGODB_URI=mongodb://iptv_user:$db_password@mongodb:27017/iptv_pilot?authSource=iptv_pilot\&replicaSet=rs0|" "$env_file"

echo "configured $env_file"
echo "NVMe data: $nvme_root"
echo "recording data: $recording_root"
