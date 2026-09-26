# 备份与运维

[返回 README](../README.md)

## 健康检查

| 地址 | 说明 |
| --- | --- |
| `/healthz` | 进程是否存活，并返回运行版本 `revision` |
| `/readyz` | 数据库、目录、磁盘余量、已配置的 qBittorrent 和后台服务是否就绪；未就绪时列出失败项 |
| `/metrics` | Prometheus 指标，需要登录 |

容器的健康状态取自 `/readyz`，用 `docker compose ps` 查看，`docker compose logs --tail 100 jav-pilot` 查看最近的日志。站点问题用“站点 → 站点诊断”定位。

需要从命令行检查站点时，可以在容器内运行（会访问真实站点，但不会创建下载任务）：

```bash
docker compose exec jav-pilot python -m jav_pilot.cli smoke-sites --code '<确定存在的番号>' --sites 'javbus,javdb,missav'
```

## 更新

见[安装指南](getting-started.md#更新)。

## 备份与恢复

需要备份的是数据目录（Compose 部署为 `data/`）和 `.env`：前者保存设置、记录和数据库，后者保存登录密码等部署配置。影片不在其中，请另行备份媒体库。

最简单的方法是停止服务后复制：

```bash
docker compose stop
cp -a data data.bak-$(date +%Y%m%d)
cp -a .env .env.bak-$(date +%Y%m%d)
docker compose up -d
```

恢复时停止服务，用备份替换 `data/` 和 `.env` 后再启动。不要在服务运行时复制数据库文件。备份中含有密码和密钥，请妥善保管。

MissAV 的浏览器数据保存在 Docker 卷 `jav-pilot-browser-profile` 中，不在上述备份内；丢失后只需重新通过站点验证。

### 命令行备份工具

从源码构建的部署还可以使用带校验的备份工具。它记录每个文件的权限和 SHA-256，恢复时先校验再逐个原子替换：

```bash
python -m jav_pilot.cli backup create --app-root '<部署目录>' --output '<备份目录>' --revision '<完整提交号>' --kind manual --mark --label before-maintenance
python -m jav_pilot.cli backup verify '<备份目录>'
python -m jav_pilot.cli backup restore '<备份目录>' --app-root '<恢复目录>'
python -m jav_pilot.cli backup media-manifest --media-root '<媒体库>' --output '<manifest.json>'
```

`restore` 默认只展示计划，追加 `--apply` 才会替换文件，建议先恢复到空目录核对。`backup prune --backup-root '<备份根目录>' --release-state-root '<发布状态目录>' --max-unprotected 10 --max-age-days 90` 清理旧备份，同样需要 `--apply`；标记保护的备份和回滚仍需要的备份不会被删除。比当前版本更新的数据不会被旧版本读取；降级时请恢复与目标版本匹配的备份。

## 发布工具

从源码部署到 NAS 时，可以用 `ops/release.py` 通过 SSH 发布：它使用已推送到 `origin/main` 的完整提交，在需要迁移数据时自动创建并校验备份，失败时可以回滚。镜像来源可以是 registry（例如 Docker Hub 上 `drdon1234/jav-pilot` 的 digest 引用），也可以是离线镜像包：

```bash
python tools/image_bundle_manifest.py --revision '<完整提交号>' --sha-tag 'jav-pilot:<完整提交号>' --image-id 'sha256:<镜像 ID>' --platform linux/amd64 --archive '<image.tar.zst>' --output '<manifest.json>'
python ops/release.py --remote '<ssh-target>' --app-root '<部署目录>' --backup-root '<备份根目录>' --state-root '<发布状态目录>' deploy --revision '<完整提交号>' --image-archive '<image.tar.zst>' --image-manifest '<manifest.json>'
python ops/release.py --remote '<ssh-target>' --app-root '<部署目录>' --backup-root '<备份根目录>' --state-root '<发布状态目录>' rollback --tool-revision '<完整提交号>' --release-id '<release-id>'
```

使用 registry 时，用 `--image-reference '<仓库>@sha256:<digest>' --image-platform linux/amd64` 代替离线镜像参数。部署和回滚默认只展示计划，确认后追加 `--apply` 执行。发布后确认容器健康、`/healthz` 中的 `revision` 与目标一致，再检查登录、搜索和下载。

### 共享维护锁

源码仓库的 Compose 配置挂载 `runtime/maintenance-locks`，让备份、恢复、发布与运行中的服务互斥。目录权限为 `0770`、锁文件为 `0660`，由 `tools/prepare_compose.py` 创建。在宿主机上对同一实例执行备份或恢复时，设置 `JAV_PILOT_MAINTENANCE_LOCK_ROOT=<部署目录>/runtime/maintenance-locks`。不要在服务运行时删除或重建锁文件。

## 历史维护

历史记录的自动清理见[配置](configuration.md#历史记录保留)。清理之后如需压缩数据库（`VACUUM`），必须在维护模式下进行：设置 `JAV_PILOT_HISTORY_MAINTENANCE_MODE=1`，并让 `JAV_PILOT_HISTORY_BACKUP_PATH` 指向 `JAV_PILOT_HISTORY_BACKUP_ROOT` 中一份已校验的备份。该功能用于源码仓库的 Compose 配置，其中备份根目录以只读方式挂载在 `/app/backups`。
