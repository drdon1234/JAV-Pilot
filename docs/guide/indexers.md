# 种子索引

[返回 README](../../README.md)

Sukebei 和 Tokyo Toshokan 通过 [Jackett](https://github.com/Jackett/Jackett) 的 Torznab 接口接入，为已有作品补充磁链。两个索引分别配置、分别报告失败，不使用 Jackett 的 `all` 聚合端点。

## 内置 Jackett

[`deploy/docker-compose.jackett.yml`](../../deploy/docker-compose.jackett.yml) 和 [`deploy/docker-compose.full.yml`](../../deploy/docker-compose.full.yml) 带有 Jackett（容器 `jav-pilot-jackett`），不需要手动配置：

- JAV Pilot 从只读挂载的 `ServerConfig.json` 读取 Jackett 的 API Key；
- Jackett 没有管理员密码时，JAV Pilot 在后台自动添加 Sukebei 和 Tokyo Toshokan 两个索引；`JAV_PILOT_PROXY` 已填写而 Jackett 未设代理时，一并写入 Jackett 的代理设置；
- “站点”中这两个来源默认启用，Torznab 地址和 API 密钥都留空即表示使用内置 Jackett；
- Jackett 启动或配置完成之前，搜索会跳过这两个来源并注明原因，不影响其他来源，也不影响 `/readyz`。

没有管理员密码的 Jackett 谁能打开 WebUI 谁就能看到 API Key，所以它默认不对外发布 9117 端口，只在 Compose 内部网络中可用。要使用 WebUI，取消 `jav-pilot-jackett` 服务中 `ports` 的注释，执行 `docker compose up -d`，并首先设置管理员密码；设置之后 JAV Pilot 不再改动它的索引。

## 使用其他 Jackett

已有 Jackett 时，在它的 WebUI 中先设置管理员密码，再添加 Sukebei（`sukebeinyaasi`）和 Tokyo Toshokan（`tokyotosho`）两个索引，并复制页面右上角的 API Key。需要代理时在 Jackett 的设置中填写。然后按下面的“在 JAV Pilot 中配置”填写地址和 API Key。

也可以使用独立的 [`ops/indexers.compose.yml`](../../ops/indexers.compose.yml)：它固定镜像版本、不对外发布端口，并以 `jav-pilot-indexers` 这个名字加入已有的 `jav-pilot` 网络。把它（命名为 `compose.yml`）和 [`ops/indexers.py`](../../ops/indexers.py) 放在单独的目录中，然后：

```bash
docker compose -f compose.yml up -d
JACKETT_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' jav-pilot-indexers)
python3 indexers.py configure-public --origin "http://$JACKETT_IP:9117" --server-config config/Jackett/ServerConfig.json
python3 indexers.py verify --origin "http://$JACKETT_IP:9117" --server-config config/Jackett/ServerConfig.json --query '<番号或前缀>'
```

`configure-public` 只添加尚未配置的两个索引；`verify` 用你提供的番号或前缀检查查询结果。

## 在 JAV Pilot 中配置

使用内置 Jackett 时不需要这一步。其他情况下，在 JAV Pilot 的“站点”中启用 Sukebei、Tokyo Toshokan，并填写：

| 来源 | Torznab API 地址 |
| --- | --- |
| Sukebei | `http://<Jackett 地址>:9117/api/v2.0/indexers/sukebeinyaasi/results/torznab/api` |
| Tokyo Toshokan | `http://<Jackett 地址>:9117/api/v2.0/indexers/tokyotosho/results/torznab/api` |

使用 `ops/indexers.compose.yml` 部署时，Jackett 地址是 `jav-pilot-indexers`；Jackett 部署在其他机器上时，填它的局域网地址。“固定 IP”可以留空，JAV Pilot 会在每次请求时解析一次服务名并直接连接解析结果；填写后则只连接这些地址。

API key 填在“API 密钥”中，不要放进地址的查询参数，也不要出现在截图或日志中。搜索时需要开启“解析磁链”，种子索引才会参与。索引结果中指向其他网站的种子链接仍按外部地址处理，只允许访问公网。

## 升级与停用

停用时先在 JAV Pilot 中关闭两个来源，再停止 Jackett；保留它的配置目录，以后可以直接恢复。
