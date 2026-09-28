# 备份与运维

[返回 README](../../README.md)

## 健康检查

| 地址 | 说明 |
| --- | --- |
| `/healthz` | 进程是否存活，并返回运行版本 `revision` |
| `/readyz` | 数据库、目录、磁盘余量、已配置的 qBittorrent 和后台服务是否就绪；未就绪时列出失败项 |
| `/metrics` | Prometheus 指标，需要登录 |

容器的健康状态取自 `/healthz`，只反映进程是否存活，qBittorrent 掉线或磁盘余量不足不会让容器变为 unhealthy；依赖项是否就绪看 `/readyz`。用 `docker compose ps` 查看，`docker compose logs --tail 100 jav-pilot` 查看最近的日志。站点问题用“站点 → 站点诊断”定位。

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

在宿主机上用源码运行时，还可以使用带校验的备份工具。它记录每个文件的权限和 SHA-256，恢复时先校验再逐个原子替换：

```bash
python -m jav_pilot.cli backup create --app-root '<部署目录>' --output '<备份目录>' --revision '<完整提交号>' --kind manual --mark --label before-maintenance
python -m jav_pilot.cli backup verify '<备份目录>'
python -m jav_pilot.cli backup restore '<备份目录>' --app-root '<恢复目录>'
python -m jav_pilot.cli backup media-manifest --media-root '<媒体库>' --output '<manifest.json>'
```

`restore` 默认只展示计划，追加 `--apply` 才会替换文件，建议先恢复到空目录核对。`backup prune --backup-root '<备份根目录>' --max-unprotected 10 --max-age-days 90` 清理旧备份，同样需要 `--apply`；标记保护的备份不会被删除。比当前版本更新的数据不会被旧版本读取；降级时请恢复与目标版本匹配的备份。

## 历史维护

历史记录的自动清理见[配置](configuration.md#历史记录保留)。清理之后如需压缩数据库（`VACUUM`），必须在维护模式下进行：在 Compose 配置中以只读方式挂载备份根目录（例如 `./backups:/app/backups:ro`），在 `.env` 中设置 `JAV_PILOT_HISTORY_BACKUP_ROOT=/app/backups`、`JAV_PILOT_HISTORY_MAINTENANCE_MODE=1`，并让 `JAV_PILOT_HISTORY_BACKUP_PATH` 指向其中一份已校验的备份。完成后删除这几项并重新执行 `docker compose up -d`。
