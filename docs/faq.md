# 常见问题

[返回 README](../README.md)

## 所有站点都连不上

诊断停在 DNS 或连接时，通常需要代理。在 `.env` 中设置 `JAV_PILOT_PROXY`，例如代理软件在宿主机的 7890 端口，就填 `http://host.docker.internal:7890`，并在代理软件中允许局域网连接，然后执行 `docker compose up -d`。Clash 等 Fake-IP 模式在设置代理后会自动兼容。

## 容器一直是 unhealthy

访问 `http://<主机 IP>:8766/readyz` 查看未通过的检查项：

- 媒体库、下载目录相关项：`PUID:PGID` 对这些目录没有写权限，`docker compose logs jav-pilot-init` 中会有提示。
- `qbittorrent`：已填写 qBittorrent 地址但连接或登录失败，见下一条。
- 磁盘相关项：目录所在磁盘空间不足。

## qBittorrent 显示未连接

检查 Web API 地址（容器里不能用 `localhost`）、账号密码，以及 qBittorrent 的 WebUI 设置是否限制了访问来源。多次用错误密码登录后，qBittorrent 会暂时封禁 JAV Pilot 的地址（默认一小时），修正密码后需要等待或重启 qBittorrent。

## 下载完成了，但没有移到媒体库

只有分类与“JAV 下载分类”一致、且仍在暂存目录中的已完成任务才会被整理。确认分类正确、“完成整理目录”填的是 qBittorrent 看到的路径，并且 qBittorrent 对媒体库目录有写权限。

## Web 下载失败

视频站经常改版或要求验证。Web 下载会按站点优先级依次尝试并自动重试，仍然失败的记录在“下载 → 失败归档”，可以从那里打开作品详情，换用磁链或稍后重新发起 Web 下载。Web 下载一直在等待时，检查是否全局暂停、是否在允许的时段内，以及磁盘余量。

## 手动放进媒体库的影片没有出现

媒体库每 5 分钟检查一次变化，每天完整扫描一次；也可以点击“媒体库”页右上角的“重建媒体库索引”立即扫描。文件夹名或文件名中需要包含番号，否则会显示为“未识别番号”。

## NFO 或图片写不进媒体库

确认媒体库没有以只读方式挂载，并且 `PUID:PGID` 对作品所在目录有写权限。

## 局域网内其他设备打不开

Compose 部署默认在所有网卡上监听；检查主机防火墙是否放行 8766 端口。从源码构建时默认只监听本机，需要设置 `JAV_PILOT_COMPOSE_LAN_BIND`，见[安装指南](getting-started.md#从源码构建)。

## 报告问题

请附上脱敏的错误信息、JAV Pilot 版本（`http://<主机 IP>:8766/healthz` 返回的 `revision`）和操作步骤。不要上传 `.env`、Cookie、数据库、完整日志或影片。安全问题请按[安全说明](../SECURITY.md)私下报告。
