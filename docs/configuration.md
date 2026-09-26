# 配置

[返回 README](../README.md)

## 配置从哪里来

- **环境变量**：部署时在 `.env` 中设置。Compose 部署的全部配置项见 [`deploy/.env.example`](../deploy/.env.example)，从源码构建时见 [`.env.example`](../.env.example)。
- **网页设置**：在界面中保存，写入数据目录。同一项同时存在时，网页中保存的值优先，环境变量只作为初始值。

数据目录（Compose 部署为 `data/`）中的主要文件：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 站点、解析规则、来源优先级、整理规则、默认参数 |
| `app_config.json` | qBittorrent、登录、通知、历史保留策略 |
| `ai_translation.json` | AI 翻译配置（权限 600） |
| `site_diagnostic_codes.json` | 站点诊断使用的番号 |
| `*.sqlite3` | 下载任务、搜索会话、元数据、媒体库索引等 |

配置文件以原子替换方式写入，修改前的版本会保留备份。两个页面同时编辑设置时，后保存的一方会收到冲突提示，草稿不会丢失，可以选择载入最新设置。

## qBittorrent 与媒体目录

| 配置项 | 含义 |
| --- | --- |
| `JAV_PILOT_QB_URL` | JAV Pilot 能访问的 qBittorrent Web API 地址 |
| `JAV_PILOT_QB_CATEGORY` | JAV 下载使用的分类，默认 `jav` |
| `JAV_PILOT_QB_SAVE_PATH` | qBittorrent 看到的暂存目录 |
| `JAV_PILOT_QB_LIBRARY_PATH` | qBittorrent 看到的整理目录；留空时不自动移动 |
| `JAV_PILOT_QB_APP_LIBRARY_PATH` | 同一媒体目录在 JAV Pilot 容器内的路径，Compose 中固定为 `/media/JAV` |
| `JAV_PILOT_QB_STAGING_HOST_PATH` | 暂存目录在宿主机上的路径 |
| `JAV_PILOT_LIBRARY_HOST_PATH` | 媒体库在宿主机上的路径 |

qBittorrent 与 JAV Pilot 看到的路径可以不同，但必须指向同一份宿主机目录；映射示例见[使用指南](usage.md#2-连接-qbittorrent可选)。JAV Pilot 需要媒体库的写权限来生成 NFO 和图片，qBittorrent 需要移动文件的权限。

新任务只进入 JAV 分类和暂存目录。整理器只处理已完成、分类一致且仍在暂存目录中的任务，通过 qBittorrent 移动文件，任务继续做种。

## 整理与命名

整理后的作品按 `<分类>/<番号>/<番号>_<标题>_<年份>.<扩展名>` 放入媒体库；标题或年份缺失时省略对应部分，不会覆盖同名文件。Web 下载的不同版本以 `_原片`、`_中文字幕`、`_无码影片` 区分，海报和背景图使用 `poster.jpg`、`fanart.jpg`。

“整理”页面中的规则按标题或磁链名称，把新的 qBittorrent 任务放进暂存目录下的不同子目录；规则的分类必须与 JAV 下载分类一致，路径只能位于暂存目录内。可以用“测试规则”输入番号和标题，查看会匹配到哪条规则。

## 搜索与下载

- **精确匹配**：只保留番号前缀与输入完全一致的作品；输入前缀时不会混入前缀相近的系列。资料搜索与 Web 搜索都支持。
- **下载查重**：创建任务前按番号检查 qBittorrent 任务、Web 下载任务和媒体库，已下载或正在下载的作品需要确认后才会再次创建。
- **BT 批量导入**：支持 magnet、`thunder://` 和 BTIH，每批最多 50 条，先预览再确认。
- **Web 下载队列**：暂停会保留断点，“重试”从断点继续，“重新开始”放弃断点。同时下载数最多 8 个；“下载”页的队列控制中可以调整并发、限速和允许下载的时段。

“默认参数”集中设置默认搜索站点、结果数量、Web 下载画质上限与版本顺序、元数据自动补全、翻译和搜索记录保留条数。

## 翻译

标题翻译默认开启，通过免费的公共翻译服务完成，只发送标题文本，结果缓存在本地；可以在“默认参数”中关闭。

AI 翻译需要单独配置（“默认参数 → AI 翻译”），只在点击“AI 翻译”按钮时调用，译文显示在原标题下方。支持的服务：

| 协议 | 服务商 |
| --- | --- |
| OpenAI Chat Completions | 自定义 OpenAI 兼容、OpenAI、xAI Grok、DeepSeek、Moonshot / Kimi、阿里云百炼 / 通义千问、智谱 AI / GLM、火山引擎方舟 / 豆包、腾讯混元、百度千帆 / 文心、Mistral AI、Groq、OpenRouter、SiliconFlow、Together AI、Fireworks AI、DeepInfra |
| Azure OpenAI | Azure OpenAI |
| Anthropic Messages | Anthropic Claude |
| Gemini generateContent | Google Gemini |
| Ollama `/api/chat` | Ollama |

- API Key 保存后不会再显示或返回。
- 公网地址必须使用 HTTPS。使用本机或局域网服务（Ollama、LM Studio、自建网关）时需开启“允许本机或局域网地址”；容器里的 `localhost` 不是宿主机，应填写 `http://host.docker.internal:<端口>` 或宿主机的局域网 IP。
- 译文按服务、模型和原文缓存，重复点击不会重复计费；可以设置每日请求上限。

## 代理与浏览器

- `JAV_PILOT_PROXY`：访问站点使用的代理。
- `JAV_PILOT_ALLOW_FAKE_IP_DNS`：仅在 DNS 返回 Fake-IP 地址且没有设置代理时设为 `1`；设置了代理时会自动兼容。
- `JAV_PILOT_BROWSER_CHANNEL`：一般留空，使用镜像自带的浏览器。

MissAV 使用独立的浏览器数据保存站点的信任状态，保存在 Docker 卷 `jav-pilot-browser-profile` 中，不读取你自己的浏览器资料，也不包含在应用备份中。

## 通知

支持 Webhook、Gotify、Telegram 和 NAS 通知，默认关闭，在“设置 → 通知”中配置。通知发送失败不会影响下载。

## 历史记录保留

默认不自动清理历史记录。在“历史”中设置保留天数、时区和执行时间后，会按计划清理下载记录；清理不会删除影片、图片、NFO 或断点文件。
