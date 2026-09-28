<p align="center">
  <img src="docs/images/banner.svg" alt="JAV Pilot" width="100%">
</p>

<div align="center">

<b>简体中文</b> ｜
<a href="docs/i18n/README_zh-TW.md">繁體中文</a> ｜
<a href="docs/i18n/README_en.md">English</a> ｜
<a href="docs/i18n/README_ja.md">日本語</a> ｜
<a href="docs/i18n/README_fr.md">Français</a> ｜
<a href="docs/i18n/README_es.md">Español</a> ｜
<a href="docs/i18n/README_ru.md">Русский</a> ｜
<a href="docs/i18n/README_ar.md">العربية</a>

<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/v/drdon1234/jav-pilot?sort=semver&label=docker&color=3567b7" alt="Docker 版本"></a>
<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/pulls/drdon1234/jav-pilot?color=3567b7" alt="Docker 拉取次数"></a>
<img src="https://img.shields.io/badge/platform-linux%2Famd64-3567b7" alt="平台">
<img src="https://img.shields.io/badge/python-3.11+-3567b7" alt="Python 3.11+">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-3567b7" alt="MIT License"></a>
<br>

<a href="docs/guide/getting-started.md">安装指南</a> ｜
<a href="docs/guide/usage.md">使用指南</a> ｜
<a href="docs/guide/faq.md">常见问题</a> ｜
<a href="docs/guide/configuration.md">配置</a> ｜
<a href="https://github.com/drdon1234/JAV-Pilot/issues">问题反馈</a>

</div>

<br>

JAV Pilot 是一款自托管的影片搜索、下载与媒体库管理应用，可部署在 NAS、Linux 服务器或个人电脑上。它聚合多个资料站的搜索结果，通过 qBittorrent 或视频站完成下载，并在下载完成后自动归档到媒体库、生成海报与 NFO 元数据，供 Jellyfin、Emby、Kodi 直接识别。所有配置、记录与媒体文件均保存在本地，支持通过桌面与移动端浏览器访问。

<p align="center">
  <img src="docs/images/search.png" alt="JAV Pilot 搜索结果页" width="92%">
  <br>
  <sub>截图均使用虚构的示例数据。</sub>
</p>

## ✨ 主要功能

1. 🔍 **多站点聚合搜索**：并行查询 JavBus、JavDB、FC2 等资料站，按番号合并同一作品，并保留每项字段的来源。
2. 🧲 **磁链汇总**：展示文件大小、来源与字幕信息，按 info hash 去重；可通过 Jackett 接入 Sukebei、Tokyo Toshokan。
3. ⬇️ **双下载通道**：磁链任务提交至 qBittorrent；Web 下载自动选择画质，并按原片、中文字幕、无码的优先级选择版本。
4. 🔁 **下载查重**：创建任务前核对 qBittorrent 任务、Web 下载任务与媒体库，避免重复下载。
5. 🗂️ **自动整理**：下载完成后按番号归档，生成海报、背景图与 NFO 元数据。
6. 📚 **媒体库管理**：扫描影片目录（包括手动添加的文件），补全缺失的海报与元数据。
7. 🏆 **排行榜**：汇总 JavDB、FANZA、FC2、MGStage 等来源的作品、女优与分类榜单，支持在后台批量解析详情。
8. 🌐 **标题翻译**：内置公共翻译服务，并支持接入 OpenAI 兼容接口、Claude、Gemini、Ollama 等 AI 服务；译文默认使用当前界面语言。
9. 🩺 **站点诊断**：定期检测各站点的可用性，并区分 DNS、连接、TLS、人机验证与页面结构变化等失败原因。
10. 🔔 **通知推送**：下载完成或失败、磁盘空间不足、站点异常时，通过 Webhook、Gotify、Telegram 或 NAS 通知推送。
11. 📱 **多语言响应式界面**：提供简体中文、繁體中文、English、日本語、Français、Español、Русский、العربية 八种界面语言，默认跟随浏览器与系统语言；适配桌面与移动端，支持浅色与深色主题。

## 🚀 快速开始

提供三种安装方式，首次部署建议采用方式一。

