# 安装

[返回 README](../README.md)

JAV Pilot 以 Docker 镜像 [`drdon1234/jav-pilot`](https://hub.docker.com/r/drdon1234/jav-pilot) 发布，只提供 `linux/amd64` 版本。推荐用 Docker Compose 部署；想修改代码时可以从源码构建。Windows 用户请在 WSL2 中操作。

## 使用 Docker Compose 部署（推荐）

### 选择配置文件

| 配置文件 | 包含的服务 | 适合 |
| --- | --- | --- |
| [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | JAV Pilot | 已有 qBittorrent，或只用 Web 下载 |
| [`deploy/docker-compose.qbittorrent.yml`](../deploy/docker-compose.qbittorrent.yml) | JAV Pilot 和 qBittorrent | 还没有 qBittorrent |

两者都使用 `latest` 版本的镜像，配置模板共用 [`deploy/.env.example`](../deploy/.env.example)。

### 部署

新建一个目录，下载配置文件（统一保存为 `docker-compose.yml`）和配置模板。使用带 qBittorrent 的配置时，把第一个链接中的文件名换成 `docker-compose.qbittorrent.yml`：

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/deploy/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/deploy/.env.example
```

`.env` 中必须填写两项：

- `JAV_PILOT_AUTH_PASSWORD`：登录密码，至少 12 个字符。
- `JAV_PILOT_AUTH_SECRET`：会话密钥，至少 32 个随机字符。修改它会让已登录的设备重新登录。

下面的命令生成密钥，并把运行身份设为当前用户；密码请用编辑器填写：

```bash
sed -i "s/^JAV_PILOT_AUTH_SECRET=.*/JAV_PILOT_AUTH_SECRET=$(openssl rand -hex 32)/" .env
sed -i "s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

启动并查看状态，`jav-pilot` 显示 `healthy` 后即可访问 `http://<主机 IP>:8766`：

```bash
docker compose up -d
docker compose ps
```

缺少必填项时 `docker compose` 会直接报错并说明缺少哪一项。第一次启动需要拉取约 1.1 GB 的镜像。

请始终通过 Compose 启动，不要直接 `docker run` 镜像：Compose 配置提供了 MissAV 浏览器需要的虚拟显示、目录准备和安全限制。

### 目录与运行身份

容器以 `.env` 中的 `PUID:PGID` 读写文件，应当是宿主机上拥有影片目录的账号（用 `id -u`、`id -g` 查看），不要用 0（root）。默认所有目录都在部署目录下，可以在 `.env` 中改到 NAS 的共享目录：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `JAV_PILOT_LIBRARY_HOST_PATH` | 媒体库：整理后的影片、NFO 和封面 | `./media/JAV` |
| `JAV_PILOT_QB_STAGING_HOST_PATH` | qBittorrent 下载的暂存目录 | `./downloads/jav` |
| `JAV_PILOT_WEB_DOWNLOAD_HOST_PATH` | Web 下载的临时目录 | `./downloads/jav-web` |
| `JAV_PILOT_DATA_HOST_PATH` | 设置、记录和数据库 | `./data` |

每次启动时，一次性服务 `jav-pilot-init` 会把 Docker 新建的空目录交给 `PUID:PGID`。已经有文件的目录不会被修改；它们不属于该账号时，`docker compose logs jav-pilot-init` 会给出提示，请自行调整权限，不要递归修改整个媒体库的所有者。

### 同时部署 qBittorrent

带 qBittorrent 的配置使用 [linuxserver/qbittorrent](https://docs.linuxserver.io/images/docker-qbittorrent/) 镜像，与 JAV Pilot 使用同一个 `PUID:PGID`，并以相同的容器路径挂载下载和媒体库目录，因此不需要做路径映射。启动后：

1. 查看 qBittorrent 生成的临时密码：`docker compose logs qbittorrent | grep -i password`。
2. 打开 `http://<主机 IP>:8080`，用户名 `admin`，用临时密码登录，在“工具 → 选项 → WebUI”中设置自己的密码。
3. 在 JAV Pilot 的“设置 → qBittorrent”中填写地址 `http://qbittorrent:8080`、用户名 `admin` 和新密码；暂存目录与整理目录保持默认的 `/downloads/jav`、`/media/JAV`。

请先完成第 2 步再填写地址：临时密码每次重启都会变化，JAV Pilot 连续登录失败会让 qBittorrent 将它封禁一小时。qBittorrent 的端口和配置目录可在 `.env` 的 qBittorrent 部分修改。

### 更新

```bash
docker compose pull
docker compose up -d
```

默认跟随 `latest`；需要固定版本时，把 `.env` 中的 `JAV_PILOT_IMAGE` 改成例如 `drdon1234/jav-pilot:0.0.1`。发布说明提到部署配置有变化时，重新下载 `docker-compose.yml`，`.env` 保持不变。更新前的备份方法见[备份与运维](operations.md#备份与恢复)。

## 从源码构建

需要 Git、Python 3 和 Docker Compose v2。源码仓库根目录的 `docker-compose.yml` 在本机构建镜像，也是[发布工具](operations.md#发布工具)使用的配置。

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
python3 tools/prepare_compose.py
docker compose build
python3 tools/prepare_compose.py --browser-volume
docker compose up -d --no-build
```

`prepare_compose.py` 生成私有的 `.env`（含随机登录密码和会话密钥）、数据与媒体目录以及共享维护锁，已有文件会保留并校验。它默认使用当前非 root 用户的 UID:GID；必须由 root 准备时，传入 `--uid <服务 UID> --gid <共享 GID>`。影片目录同样由上表中的变量设置，默认位于项目的 `runtime/` 下。第一次构建需要下载浏览器和依赖并编译 FFmpeg。

`--browser-volume` 用本地镜像初始化空的浏览器数据卷；已有卷的权限不一致时会拒绝修改，请按提示核对，不要删除卷来绕过。

这种方式默认只监听本机。需要局域网访问时，把 `.env` 中的 `JAV_PILOT_COMPOSE_LAN_BIND` 改成主机的局域网 IP（例如 `192.168.1.10`），再执行 `docker compose up -d --no-build`。想用发布镜像代替本地构建时，在 `.env` 中设置 `JAV_PILOT_IMAGE_REFERENCE=drdon1234/jav-pilot:<版本>`，并用 `docker compose pull` 代替 `docker compose build`。

更新时执行 `git pull`、`docker compose build`、`docker compose up -d --no-build`。

## 远程访问

Compose 部署默认在所有网卡上监听 8766 端口，局域网设备可以直接访问；不要把这个端口直接映射到公网。需要从外网访问时，请通过 HTTPS 反向代理：

- 把反向代理的地址写入 `JAV_PILOT_TRUSTED_PROXY_CIDRS`，只有这些来源才能提供转发的主机名和协议头。
- 保持 `JAV_PILOT_ALLOW_INSECURE_REMOTE=0`；未配置完整登录时，服务拒绝在非本机地址上启动。

通过普通 HTTP 访问时，浏览器不允许网页直接写入剪贴板，复制磁链会改为展开完整文本供手动复制。

## 不使用 Docker

需要 Python 3.11 以上（推荐 3.12）、Node.js 24 和 npm。Linux / macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
jav-pilot serve --host 127.0.0.1 --port 8766
```

Windows PowerShell 中用 `python -m venv .venv` 和 `.venv\Scripts\Activate.ps1` 创建并激活虚拟环境，其余命令相同。

这种方式只监听本机、默认不需要登录，数据保存在项目下的 `data/`。它不包含 FFmpeg 和浏览器所需的全部系统库：Web 下载默认关闭，安装 FFmpeg 后设置 `JAV_PILOT_WEB_DOWNLOAD_ENABLED=1` 启用；在没有桌面的 Linux 上运行 MissAV 浏览器需要 Xvfb。需要登录时设置 `JAV_PILOT_AUTH_ENABLED=1`、`JAV_PILOT_AUTH_USERNAME`、`JAV_PILOT_AUTH_PASSWORD` 和 `JAV_PILOT_AUTH_SECRET` 环境变量。

## 启动失败时

先看 `docker compose logs jav-pilot` 和 `http://<主机 IP>:8766/readyz` 中未通过的检查项，再按对应项检查目录权限、磁盘余量或 qBittorrent 连接。常见情况见[常见问题](faq.md)。
