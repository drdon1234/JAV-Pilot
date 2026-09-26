# 架构与数据契约

[返回 README](../README.md)

## 组件

| 目录或包 | 职责 |
| --- | --- |
| `frontend/` | React、TypeScript、Vite；页面按路由加载 |
| `api/` | HTTP 服务与鉴权：`routing.py` 为路由表，`routes/` 按领域划分路由组，`services/` 管理后台服务的生命周期，`state.py` 集中进程级单例、锁与缓存 |
| `search/`、`indexers/` | 资料搜索协调、持久会话与来源解析；`search/resources/` 负责 Web 资源发现、筛选、分页、续搜与选择 |
| `torrent/` | qB API、磁链探测与选择、导入、完成整理和保种 |
| `web_download/` | Web 下载持久队列、worker、来源 provider、断点和归档；`batches/` 为批量任务 |
| `missav/` | MissAV 站点客户端、可信浏览器运行时与 worker |
| `downloads/` | 种子与 Web 下载共用的失败替换、资源恢复和下载历史 |
| `library/` | 本地媒体事实索引与只读查询 |
| `media_metadata/` | 元数据来源、图片与 NFO 发布；`review/` 负责来源证据、人工值、锁定和原子发布 |
| `history/`、`notifications/` | 历史生命周期、保留策略与通知 Outbox |
| `sites/` | 站点解析规则、诊断与冒烟检查 |
| `config/`、`core/`、`net/`、`security/`、`translation/` | 配置与设置；模型、缓存、迁移、可观测性等通用基础；受限出站 HTTP；鉴权与安全基线；翻译 |
| `maintenance/`、`ops/` | 快照、恢复、归档迁移、schema 契约与不可变发布 |
| `deploy/`（仓库根目录） | 使用发布镜像的独立 Compose 配置：仅 JAV Pilot，或附带 qBittorrent |

相对路径均位于 `jav_pilot/`。`cli.py` 是命令行入口；`auth.py`、`backup_restore.py`、`schema_contract.py`、`browser_acceptance.py` 是部署工具跨版本使用的稳定导入路径，只转发到对应包内的实现，不要移除。后端采用有界线程和后台任务管理器；HTTP 请求不会无限制创建浏览器或下载 worker。前端结果筛选和分页优先查询持久会话，不重跑上游搜索。

## 搜索和提交边界

站点按能力集合组织：`metadata_search` 表示关键词资料发现，`metadata_detail` 表示精确番号详情，`torrent_search` 表示种子搜索，`resource_search` 表示 Web 资源发现。能力可以组合；详情专用来源不参与自由关键词发现。同一番号的资料跨站聚合，磁链按 info hash 合并并保留来源。具体来源及默认启用状态集中在 `config/source_catalog.py`。

资料与资源搜索的 `result_limit` 为 1-999。浏览器断开不抹除持久任务；资源搜索在同一 session 保存 revision、页游标及页内进度。失败页原位重试，达到上限或取消后才调整上限续搜，从 `next_page` 恢复，不重新扫描已访问页。

选择基于同一扫描代际及 `source_revision`。扫描仍在运行时，服务端验证已保存的选择和提交边界；请求中的 `idempotency_key` 防止网络重试重复创建意图。选择接口不校验 MissAV 来源、Manifest 或画质，这些由后台下载阶段验证。只有显式提交才创建任务。

资源搜索页拥有批量规则 CRUD，下载页只展示规则。详情预取只接受服务端保存的会话和作品身份，不能由客户端注入任意详情 URL。

## 媒体与资产边界

媒体库页面只读，查询 SQLite 事实索引；后台扫描负责文件系统访问。实际视频流通过受限 `ffprobe` 验证，不能仅凭文件名声称画质。元数据审校保存来源与锁定字段，发布前复核文件身份，执行最小必要备份与原子替换。

qB 与 Web 下载使用分开的暂存根。归档限制在配置媒体根内，禁止路径穿越、符号链接逃逸和覆盖冲突。历史记录清理必须保留去重、provenance 与恢复所需事实。

## 当前 schema

以 `jav_pilot.schema_contract.runtime_schema_contract()` 的输出为准。旧版本的镜像不一定能读取新版本的数据。

| 组件 | 当前 schema |
| --- | ---: |
| `app_config` | 8 |
| `settings` | 12 |
| `web_downloads` | 9 |
| `web_download_batches` | 10 |
| `download_replacements` | 6 |
| `resource_search` | 6 |
| `magnet_selection` | 1 |
| `detail_prefetch` | 2 |
| `metadata_search_sessions` | 3 |
| `media_metadata` | 3 |
| `media_metadata_review` | 6 |
| `notifications` | 3 |
| `site_diagnostics` | 1 |
| `media_library` | 8 |

SQLite 迁移在事务中校验 invariant 并记录 `schema_migrations`；失败回滚。JSON 迁移先在内存完成校验，再备份并原子替换。畸形或未来 schema 不被静默改写。降级必须从匹配目标版本的已验证快照恢复。

Web 队列数据库保存任务状态，不保存 HLS URL、Cookie、Header 或 Token。浏览器站点信任保存在数据根之外的敏感卷，不能把该卷当成普通可公开缓存。

## 外部来源

资料来源的适配器位于 `indexers/`，Web 视频来源位于 `web_download/providers.py` 与 `missav/`，站点的域名、解析器、能力和优先级来自设置（默认值见 `config/source_catalog.py`）。所有适配器遵守同一组约定：

- 作品身份按规范化番号确认，不凭近似标题合并；无合法番号的条目保留来源身份。
- 搜索卡片、详情字段、磁链、图片和媒体捕获分阶段处理，一个阶段失败不抹除其他阶段的结果。
- 真实空结果需要明确的空态证据；挑战页、HTTP 错误、地区限制和结构变化报告为错误。
- HTML、图片、清单、分片、响应体、并发和超时都有上限。
- 校验初始 URL、DNS、每次重定向和最终 origin；内网、loopback、云元数据地址和带凭据的 URL 在任何回退路径中都不放行。媒体主机白名单属于代码中的安全边界，不能通过设置放开。
- MissAV 使用独立的浏览器 profile；临时 manifest URL、Cookie、签名参数和请求头不写入数据库、日志或截图。

字段出处记录在 `field_sources` 中；人工值和锁定字段优先于自动补全。站点配置变化会改变缓存身份，旧解析结果不会被当作当前资料。

## 配置并发

`GET /api/settings` 返回独立 `SettingsSnapshot`：`settings` 是落盘配置，`revision` 是当前内容的 SHA-256 修订号。保存提交 `expected_revision`；检查与写入处于同一配置事务，过期请求返回 `409`。修订号不混入配置 schema。

前端草稿记录加载时修订号与本地编辑计数。保存响应仅替换未继续编辑的草稿；保存期间的新输入保留，下一次保存使用已成功返回的修订号。

## 性能边界

请求体、上游响应、图片、并发浏览器、工作线程、缓存和任务扫描均有边界。前端入口 JS 预算 350 KiB、入口 CSS 100 KiB、单个懒加载 JS 75 KiB、初始资源合计 450 KiB。预算检查读取构建结果，不以压缩后大小替代原始限制。

具体 API 见 [HTTP API](api.md)，各来源的能力见[来源说明](sources.md)，修改适配器的流程见[开发与验证](development.md#站点适配)。