| 方式 | 适用场景 | 前置条件 |
| --- | --- | --- |
| [一：Docker 部署（推荐）](#方式一docker-部署推荐) | NAS、Linux 服务器，或 Windows 下的 WSL2 | Docker 与 Docker Compose v2 |
| [二：从源码构建镜像](#方式二从源码构建镜像) | 需要修改源码或自行构建镜像 | Docker、Git |
| [三：不使用 Docker](#方式三不使用-docker) | 没有 Docker 环境，或需要以系统服务方式运行 | Git、Python 3.11+、Node.js 24 |

### 方式一：Docker 部署（推荐）

使用 Docker Hub 上发布的镜像，只需一个 `docker-compose.yml` 与一个 `.env` 文件。

> [!NOTE]
> 镜像仅支持 x86_64（amd64）架构。Windows 用户请在 WSL2 中执行以下步骤。

**1. 选择配置文件**

各配置文件部署的服务如下：

| 配置文件 | 部署的服务 |
| --- | --- |
| `docker-compose.yml` | JAV Pilot |
| `deploy/docker-compose.jackett.yml` | JAV Pilot、Jackett |
| `deploy/docker-compose.qbittorrent.yml` | JAV Pilot、qBittorrent |
| `deploy/docker-compose.full.yml` | JAV Pilot、qBittorrent、Jackett |

qBittorrent 为 BT 下载器，Jackett 为 Sukebei、Tokyo Toshokan 提供 Torznab 接口。一同部署的容器分别命名为 `jav-pilot-qbittorrent` 与 `jav-pilot-jackett`，不会与已有的同类容器冲突。

**2. 下载配置文件与配置模板**

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/.env.example
```

如果第 1 步选择的是 `deploy/` 下的文件，请将第二条命令中的 `main/docker-compose.yml` 替换为对应路径（例如 `main/deploy/docker-compose.full.yml`），保存的文件名仍为 `docker-compose.yml`。

**3. 填写 `.env`**

执行以下命令，生成会话密钥，并将运行身份（`PUID` / `PGID`）设为当前账号：

```bash
sed -i "s/^JAV_PILOT_AUTH_SECRET=.*/JAV_PILOT_AUTH_SECRET=$(openssl rand -hex 32)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

然后编辑 `.env`（例如 `nano .env`）：

- `JAV_PILOT_AUTH_PASSWORD`：登录密码，至少 12 个字符，必填。
- `QBITTORRENT_PASSWORD`：一同部署 qBittorrent 时填写，至少 6 个字符。该密码将作为 qBittorrent WebUI 的密码（用户名 `admin`），JAV Pilot 使用同一凭据自动连接。
- 目录：媒体库默认位于当前目录下的 `media/`。如需使用 NAS 上的其他目录，请修改 `.env` 中的“目录”部分。

**4. 启动服务**

```bash
docker compose up -d
```

首次启动需要拉取约 1.1 GB 的镜像。启动完成后，在浏览器中访问 `http://<主机 IP>:8766`，使用用户名 `admin` 与上一步设置的密码登录。

缺少必填配置时，`docker compose` 会给出明确提示；其他问题请参阅[安装指南](docs/guide/getting-started.md#启动失败时)。qBittorrent 与 Jackett 的详细说明见[安装指南](docs/guide/getting-started.md#同时部署-qbittorrent-或-jackett)与[种子索引](docs/guide/indexers.md)。

### 方式二：从源码构建镜像

在本机构建镜像，适用于需要修改源码的场景。除镜像来源外，其余步骤与方式一相同。

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
docker build -t jav-pilot:local .
cp .env.example .env
```

按方式一第 3 步填写 `.env`，并将其中的 `JAV_PILOT_IMAGE` 设为 `jav-pilot:local`，然后执行 `docker compose up -d`。首次构建需要下载浏览器与依赖并编译 FFmpeg，耗时较长。

该方式默认仅部署 JAV Pilot。如需同时部署 qBittorrent 或 Jackett，请参阅[安装指南](docs/guide/getting-started.md#方式二从源码构建镜像)。

### 方式三：不使用 Docker

JAV Pilot 本身即为 Web 服务，可直接运行于 Linux、macOS 或 Windows。前置条件为 Git、Python 3.11 及以上（推荐 3.12）、Node.js 24 与 npm；Debian / Ubuntu 另需安装 `python3-venv`。以 Linux / macOS 为例：

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
jav-pilot serve
```

启动后访问 `http://127.0.0.1:8766`。默认仅监听本机，无需登录。Windows 下的命令参见[安装步骤](docs/guide/getting-started.md#安装步骤)；局域网访问（需配置登录）、以 systemd 服务运行、媒体库目录与 Web 下载的配置，请参阅[安装指南](docs/guide/getting-started.md#方式三不使用-docker)。

### 更新

**方式一**：在配置文件所在目录执行

```bash
docker compose pull
docker compose up -d
```

**方式二**：在仓库目录执行

```bash
git pull
docker build -t jav-pilot:local .
docker compose up -d
```

**方式三**：在仓库目录执行，完成后重启服务

```bash
git pull
python -m pip install -e .
npm --prefix frontend ci
npm --prefix frontend run build
```

## 🧭 初始配置

1. **检查站点可用性**：在“站点 → 站点诊断”中填写一个确定存在的番号，点击“测试全部站点”。如果站点均无法访问，通常需要配置代理，参见[常见问题](docs/guide/faq.md#所有站点都连不上)。
2. **连接已有的 qBittorrent**（可选）：一同部署的 qBittorrent 已自动连接，可跳过此步。使用已有的 qBittorrent 时，在“设置 → qBittorrent”中填写地址与账号；如果 qBittorrent 与 JAV Pilot 看到的目录路径不一致，请按[使用指南](docs/guide/usage.md#2-连接-qbittorrent可选)配置路径映射。
3. **搜索与下载**：在“搜索”中输入番号并打开作品详情，选择磁链的“下载”或“开始 Web 下载”，然后在“下载”页查看进度。

完整说明参见[使用指南](docs/guide/usage.md)。

## 🌐 支持的来源与集成

| 类型 | 支持 |
| --- | --- |
| 资料与磁链 | JavBus、JavDB、FC2（默认启用）；FANZA、MGS、AVBase、FC2DB、JAVTEN（可在“站点”中启用） |
| 种子索引 | Sukebei、Tokyo Toshokan（通过 Jackett，可与 JAV Pilot 一同部署） |
| 视频下载 | JableTV、SupJav、MissAV |
| 排行榜 | JavDB、FANZA、FC2、MGStage、JavMenu，以及一本道、加勒比等无码厂商官网 |
| 下载器 | qBittorrent |
| 媒体服务器 | Jellyfin、Emby、Kodi（读取 NFO、海报与背景图） |
| AI 翻译 | OpenAI 及兼容接口、Anthropic Claude、Google Gemini、Azure OpenAI、Ollama 等 |
| 通知 | Webhook、Gotify、Telegram、NAS 通知 |

外部站点可能改版、限制访问地区或要求人机验证。各站点提供的数据与当前可用性，参见[来源说明](docs/guide/sources.md)与应用内的“站点 → 站点诊断”。

## 📸 界面

<p align="center">
  <img src="docs/images/detail.png" alt="作品详情" width="49%">
  <img src="docs/images/downloads.png" alt="下载任务" width="49%">
</p>
<p align="center">
  <img src="docs/images/library.png" alt="媒体库" width="64%">
  <img src="docs/images/mobile-search.png" alt="移动端搜索" width="16%">
  <img src="docs/images/mobile-detail.png" alt="移动端作品详情" width="16%">
  <br>
  <sub>作品详情 · 下载任务 · 媒体库 · 移动端</sub>
</p>

## 📖 文档

| 使用 | 开发 |
| --- | --- |
| [安装指南](docs/guide/getting-started.md)：安装方式、目录与远程访问 | [架构](docs/guide/architecture.md) |
| [使用指南](docs/guide/usage.md)：从站点检查到下载与整理 | [HTTP API](docs/guide/api.md) |
| [常见问题](docs/guide/faq.md) · [配置](docs/guide/configuration.md) | [开发与验证](docs/guide/development.md) |
| [来源说明](docs/guide/sources.md) · [种子索引](docs/guide/indexers.md) | [安全说明](SECURITY.md) |
| [备份与运维](docs/guide/operations.md) | |

## 🔒 数据与隐私

设置、记录与数据库保存在部署目录的 `data/` 中，登录凭据保存在 `.env` 中，请勿对外分享。JAV Pilot 仅访问已启用的站点；除此之外，只有以下内容会发送至外部服务：启用标题翻译时的标题文本（发送至公共翻译服务，可关闭）、点击“AI 翻译”时发送至所配置 AI 服务的标题，以及所配置的通知。

## ⚖️ 免责声明

JAV Pilot 仅为工具软件，不提供任何影片、账号或资源，也不保证能够检索或下载特定内容。请仅下载有权获取的内容，并遵守所在地法律法规及各站点的使用条款。

## 📄 许可证

代码采用 [MIT License](LICENSE)。该许可证不授予第三方影片、图片、站点资料或商标的使用权。

## 💬 反馈与支持

问题与建议请通过 [Issues](https://github.com/drdon1234/JAV-Pilot/issues) 提交。欢迎通过 Star 支持本项目。
