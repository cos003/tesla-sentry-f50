# Pi Zero W v1.1 重刷后安装 v2.5.3

适用：Raspberry Pi Zero W v1.1、Raspberry Pi OS 13、32GB 虚拟 U 盘。
脚本依据 `tesla_sentry_f50_chat_summary.md` 中已经成功的部署步骤编写。
刷写系统卡前备份 `/archive`、`/opt/tesla-sentry/teslacam.img`、
`/opt/tesla-sentry/auth.conf`、`/opt/tesla-sentry/cloud.json`、
`/etc/tesla-sentry.conf` 及 `/var/lib/tesla-sentry-cloud/queue.sqlite`。

系统首次启动并连上 Wi-Fi 后，在 Pi 上执行：

```sh
curl -fsSL https://raw.githubusercontent.com/cos003/tesla-sentry-f50/main/scripts/bootstrap-pi-zero-w-v2.5.3.sh -o ~/tsf-bootstrap.sh
sudo sh ~/tsf-bootstrap.sh
```

首次运行会安装软件包、写入 USB Device 模式配置，然后提示重启。重启后再次运行
`sudo sh ~/tsf-bootstrap.sh`。脚本会创建开机 Gadget 初始化服务、下载并固定
`bdc96d8`（v2.5.3）、运行兼容性检查、创建 exFAT 虚拟盘及 `TeslaCam` 目录、
安装项目并检查版本。它不会覆盖已经存在的 `teslacam.img` 和 `auth.conf`。

查看状态：

```sh
systemctl is-active tesla-gadget-init.service tesla-gadget.service tesla-live.service sentry-viewer.service
sudo sh /opt/tesla-sentry/tesla-gadget.sh status
hostname -I
```

安装脚本本身不恢复旧卡数据，也不自动绑定麦特云。要沿用旧云设备身份，需恢复
`cloud.json` 和原队列，并单独安装云上传服务；若没有备份则重新绑定。
后续在网页点击 OTA 更新会升级到仓库的最新版，无法保持 v2.5.3。
