# 种子索引

[返回 README](../README.md)

Sukebei 和 Tokyo Toshokan 通过 [Jackett](https://github.com/Jackett/Jackett) 的 Torznab 接口接入，为已有作品补充磁链。两个索引分别配置、分别报告失败，不使用 Jackett 的 `all` 聚合端点。

## 部署 Jackett

使用 [`ops/indexers.compose.yml`](../ops/indexers.compose.yml)。它固定镜像版本、关闭容器内自动更新、不对外发布端口，只接入 JAV Pilot 所在的 Docker 网络，JAV Pilot 通过 `jav-pilot-indexers:9117` 访问它。

默认接入 JAV Pilot 的 Compose 网络 `jav-pilot`，并使用固定地址 `172.26.0.250`。这个地址必须位于该网络的网段内且未被占用，可以用 `docker network inspect jav-pilot` 查看网段；可用 `JAV_PILOT_INDEXER_NETWORK`、`JAV_PILOT_INDEXER_IP` 修改，配置目录的所有者由 `JAV_PILOT_INDEXER_UID` / `JAV_PILOT_INDEXER_GID` 设置。已有 Jackett 时请复用，不要重复创建。

在单独的目录中放置该 Compose 文件（命名为 `compose.yml`）和 [`ops/indexers.py`](../ops/indexers.py)，创建仅服务账号可访问的 `config/` 目录，然后：

```bash
docker compose -f compose.yml up -d
python3 indexers.py configure-public --origin http://172.26.0.250:9117 --server-config config/Jackett/ServerConfig.json
python3 indexers.py verify --origin http://172.26.0.250:9117 --server-config config/Jackett/ServerConfig.json --query '<番号或前缀>'
```

`configure-public` 只添加尚未配置的两个索引，不改动已有设置；`verify` 用你提供的番号或前缀（可重复传入 `--query`）检查能力和查询结果，输出中不包含 API key。

需要通过代理访问时，先停止 Jackett，执行 `python3 indexers.py inherit-proxy`（同样传入 `--origin` 和 `--server-config`）复用 JAV Pilot 的代理设置，再重新启动。

## 在 JAV Pilot 中配置

API key 由 Jackett 生成，位于 `config/Jackett/ServerConfig.json` 的 `APIKey` 字段。在“站点”中启用 Sukebei、Tokyo Toshokan，并填写：

| 来源 | Torznab API 地址 | 允许的固定 IP |
| --- | --- | --- |
| Sukebei | `http://jav-pilot-indexers:9117/api/v2.0/indexers/sukebeinyaasi/results/torznab/api` | `172.26.0.250` |
| Tokyo Toshokan | `http://jav-pilot-indexers:9117/api/v2.0/indexers/tokyotosho/results/torznab/api` | `172.26.0.250` |

API key 填在“API 密钥”中，不要放进地址的查询参数，也不要出现在截图或日志中。搜索时需要开启“解析磁链”，种子索引才会参与。

## 升级与停用

升级前核对新镜像的来源和摘要，保留 `config/` 目录。停用时先在 JAV Pilot 中关闭两个来源，再执行 `docker compose -f compose.yml down`；不要删除配置目录。
