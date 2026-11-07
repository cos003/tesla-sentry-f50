#!/bin/sh
# Optional uploader for a Pi already running Sentry. Does not change USB storage.
set -eu
TSF_CLOUD_DIR=${1:-/opt/tesla-sentry}
case "$TSF_CLOUD_DIR" in /*) ;; *) echo '安装目录必须是绝对路径' >&2; exit 1;; esac
case "$TSF_CLOUD_DIR" in *[!a-zA-Z0-9_./-]*) echo '安装目录包含不支持的字符' >&2; exit 1;; esac
[ "$(id -u)" = 0 ] || { echo '请使用 sudo' >&2; exit 1; }
[ -d /run/systemd/system ] || { echo '需要 systemd' >&2; exit 1; }
TSF_SYSTEMD_VERSION=$(systemctl --version | awk 'NR==1 { print $2 }')
[ "$TSF_SYSTEMD_VERSION" -ge 247 ] || { echo '需要 systemd 247 或更新版本' >&2; exit 1; }
TSF_SOURCE=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TSF_PYTHON=$(command -v python3)
[ -s "$TSF_CLOUD_DIR/cloud.json" ] || { echo '请先执行 sentry-cloud-sync.py enroll 完成绑定' >&2; exit 1; }
# Preserve the installed viewer before enabling its new read-only extractor command.
if [ "${TSF_CLOUD_OTA:-0}" != 1 ] && [ -f "$TSF_CLOUD_DIR/sentry-viewer.py" ] && [ ! -f "$TSF_CLOUD_DIR/sentry-viewer.pre-cloud.py" ]; then
    cp -p "$TSF_CLOUD_DIR/sentry-viewer.py" "$TSF_CLOUD_DIR/sentry-viewer.pre-cloud.py"
fi
if [ "${TSF_CLOUD_OTA:-0}" != 1 ] && [ "$TSF_SOURCE" != "$TSF_CLOUD_DIR" ]; then
install -m 755 "$TSF_SOURCE/sentry-cloud-sync.py" "$TSF_CLOUD_DIR/sentry-cloud-sync.py"
install -m 755 "$TSF_SOURCE/sentry-viewer.py" "$TSF_CLOUD_DIR/sentry-viewer.py"
fi
# Preserve credentials during OTA; initial installation sets their permissions.
if [ "${TSF_CLOUD_OTA:-0}" != 1 ]; then chmod 600 "$TSF_CLOUD_DIR/cloud.json"; fi
cat > /etc/systemd/system/sentry-cloud-sync.service <<EOF
[Unit]
Description=Upload Tesla Sentry metadata to MyTeslaCloud
Wants=network-online.target
After=network-online.target tesla-live.service
StartLimitIntervalSec=0
[Service]
Type=simple
DynamicUser=yes
EnvironmentFile=-/etc/tesla-sentry.conf
LoadCredential=cloud.json:$TSF_CLOUD_DIR/cloud.json
ExecStart=$TSF_PYTHON $TSF_CLOUD_DIR/sentry-cloud-sync.py run --config \${CREDENTIALS_DIRECTORY}/cloud.json
StateDirectory=tesla-sentry-cloud
StateDirectoryMode=0700
Restart=always
RestartSec=30
Nice=10
CPUQuota=40%
MemoryMax=160M
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if [ "${TSF_CLOUD_OTA:-0}" = 1 ]; then exit 0; fi
systemctl enable --now sentry-cloud-sync.service
systemctl restart sentry-cloud-sync.service
echo '上传服务已安装。用 journalctl -u sentry-cloud-sync.service 查看运行状态。'
