# 麦特云 frp 数据接入

本阶段提供手工参数配置和独立客户端服务，不包含麦特云账号登录、设备绑定接口或云端应用改动。
后台通过隧道调用现有 Sentry API，不需要 `/v/` 路径或公开查看器页面。

```text
麦特云后台 → 实例内 127.0.0.1:18080 → frps ⇄ TLS 隧道 ⇄ Pi 上 frpc → 127.0.0.1:8080
```

## 1. 准备云端

每个用户使用独立 frps 实例和随机 token。客户端与服务端选择相同的 frp 版本（本工具要求 >= 0.64.0）。
从 [frp 官方 Releases](https://github.com/fatedier/frp/releases) 获取对应架构程序，并按发布信息校验下载文件。
Pi Zero W v1.1 是 ARMv6，应选支持 ARMv6 的构建，不能使用 ARM64/ARMv7 专用程序；
安装工具会在设备上执行 `frpc --version` 和 `frpc verify`，无法运行时不会安装。

以 [frps.example.toml](frp/frps.example.toml) 为基础配置用户实例：

- 用 `openssl rand -hex 32` 生成该实例的独立 token，替换示例值。
- 配置服务器证书与私钥，证书的 SAN 必须包含客户端使用的 `server_name`。
- `bindPort = 7000` 是客户端连接入口，需通过云端防火墙/端口映射允许 Pi 访问。
- `proxyBindAddr = "127.0.0.1"` 使数据转发端口仅能在实例内部访问；只允许 `remote_port` 对应端口。
- 用 `frps verify -c /etc/frp/frps.toml` 检查，再通过云端现有服务管理方式启动 frps。

`proxyBindAddr` 会影响同一个 frps 的其他 TCP/UDP 代理，因此示例应使用专用 frps，
不要直接覆盖正在承载其他服务的 frps 配置。若麦特云后台在另一个容器内，
回环地址不跨容器，需要调整容器网络并限制来源，不能简单公开转发端口。

测试证书可在云端用以下命令生成（将域名改为实际入口域名）：

```sh
umask 077
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
  -keyout server.key -out server.crt -subj '/CN=frp.example.com' \
  -addext 'subjectAltName=DNS:frp.example.com' \
  -addext 'basicConstraints=critical,CA:TRUE'
```

将证书、私钥放到 frps 配置对应路径。仅将 `server.crt` 通过可信渠道复制到 Pi 作为信任证书，
私钥留在服务器。正式部署可以使用独立 CA 签发服务器证书，便于轮换。

## 2. 准备 Pi 参数

复制 [parameters.example.json](frp/parameters.example.json) 到设备上（真实参数文件不要提交仓库）：

```sh
cp docs/frp/parameters.example.json /tmp/frp-parameters.json
chmod 600 /tmp/frp-parameters.json
```

编辑文件，填写实际值：

| 字段 | 含义 |
| --- | --- |
| `server_addr` | Pi 可访问的 frps 域名或 IP，不带协议/端口 |
| `server_port` | frps 公网连接入口端口，示例为 7000 |
| `server_name` | 证书中的服务器名称，默认等于 `server_addr` |
| `remote_port` | 用户实例内转发端口，示例为 18080，需在 frps 允许范围内 |
| `local_port` | Pi 上 Sentry 服务端口，默认 8080 |
| `device_id` | 此设备标识，只能包含字母、数字、下划线和连字符 |
| `token` | 与该用户 frps 匹配的随机凭据，至少 32 个字符 |

本工具固定 `localIP = "127.0.0.1"`，只创建一个 TCP 代理；不接受任意代理配置或命令。
服务端仍必须限制端口，不能依赖客户端约束。JSON 只作为未来账号参数下发的输入格式，
目前没有自动从麦特云领取参数的功能。

先在本地测试 Sentry API。`curl --user admin` 会提示输入现有 Sentry 密码：

```sh
curl --fail --user admin http://127.0.0.1:8080/api/live
```

保持查看器登录认证开启；frp token 只认证隧道，不代替 API 认证。

## 3. 检查和安装客户端

安装要求：Linux、Python 3.7+、systemd 247+。本阶段不支持 OpenWrt/procd。
将适配设备的官方 `frpc` 解压到 `/tmp/frpc`，授予执行权限，可信 CA 放到 `/tmp/frp-ca.crt`。

先生成配置并用真实 frpc 校验，不改系统服务：

```sh
python3 tools/configure_frp.py \
  --parameters /tmp/frp-parameters.json --ca /tmp/frp-ca.crt \
  --frpc /tmp/frpc --output /tmp/tsf-frp-preview
```

省略 `--frpc` 也能只生成配置，但不会进行 frpc 校验。输出的配置包含 token，权限为 `0600`，
请勿公开输出内容；预览目录内的 service 文件是使用固定安装路径的模板，不能直接启动。

确认参数后安装：

```sh
sudo python3 tools/configure_frp.py \
  --parameters /tmp/frp-parameters.json --ca /tmp/frp-ca.crt \
  --frpc /tmp/frpc --install
```

安装工具校验参数、CA、frpc 版本和配置后，写入：

- `/usr/local/lib/tesla-sentry/frpc`
- `/etc/tesla-sentry/frp/frpc.toml`（root 读取，`0600`）
- `/etc/tesla-sentry/frp/ca.crt`
- `/etc/systemd/system/tesla-sentry-frp.service`

frpc 通过 systemd 临时用户运行，使用 `LoadCredential` 读取配置，不以 root 运行。
服务开机自启，进程退出后 10 秒重启；首次认证失败和断网时由 frpc 继续尝试连接。
安装成功仅表示服务已启动，不表示隧道或 API 已连通。

```sh
systemctl status tesla-sentry-frp.service --no-pager
journalctl -u tesla-sentry-frp.service -n 40 --no-pager
```

更新连接参数或证书时重新执行安装命令，会重启隧道。
成功后删除 `/tmp` 下的参数、预览目录和临时副本；安装工具不会替你删除输入文件。

## 4. 在用户的云实例内验证 API

在运行 frps 的同一网络环境执行：

```sh
curl --fail --user admin http://127.0.0.1:18080/api/live
curl --fail --user admin http://127.0.0.1:18080/api/events
curl --fail --user admin --get http://127.0.0.1:18080/api/telemetry \
  --data-urlencode 'path=/archive/RecentClips/实际目录/实际文件-front.mp4'
```

遥测路径从事件列表的录像 URL 获取：将 `/media/` 前缀替换成 `/archive/`。
视频在 `/media/...`，接口通过后再验证具体录像地址。
`/api/telemetry` 没有 SEI 时返回 `ok: false`，不能据此推断 FSD 未开启。
`frames` 每行索引 0 是片段内偏移秒数，索引 7 是 AP 原始状态；
现有 UI 将 1 映射为 FSD、2 为 AP、3 为 TACC。这些不是实时车辆状态，时间线接入还需要核对片段时间和时区。

当前 Basic 凭据具有原查看器全部权限，并非只读数据凭据。
麦特云后台应保管凭据并仅暴露需要的数据接口，校验用户与实例归属；
不要向浏览器下发 Basic 或 frp 凭据，也不要提供任意 URL/方法的通用转发接口。
独立的只读 API 凭据及云端入库不包含在本阶段。

## 5. 停用与卸载

仅停用隧道：

```sh
sudo systemctl disable --now tesla-sentry-frp.service
```

重新绑定设备或撤销访问时，云端同步更换 token、断开旧连接；仅停止客户端不能撤销已泄露的凭据。
`uninstall.sh` 会停止并删除本工具的服务、程序和连接配置；录像数据保留规则沿用原卸载脚本。
网页整包 OTA 覆盖查看器与云上传器，不安装或升级 frpc；升级隧道程序需再次运行本工具。

配置参考：[frp 客户端](https://gofrp.org/en/docs/reference/client-configures/)、
[服务端](https://gofrp.org/en/docs/reference/server-configures/)、
[TLS 校验](https://gofrp.org/en/docs/features/common/network/network-tls/)。
