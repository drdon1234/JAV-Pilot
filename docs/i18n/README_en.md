<p align="center">
  <img src="../images/banner_en.svg" alt="JAV Pilot" width="100%">
</p>

<div align="center">

<a href="../../README.md">简体中文</a> |
<a href="README_zh-TW.md">繁體中文</a> |
<b>English</b> |
<a href="README_ja.md">日本語</a> |
<a href="README_fr.md">Français</a> |
<a href="README_es.md">Español</a> |
<a href="README_ru.md">Русский</a> |
<a href="README_ar.md">العربية</a>

<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/v/drdon1234/jav-pilot?sort=semver&label=docker&color=3567b7" alt="Docker version"></a>
<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/pulls/drdon1234/jav-pilot?color=3567b7" alt="Docker pulls"></a>
<img src="https://img.shields.io/badge/platform-linux%2Famd64-3567b7" alt="Platform">
<img src="https://img.shields.io/badge/python-3.11+-3567b7" alt="Python 3.11+">
<a href="../../LICENSE"><img src="https://img.shields.io/badge/license-MIT-3567b7" alt="MIT License"></a>
<br>

<a href="../guide/getting-started.md">Installation</a> |
<a href="../guide/usage.md">Usage</a> |
<a href="../guide/faq.md">FAQ</a> |
<a href="../guide/configuration.md">Configuration</a> |
<a href="https://github.com/drdon1234/JAV-Pilot/issues">Issues</a>

</div>

<br>

JAV Pilot is a self-hosted application for searching, downloading and organizing a video library. It runs on a NAS, a Linux server or a personal computer. It aggregates results from multiple metadata sites, downloads through qBittorrent or directly from video sites, and files each completed download into the media library with posters and NFO metadata that Jellyfin, Emby and Kodi recognize directly. All settings, records and media files stay on your own machine, and the web interface works in desktop and mobile browsers.

> [!NOTE]
> The detailed documentation under `docs/` is currently available in Simplified Chinese only. The web interface is available in English and follows your browser and system language by default.

<p align="center">
  <img src="../images/search.png" alt="JAV Pilot search results" width="92%">
  <br>
  <sub>All screenshots use fictional sample data.</sub>
</p>

## ✨ Features

1. 🔍 **Multi-site search**: queries JavBus, JavDB, FC2 and other metadata sites in parallel, merges results for the same work by catalog code (番号), and records the source of every field.
2. 🧲 **Magnet aggregation**: shows file size, source and subtitle information, deduplicated by info hash; Sukebei and Tokyo Toshokan can be added through Jackett.
3. ⬇️ **Two download channels**: magnet tasks are sent to qBittorrent; web downloads select the quality automatically and prefer the original, Chinese-subtitled and uncensored versions in that order.
4. 🔁 **Duplicate check**: before a task is created, qBittorrent tasks, web download tasks and the media library are checked to avoid downloading a work twice.
5. 🗂️ **Automatic organization**: completed downloads are filed by catalog code, with posters, fanart and NFO metadata generated.
6. 💬 **Chinese subtitles**: after a download is filed and its metadata completed, external Chinese subtitles are fetched from Xunlei and SubtitleCat; the best match by catalog code, duration and language is saved next to the video and can be converted to Simplified or Traditional Chinese. The library can fetch missing subtitles in bulk or switch to another candidate.
7. 📚 **Media library**: scans the video directory, including manually added files, and fills in missing posters and metadata.
8. 🏆 **Rankings**: work, actress and genre rankings from JavDB, FANZA, FC2, MGStage and other sources, with batch detail parsing in the background.
9. 🌐 **Title translation**: a built-in public translation service, plus support for AI services such as OpenAI-compatible APIs, Claude, Gemini and Ollama; translations use the interface language by default.
10. 🩺 **Site diagnostics**: checks site availability periodically and distinguishes DNS, connection, TLS, anti-bot challenge and page-structure failures.
11. 🔔 **Notifications**: notifies through Webhook, Gotify, Telegram or NAS notifications when a download completes or fails, disk space runs low, or a site fails.
12. 📱 **Multilingual, responsive interface**: available in 简体中文, 繁體中文, English, 日本語, Français, Español, Русский and العربية, following the browser and system language by default; works on desktop and mobile, with light and dark themes.

## 🚀 Quick start

Three installation methods are available. Option 1 is recommended for a first deployment.

