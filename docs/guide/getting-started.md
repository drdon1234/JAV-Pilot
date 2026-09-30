# 安装

[返回 README](../../README.md)

有三种安装方式，任选其一：

| 方式 | 适合谁 | 需要准备 |
| --- | --- | --- |
| [一：Docker 部署（推荐）](#方式一docker-部署推荐) | 大多数人：NAS、Linux 主机，或 Windows 的 WSL2 | Docker（含 Docker Compose v2） |
| [二：从源码构建镜像](#方式二从源码构建镜像) | 想修改代码、自己构建镜像的人 | Docker、Git |
| [三：不使用 Docker](#方式三不使用-docker) | 主机上没有 Docker，或想作为系统服务直接运行 | Git、Python 3.11 以上、Node.js 24 |

方式一使用 Docker Hub 上的镜像 [`drdon1234/jav-pilot`](https://hub.docker.com/r/drdon1234/jav-pilot)。方式一、二的镜像只支持 `linux/amd64`。

## 方式一：Docker 部署（推荐）

### 选择配置文件

| 配置文件 | 部署的服务 |
| --- | --- |
| [`docker-compose.yml`](../../docker-compose.yml)（仓库根目录） | JAV Pilot |
| [`deploy/docker-compose.jackett.yml`](../../deploy/docker-compose.jackett.yml) | JAV Pilot、Jackett |
| [`deploy/docker-compose.qbittorrent.yml`](../../deploy/docker-compose.qbittorrent.yml) | JAV Pilot、qBittorrent |
| [`deploy/docker-compose.full.yml`](../../deploy/docker-compose.full.yml) | JAV Pilot、qBittorrent、Jackett |

配套容器命名为 `jav-pilot-qbittorrent`、`jav-pilot-jackett`，不会和已有的 qBittorrent、Jackett 容器重名。它们都使用 `latest` 版本的镜像，配置模板共用 [`.env.example`](../../.env.example)。

### 部署

新建一个目录，下载配置文件（统一保存为 `docker-compose.yml`）和配置模板。使用其他配置时，把第一个链接换成 `deploy/` 下对应的文件，例如 `.../main/deploy/docker-compose.full.yml`：

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/.env.example
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

请始终通过 Compose 启动，不要直接 `docker run` 镜像：Compose 配置提供了目录准备所需的启动方式、浏览器需要的共享内存和安全限制。

`.env` 中的所有项都会原样传给 JAV Pilot 容器，[配置](configuration.md)中提到的其他环境变量也可以直接写进 `.env`，修改后执行 `docker compose up -d` 生效。

### 目录与运行身份

容器以 `.env` 中的 `PUID:PGID` 读写文件，应当是宿主机上拥有影片目录的账号（用 `id -u`、`id -g` 查看），不要用 0（root），`PUID=0` 时容器会拒绝启动。默认所有目录都在部署目录下，可以在 `.env` 中改到 NAS 的共享目录：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `JAV_PILOT_LIBRARY_HOST_PATH` | 媒体库：整理后的影片、NFO 和封面 | `./media/JAV` |
| `JAV_PILOT_QB_STAGING_HOST_PATH` | qBittorrent 下载的暂存目录 | `./downloads/jav` |
| `JAV_PILOT_WEB_DOWNLOAD_HOST_PATH` | Web 下载的临时目录 | `./downloads/jav-web` |
| `JAV_PILOT_DATA_HOST_PATH` | 设置、记录和数据库 | `./data` |

每次启动时，`jav-pilot` 容器先以 root 身份把 Docker 新建的空目录交给 `PUID:PGID`，随后切换到 `PUID:PGID` 运行，不保留任何特权。已经有文件的目录不会被修改；它们不属于该账号时，`docker compose logs jav-pilot` 开头会给出提示，请自行调整权限，不要递归修改整个媒体库的所有者。

### 同时部署 qBittorrent 或 Jackett

配套服务使用 [linuxserver/qbittorrent](https://docs.linuxserver.io/images/docker-qbittorrent/) 和 [linuxserver/jackett](https://docs.linuxserver.io/images/docker-jackett/) 镜像，与 JAV Pilot 使用同一个 `PUID:PGID`。qBittorrent 以相同的容器路径挂载下载和媒体库目录，因此不需要做路径映射。

qBittorrent 没有设置初始密码的环境变量。在 `.env` 中填写 `QBITTORRENT_PASSWORD`（至少 6 个字符）后，JAV Pilot 启动时会把它写入 `qBittorrent.conf`（qBittorrent 等 JAV Pilot 健康后才启动，所以首次启动就用上这个密码），JAV Pilot 也默认用它登录（用户名 `admin`），不需要其他步骤。配置文件中已经有密码时不会覆盖；之后要改密码，先在 qBittorrent WebUI 中修改，再到 JAV Pilot 的“设置 → qBittorrent”中更新。

没填 `QBITTORRENT_PASSWORD` 时，qBittorrent 每次启动都使用新的临时密码（`docker compose logs jav-pilot-qbittorrent | grep -i password`）。JAV Pilot 照常运行，在填写密码之前不尝试登录，也不会因此报告未就绪：

1. 打开 `http://<主机 IP>:8080`，用户名 `admin`，用临时密码登录，在“工具 → 选项 → WebUI”中设置自己的密码。
2. 在 JAV Pilot 的“设置 → qBittorrent”中填写这个密码并保存，JAV Pilot 会立即连接。

登录失败时，JAV Pilot 按 5 分钟、30 分钟、1 小时的间隔重试，避免触发 qBittorrent 的封禁；保存新的账号密码后立即重试。qBittorrent 的端口和配置目录可在 `.env` 的 qBittorrent 部分修改。

Jackett 无需任何设置，见[种子索引](indexers.md)。已有 qBittorrent 时，在“设置 → qBittorrent”中填写它的地址即可。

### 更新

```bash
docker compose pull
docker compose up -d
```

默认跟随 `latest`；需要固定版本时，把 `.env` 中的 `JAV_PILOT_IMAGE` 改成例如 `drdon1234/jav-pilot:0.1.0`。发布说明提到部署配置有变化时，重新下载 `docker-compose.yml`，`.env` 保持不变。更新前的备份方法见[备份与运维](operations.md#备份与恢复)。

## 方式二：从源码构建镜像

需要 Git 和 Docker Compose v2。在仓库目录中构建镜像，再用仓库根目录的 `docker-compose.yml` 启动：

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
docker build -t jav-pilot:local .
cp .env.example .env
```

按上文填写 `.env`，并把 `JAV_PILOT_IMAGE` 改成 `jav-pilot:local`，然后执行 `docker compose up -d`。数据、下载和媒体库默认放在仓库目录下，这些目录已被 Git 忽略。第一次构建需要下载浏览器和依赖并编译 FFmpeg。需要代理才能下载依赖时，传入 Docker 的代理构建参数，例如 `docker build --build-arg HTTPS_PROXY=http://127.0.0.1:7890 --network host -t jav-pilot:local .`。

需要一起部署 qBittorrent 或 Jackett 时，使用 `deploy/` 中对应的配置文件，并以仓库目录作为项目目录，数据和 `.env` 仍在仓库目录下，例如：

```bash
docker compose -f deploy/docker-compose.full.yml --project-directory . up -d
```

之后的 `docker compose` 命令（`ps`、`logs`、`down` 等）都要带上这两个参数。

更新时执行 `git pull`、`docker build -t jav-pilot:local .`，再执行 `docker compose up -d`。

## 方式三：不使用 Docker

JAV Pilot 本身就是一个 Web 服务：`jav-pilot serve` 同时提供网页和 API，不依赖 Docker。需要 Python 3.11 以上（推荐 3.12）、Node.js 24 和 npm。

### 安装步骤

Debian / Ubuntu 先安装系统依赖（FFmpeg 只有 Web 下载需要；Xvfb 用于没有桌面的主机）：

```bash
sudo apt install git python3-venv xvfb ffmpeg
```

Ubuntu 软件源中的 Node.js 版本较旧，请从 [nodejs.org](https://nodejs.org/) 安装 Node.js 24。

下面的命令请用将来运行服务的账号执行：Playwright 把浏览器装在当前账号的主目录下，换一个账号运行服务会找不到浏览器。Linux / macOS：

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
```

Windows PowerShell 中用 `python -m venv .venv` 和 `.venv\Scripts\Activate.ps1` 创建并激活虚拟环境，其余命令相同。在没有桌面的 Linux 上，再执行 `sudo .venv/bin/python -m playwright install-deps chromium` 安装浏览器需要的系统库。

请在仓库目录中启动服务：网页从 `frontend/dist` 读取，数据默认保存在启动目录下的 `data/`。在其他目录启动时，用 `JAV_PILOT_STATIC_DIR` 和 `JAV_PILOT_DATA_DIR` 分别指定网页和数据目录。

### 在本机使用

```bash
jav-pilot serve
```

默认监听 `127.0.0.1:8766`，只能在本机访问，不需要登录。

### 作为局域网 Web 服务运行

监听局域网地址（例如 `--host 0.0.0.0`）时必须配置完整的登录，否则服务拒绝启动。这种方式不读取 `.env`，配置通过环境变量提供，常用项如下：

```bash
# 必填：登录密码至少 12 个字符；会话密钥至少 32 个随机字符（openssl rand -hex 32）
JAV_PILOT_AUTH_PASSWORD=
JAV_PILOT_AUTH_SECRET=
# 影片目录（媒体库）。未设置时为 data/media/JAV，该目录不存在时 /readyz 会报告未就绪
JAV_PILOT_MEDIA_LIBRARY_PATH=/srv/media/JAV
JAV_PILOT_MEDIA_METADATA_LIBRARY_PATH=/srv/media/JAV
# 代理（可选）。三项填同一个地址
JAV_PILOT_PROXY=
HTTP_PROXY=
HTTPS_PROXY=
NO_PROXY=127.0.0.1,localhost
```

Web 下载默认关闭，需要先安装 FFmpeg，再设置：

```bash
JAV_PILOT_WEB_DOWNLOAD_ENABLED=1
JAV_PILOT_WEB_DOWNLOAD_STAGING_PATH=/srv/downloads/jav-web
JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH=/srv/media/JAV
```

连接 qBittorrent 时，`JAV_PILOT_QB_APP_LIBRARY_PATH` 是媒体库在这台主机上的路径，未设置时使用 `JAV_PILOT_MEDIA_METADATA_LIBRARY_PATH`，映射规则见[使用指南](usage.md#2-连接-qbittorrent可选)。其他配置项见[配置](configuration.md)。

MissAV 使用有界面的浏览器，在没有桌面的 Linux 上要用 `xvfb-run -a jav-pilot serve --host 0.0.0.0 --port 8766` 启动。

### 作为 systemd 服务

把上面的环境变量写入 `/etc/jav-pilot.env`，权限设为 600（`sudo chmod 600 /etc/jav-pilot.env`），再创建 `/etc/systemd/system/jav-pilot.service`。下面假设仓库位于 `/opt/JAV-Pilot`，由服务账号 `jav` 完成上面的安装，它需要对仓库下的 `data/` 和影片目录有读写权限：

```ini
[Unit]
Description=JAV Pilot
Wants=network-online.target
After=network-online.target

[Service]
User=jav
WorkingDirectory=/opt/JAV-Pilot
EnvironmentFile=/etc/jav-pilot.env
ExecStart=/usr/bin/xvfb-run -a /opt/JAV-Pilot/.venv/bin/jav-pilot serve --host 0.0.0.0 --port 8766
Restart=on-failure
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jav-pilot
journalctl -u jav-pilot -f
```

更新时在仓库目录执行 `git pull`、`python -m pip install -e .`、`npm --prefix frontend ci` 和 `npm --prefix frontend run build`，再 `sudo systemctl restart jav-pilot`。需要从外网访问时同样通过 HTTPS 反向代理，见[远程访问](#远程访问)。

## 远程访问

方式一、二默认在所有网卡上监听 8766 端口，局域网设备可以直接访问；方式三要按[上文](#作为局域网-web-服务运行)设置。不要把这个端口直接映射到公网。需要从外网访问时，请通过 HTTPS 反向代理：

- 把反向代理的地址写入 `JAV_PILOT_TRUSTED_PROXY_CIDRS`，只有这些来源才能提供转发的主机名和协议头。反向代理还要传递 `X-Forwarded-For`，登录限流才会按真实访客地址计数，而不是把所有人都算作代理地址。
- 保持 `JAV_PILOT_ALLOW_INSECURE_REMOTE=0`；未配置完整登录时，服务拒绝在非本机地址上启动。

通过普通 HTTP 访问时，浏览器不允许网页直接写入剪贴板，复制磁链会改为展开完整文本供手动复制。

## 启动失败时

先看日志（Docker 部署为 `docker compose logs jav-pilot`，systemd 服务为 `journalctl -u jav-pilot`）和 `http://<主机 IP>:8766/readyz` 中未通过的检查项，再按对应项检查目录权限、磁盘余量或 qBittorrent 连接。常见情况见[常见问题](faq.md)。
