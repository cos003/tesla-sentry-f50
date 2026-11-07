# Pi 版本包 OTA

systemd 设备的版本包同时更新查看器、云上传器、独立更新程序和上传服务安装脚本。
发布清单 version.json 使用 bundle_format=1，files 为固定四个文件各自的 SHA256。
不下载或改写 auth.conf、cloud.json、/etc/tesla-sentry.conf、录像镜像、归档和队列数据库。
SHA256 防止下载损坏和镜像版本混用，不是发布者签名；发布仓库及更新源必须受信任。

## 首次从旧版迁移

旧版查看器只会更新自己。因此首次需要两次操作：

1. 设备 /etc/tesla-sentry.conf 设置 TSF_OTA_REPO=cos003/tesla-sentry-f50，重启查看器使更新源生效。
   如存在 TSF_OTA_SOURCES，需同步更新或移除，不能继续指向原上游。
2. GitHub main 发布新版本后，旧版网页点升级，先装入新版查看器。
3. 页面恢复后再次检查更新，显示「需要补齐组件」，再次点升级安装同版本完整组件。

之后每次发布只需一次整包更新。若组件缺失或被改动，同版本也能补齐；
全部组件已匹配时不会重复覆盖备份。没有绑定云同步的设备也能下载上传器，
但不会自动绑定账号或开启上传；绑定和首次服务安装仍按 CLOUD_SYNC.md 操作。

## 安装、回滚与恢复

查看器先按清单哈希下载到安装目录 .ota/stage-*，编译检查后交给独立 systemd 临时单元
tesla-sentry-ota 执行，更新过程不依赖原查看器进程存活。更新器再次校验哈希、
版本号、Python 语法、查看器自检、上传器 CLI 及 shell 语法。

安装前备份整组程序、上传服务单元及版本标记，持久记录事务日志。
只暂停原本正在运行的上传器，逐文件原子替换，更新已有绑定设备的服务单元，
重启查看器，并恢复原本运行的上传器。被手工停用的上传器保持停用。
检查服务运行状态和查看器本机 HTTP 响应，失败恢复整组文件和原服务单元。
这项启动检查不等于云端连通、完整解析性能或长期稳定性测试。

为处理替换期间断电，安装器启用 tesla-sentry-ota-recover.service。
下次开机在查看器/上传器之前检查未完成日志，恢复原文件，再让正常服务启动。
成功后仅保留本次回滚所需备份和当前暂存目录，避免版本包无限累积。
不应手工删除 .ota 中的 journal、backup-*、recovery.py，否则会破坏回滚/开机恢复。

网页的「回滚」恢复上一整组程序及服务文件；绑定和队列保持不变。
无 bundle 清单的老发布源仍使用原单文件更新逻辑。
整包更新要求 root 查看器、systemd、systemd-run，本版不支持 procd 的整包更新。

诊断：

```sh
journalctl -u tesla-sentry-ota -n 60 --no-pager
sudo cat /opt/tesla-sentry/.ota/journal.json
sudo cat /opt/tesla-sentry/.ota/last-error.json
```

若设备断电后未自动恢复，可在本机检查备份后执行：

```sh
sudo python3 /opt/tesla-sentry/.ota/recovery.py --root /opt/tesla-sentry --recover
sudo systemctl restart sentry-viewer.service
```

## 发布

代码定稿、完成检查后执行：

```sh
python3 tools/make_release.py 2.5.0 "新增查看器与云同步上传器整包 OTA"
```

该命令更新 APP_VERSION 和包含四文件哈希的 version.json。
生成清单后不要再修改包内文件；若有修改，需重新生成清单。
仅推送已审核的程序、脚本、版本清单和必要文档，不要把绑定凭据、队列或私人录像加入提交。
本文档不代表代码已推送或设备已升级。