| Method | Use case | Prerequisites |
| --- | --- | --- |
| [Option 1: Docker (recommended)](#option-1-docker-deployment-recommended) | NAS, Linux server, or WSL2 on Windows | Docker and Docker Compose v2 |
| [Option 2: Build from source](#option-2-build-the-image-from-source) | Modifying the source or building your own image | Docker, Git |
| [Option 3: Without Docker](#option-3-without-docker) | No Docker available, or running as a system service | Git, Python 3.11+, Node.js 24 |

### Option 1: Docker deployment (recommended)

Uses the image published on Docker Hub. Only a `docker-compose.yml` and a `.env` file are needed.

> [!NOTE]
> The image supports x86_64 (amd64) only. On Windows, run the following steps inside WSL2.

**1. Choose a Compose file**

Each Compose file deploys the following services:

| Compose file | Services |
| --- | --- |
| `docker-compose.yml` | JAV Pilot |
| `deploy/docker-compose.jackett.yml` | JAV Pilot, Jackett |
| `deploy/docker-compose.qbittorrent.yml` | JAV Pilot, qBittorrent |
| `deploy/docker-compose.full.yml` | JAV Pilot, qBittorrent, Jackett |

qBittorrent is a BitTorrent client; Jackett provides the Torznab interface for Sukebei and Tokyo Toshokan. The bundled containers are named `jav-pilot-qbittorrent` and `jav-pilot-jackett`, so they do not conflict with existing containers of the same kind.

**2. Download the Compose file and the configuration template**

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/.env.example
```

If you chose a file under `deploy/` in step 1, replace `main/docker-compose.yml` in the second command with its path (for example `main/deploy/docker-compose.full.yml`). Keep the saved file name `docker-compose.yml`.

**3. Fill in `.env`**

Run the following command to generate the session secret and set the runtime identity (`PUID` / `PGID`) to the current account:

```bash
sed -i "s/^JAV_PILOT_AUTH_SECRET=.*/JAV_PILOT_AUTH_SECRET=$(openssl rand -hex 32)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

Then edit `.env` (for example with `nano .env`):

- `JAV_PILOT_AUTH_PASSWORD`: login password, at least 12 characters. Required.
- `QBITTORRENT_PASSWORD`: required when qBittorrent is bundled, at least 6 characters. It becomes the qBittorrent WebUI password (user `admin`), and JAV Pilot connects with the same credentials automatically.
- Directories: the media library defaults to `media/` in the current directory. To use another directory on the NAS, edit the “目录” (Directories) section of `.env`.

**4. Start the service**

```bash
docker compose up -d
```

The first start pulls an image of about 1.1 GB. Then open `http://<host IP>:8766` in a browser and sign in as `admin` with the password set in the previous step.

If a required setting is missing, `docker compose` reports it explicitly. For other problems, see the [installation guide](../guide/getting-started.md#启动失败时). For details on qBittorrent and Jackett, see the [installation guide](../guide/getting-started.md#同时部署-qbittorrent-或-jackett) and [torrent indexers](../guide/indexers.md).

### Option 2: Build the image from source

Builds the image locally, for when the source needs to be modified. Apart from the image, all steps are the same as in Option 1.

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
docker build -t jav-pilot:local .
cp .env.example .env
```

Fill in `.env` as in step 3 of Option 1, set `JAV_PILOT_IMAGE` to `jav-pilot:local`, then run `docker compose up -d`. The first build downloads the browser and dependencies and compiles FFmpeg, which takes a while.

This deploys JAV Pilot only. To deploy qBittorrent or Jackett as well, see the [installation guide](../guide/getting-started.md#方式二从源码构建镜像).

### Option 3: Without Docker

JAV Pilot is itself a web service and runs directly on Linux, macOS or Windows. Prerequisites: Git, Python 3.11 or later (3.12 recommended), Node.js 24 and npm; Debian / Ubuntu also need `python3-venv`. On Linux / macOS:

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

Then open `http://127.0.0.1:8766`. By default the service listens on localhost only and requires no login. For the Windows commands, see [installation steps](../guide/getting-started.md#安装步骤). For LAN access (login required), running as a systemd service, the media library directory and web downloads, see the [installation guide](../guide/getting-started.md#方式三不使用-docker).

### Updating

**Option 1**: run in the directory containing the Compose file

```bash
docker compose pull
docker compose up -d
```

**Option 2**: run in the repository directory

```bash
git pull
docker build -t jav-pilot:local .
docker compose up -d
```

**Option 3**: run in the repository directory, then restart the service

```bash
git pull
python -m pip install -e .
npm --prefix frontend ci
npm --prefix frontend run build
```

## 🧭 Initial setup

1. **Check site availability**: open “Sites → Site diagnostics”, enter a catalog code that is known to exist, and click “Test all sites”. If no site is reachable, a proxy is usually required; see the [FAQ](../guide/faq.md#所有站点都连不上).
2. **Connect an existing qBittorrent** (optional): a bundled qBittorrent is already connected, so this step can be skipped. For an existing qBittorrent, enter its address and account under “Settings → qBittorrent”. If qBittorrent and JAV Pilot see different directory paths, configure the path mapping as described in the [usage guide](../guide/usage.md#2-连接-qbittorrent可选).
3. **Search and download**: enter a catalog code under “Search”, open the work details, choose “Download” on a magnet or “Start Web download”, and follow the progress on the “Downloads” page.

See the [usage guide](../guide/usage.md) for the complete walkthrough.

## 🌐 Supported sources and integrations

| Category | Supported |
| --- | --- |
| Metadata and magnets | JavBus, JavDB, FC2 (enabled by default); FANZA, MGS, AVBase, FC2DB, JAVTEN (can be enabled under “Sites”) |
| Torrent indexers | Sukebei, Tokyo Toshokan (through Jackett, which can be deployed with JAV Pilot) |
| Video downloads | JableTV, SupJav, MissAV |
| Rankings | JavDB, FANZA, FC2, MGStage, JavMenu, and the official sites of uncensored studios such as 1Pondo and Caribbeancom |
| Download client | qBittorrent |
| Media servers | Jellyfin, Emby, Kodi (reading NFO, posters and fanart) |
| AI translation | OpenAI and compatible APIs, Anthropic Claude, Google Gemini, Azure OpenAI, Ollama and more |
| Notifications | Webhook, Gotify, Telegram, NAS notifications |

External sites may change their layout, restrict access by region or require anti-bot verification. For what each site provides and its current availability, see [sources](../guide/sources.md) and “Sites → Site diagnostics” in the application.

## 📸 Screenshots

<p align="center">
  <img src="../images/detail.png" alt="Work details" width="49%">
  <img src="../images/downloads.png" alt="Download tasks" width="49%">
</p>
<p align="center">
  <img src="../images/library.png" alt="Media library" width="64%">
  <img src="../images/mobile-search.png" alt="Mobile search" width="16%">
  <img src="../images/mobile-detail.png" alt="Mobile work details" width="16%">
  <br>
  <sub>Work details · Download tasks · Media library · Mobile</sub>
</p>

## 📖 Documentation

The documentation is written in Simplified Chinese.

| Usage | Development |
| --- | --- |
| [Installation guide](../guide/getting-started.md): installation methods, directories and remote access | [Architecture](../guide/architecture.md) |
| [Usage guide](../guide/usage.md): from site checks to downloading and organizing | [HTTP API](../guide/api.md) |
| [FAQ](../guide/faq.md) · [Configuration](../guide/configuration.md) | [Development and verification](../guide/development.md) |
| [Sources](../guide/sources.md) · [Torrent indexers](../guide/indexers.md) | [Security](../../SECURITY.md) |
| [Backup and operations](../guide/operations.md) | |

## 🔒 Data and privacy

Settings, records and databases are stored in `data/` under the deployment directory, and login credentials in `.env`; do not share either of them. JAV Pilot only contacts the sites that are enabled. Beyond that, only the following is sent to external services: title text when title translation is enabled (sent to a public translation service; can be turned off), titles sent to the configured AI service when “AI translate” is clicked, and the configured notifications.

## ⚖️ Disclaimer

JAV Pilot is a software tool. It does not provide any videos, accounts or resources, and does not guarantee that any particular content can be found or downloaded. Only download content you are entitled to obtain, and comply with local laws and the terms of use of each site.

## 📄 License

The code is released under the [MIT License](../../LICENSE). The license grants no rights to third-party videos, images, site data or trademarks.

## 💬 Feedback and support

Please report issues and suggestions through [Issues](https://github.com/drdon1234/JAV-Pilot/issues). Stars are welcome.
