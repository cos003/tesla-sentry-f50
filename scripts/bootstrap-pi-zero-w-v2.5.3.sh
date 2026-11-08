#!/bin/sh
# Fresh Raspberry Pi OS 13 install for the Pi Zero W v1.1 documented in the setup summary.
# Run as root; if the dwc2 overlay is newly added, reboot and run this script again.
set -eu

REPO=https://github.com/cos003/tesla-sentry-f50.git
COMMIT=bdc96d8
SOURCE=/opt/tesla-sentry-source-v2.5.3
GADGET=/sys/kernel/config/usb_gadget/tesla

fail() { echo "错误：$*" >&2; exit 1; }
note() { echo "==> $*"; }
[ "$(id -u)" -eq 0 ] || fail '请用 sudo sh 运行'
[ -r /proc/device-tree/model ] || fail '不是可识别的 Raspberry Pi'
MODEL=$(tr -d '\000' </proc/device-tree/model)
case "$MODEL" in *'Raspberry Pi Zero W Rev 1.1'*) ;; *) fail "此脚本只适用于 Pi Zero W v1.1；检测到：$MODEL";; esac
[ -f /etc/os-release ] || fail '无法识别系统'
. /etc/os-release
[ "$VERSION_ID" = 13 ] || fail "此脚本按 Raspberry Pi OS 13 编写；当前为 ${VERSION_ID:-未知}"
command -v apt-get >/dev/null || fail '需要 apt-get'

note '安装系统依赖'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 exfatprogs dosfstools util-linux

BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
[ -f "$BOOT_CONFIG" ] || fail '找不到 config.txt'
if ! grep -Eq '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral([[:space:]]*(#.*)?)?$' "$BOOT_CONFIG"; then
    cp -p "$BOOT_CONFIG" "$BOOT_CONFIG.tsf-pre-gadget"
    printf '\n# Tesla Sentry Pi Zero W USB Device mode\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$BOOT_CONFIG"
    note "已写入 $BOOT_CONFIG。现在重启 Pi，然后再次运行本脚本。"
    exit 0
fi
[ -n "$(ls /sys/class/udc 2>/dev/null)" ] || fail '没有 UDC；请确认已重启，并检查 Pi Zero 的 USB 数据口'
modprobe libcomposite
modprobe usb_f_mass_storage || true
modprobe exfat || true
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

note '配置开机创建 USB Gadget'
cat > /usr/local/sbin/tesla-gadget-init.sh <<'EOF'
#!/bin/sh
set -eu
modprobe libcomposite
modprobe usb_f_mass_storage || true
modprobe exfat || true
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
G=/sys/kernel/config/usb_gadget/tesla
mkdir -p "$G"
printf '0x1d6b' > "$G/idVendor"
printf '0x0104' > "$G/idProduct"
printf '0x0200' > "$G/bcdUSB"
printf '0x0100' > "$G/bcdDevice"
mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409"
printf 'TSF000001' > "$G/strings/0x409/serialnumber"
printf 'Tesla Sentry' > "$G/strings/0x409/manufacturer"
printf 'TeslaCam Storage' > "$G/strings/0x409/product"
printf 'TeslaCam' > "$G/configs/c.1/strings/0x409/configuration"
printf '250' > "$G/configs/c.1/MaxPower"
EOF
chmod 755 /usr/local/sbin/tesla-gadget-init.sh
cat > /etc/systemd/system/tesla-gadget-init.service <<'EOF'
[Unit]
Description=Initialize Tesla USB Gadget configfs
After=local-fs.target
Before=tesla-gadget.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tesla-gadget-init.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tesla-gadget-init.service

note '下载并固定项目 v2.5.3'
if [ -d "$SOURCE/.git" ]; then
    git -C "$SOURCE" fetch origin "$COMMIT"
else
    [ ! -e "$SOURCE" ] || fail "$SOURCE 已存在且不是 Git 仓库"
    git clone "$REPO" "$SOURCE"
fi
git -C "$SOURCE" checkout --detach "$COMMIT"
[ "$(git -C "$SOURCE" rev-parse --short HEAD)" = "$COMMIT" ] || fail '源码版本不匹配'

# On a disconnected USB port, `not attached` is a valid gadget state.
python3 - "$SOURCE/scripts/tesla-gadget.sh" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
old = 'if [ "$st" != "configured" ] && [ "$st" != "addressed" ]; then'
new = 'if [ "$st" != "configured" ] && [ "$st" != "addressed" ] && [ "$st" != "not attached" ] && [ "$st" != "powered" ] && [ "$st" != "default" ]; then'
if old in s:
    p.write_text(s.replace(old, new, 1))
elif new not in s:
    raise SystemExit('无法识别 tesla-gadget.sh 状态检查，请勿继续安装')
PY

note '检查兼容性并安装 32GB 虚拟 U 盘'
sh "$SOURCE/scripts/tsf-check.sh"
IMAGE=/opt/tesla-sentry/teslacam.img
if [ ! -e "$IMAGE" ]; then
    note '创建 32GB exFAT 虚拟盘及 TeslaCam 目录'
    mkdir -p /opt/tesla-sentry
    truncate -s 32G "$IMAGE"
    mkfs.exfat -n TESLACAM "$IMAGE"
    MOUNT_DIR=$(mktemp -d)
    if ! mount -o loop "$IMAGE" "$MOUNT_DIR"; then
        rmdir "$MOUNT_DIR"
        fail '虚拟盘挂载失败；镜像已创建，未执行项目安装'
    fi
    mkdir -p "$MOUNT_DIR/TeslaCam"
    sync
    umount "$MOUNT_DIR"
    rmdir "$MOUNT_DIR"
fi
sh "$SOURCE/install.sh" --size 32 --port 8080
mkdir -p /etc/systemd/system/tesla-gadget.service.d
cat > /etc/systemd/system/tesla-gadget.service.d/override.conf <<'EOF'
[Unit]
Requires=tesla-gadget-init.service
After=tesla-gadget-init.service
EOF
systemctl daemon-reload
systemctl restart tesla-gadget.service
python3 /opt/tesla-sentry/sentry-viewer.py --ota-check
note '完成。请用 hostname -I 查 IP，再访问 http://<IP>:8080/'
note '虚拟盘已格式化为 exFAT，并已创建 TeslaCam 目录。'
