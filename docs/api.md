# HTTP API

[返回 README](../README.md)

API 与界面同源。启用认证时，受保护接口需要已登录的签名 Cookie；POST 请求校验浏览器来源。动态 JSON 不应缓存。不要把凭据、Cookie、请求头或完整上游 URL 放进公开问题报告。

## 设置快照

```json
{"settings": {"schema_version": 12}, "revision": "<64-character-sha256>"}
```

上例仅说明封装结构，`settings` 实际包含全部站点与整理配置。`GET /api/settings` 取得当前快照；`POST /api/settings` 提交完整配置与 `expected_revision`，成功返回 `{ok, settings, revision}`。缺少或非法修订号为 `400`，过期快照为 `409`；冲突会保留现有配置和本地草稿；重新读取并处理自己的草稿，不能拿新修订号盲目覆盖旧内容。`POST /api/settings/validate` 只校验，不保存。

Torznab 来源配置位于站点的 `torznab` 字段，含 `endpoint`、`pinned_addresses`、`categories` 和仅写入的 `api_key`。读取、校验和保存响应只返回 `api_key_configured`；更新时省略 `api_key` 保留已有密钥，显式空字符串清除。`endpoint` 不接受密钥或查询参数，私网服务必须提供固定 IP。配置出现未识别字段时拒绝覆盖原文件。

来源能力区分 `metadata_search`、`metadata_detail`、`torrent_search` 与 `resource_search`；详情能力不等于关键词发现，资源发现也不授权下载。来源记录的 `field_sources`、`detail_provider`、`detail_identity_verified` 表示字段出处和身份校验；磁链引用的 `reported_seeders`、`reported_leechers`、`reported_at` 是索引观察值，不代表 qB 实时探种结果。元数据审校的重新抓取受站点启用状态和字段能力约束。

## 接口索引

下表用于定位接口，具体字段和状态以 `jav_pilot/api/routing.py`（路由表）、`jav_pilot/api/routes/`、`frontend/src/lib/api.ts` 与 `frontend/src/types.ts` 为准。

| 功能 | 接口 |
| --- | --- |
| 健康与指标 | `/healthz`、`/readyz`、`/metrics` |
| 运行配置 | `/api/config`、`/api/config/qb`、`/api/settings`、`/api/settings/validate` |
| 资料搜索与会话 | `/api/search/sessions`、`/api/search/sessions/action`、`/api/search/sessions/stream`、`/api/search/cancel` |
| 搜索记录 | `/api/search-history`、`/api/search-history/action` |
| 排行榜 | `/api/rankings` |
| 翻译 | `/api/translate` |
| AI 翻译 | `/api/ai-translation`、`/api/ai-translation/config`、`/api/ai-translation/test`、`/api/ai-translate` |
| 作品与图片 | `/api/works`、`/api/covers` |
| 后台详情解析 | `/api/detail-prefetch/batches`、`/api/detail-prefetch/batches/action` |
| Web 资源搜索 | `/api/resource-searches`、`/api/resource-searches/action`、`/api/resource-searches/downloads` |
| qB 任务 | `/api/downloader/status`、`/api/downloads`、`/api/downloads/action` |
| 下载记录查重 | `/api/downloads/history-lookup` |
| Web 下载 | `/api/web-downloads`、`/api/web-downloads/action`、`/api/web-downloads/control`、`/api/web-downloads/queue` |
| Web 批次 | `/api/web-downloads/batches`、`/api/web-downloads/batches/action`、`/api/web-downloads/batches/chains`、`/api/web-downloads/batches/chains/action`、`/api/web-downloads/batches/chains/export` |
| 批量规则 | `/api/web-downloads/batches/rules`、`/api/web-downloads/batches/rules/action` |
| 媒体库 | `/api/library`、`/api/library/action` |
| 元数据审校 | `/api/media-metadata/review`、`/api/media-metadata/review/open`、`/api/media-metadata/review/draft`、`/api/media-metadata/review/refetch`、`/api/media-metadata/review/image`、`/api/media-metadata/review/preview`、`/api/media-metadata/review/publish`、`/api/media-metadata/review/abandon` |
| 历史生命周期 | `/api/history/status`、`/api/history/preview`、`/api/history/execute`、`/api/history/export`、`/api/history/retention`、`/api/history/vacuum` |
| 通知 | `/api/notifications`、`/api/notifications/config`、`/api/notifications/test`、`/api/notifications/retry` |
| 站点诊断 | `/api/site-diagnostics`、`/api/site-diagnostics/probe` |
| 整理预览 | `/api/organizer/preview` |

搜索流的 `base` 与 `done` 事件带 `skipped`（来源 ID → 原因），说明哪些已选来源不适用于本次查询，例如仅支持 FC2 番号或未开启解析磁链；它们不是错误。`/api/resource-searches` 创建时可带 `exact_match`，会话按输入番号前缀过滤后再计入结果上限。`POST /api/media-metadata/action` 接受 `{"action": "complete_all"}`（一键补全）。`/api/site-diagnostics/probe` 接受 `jav_code` 与 `fc2_code`，两者可留空，并会保存供定时诊断使用。

AI 翻译的配置与站点设置分开保存：`GET /api/ai-translation` 返回配置、服务商列表与当日请求次数，其中只有 `api_key_configured`，从不返回 API Key；`POST /api/ai-translation/config` 省略 `api_key` 表示保留已保存的 Key，传空字符串表示清除。`POST /api/ai-translation/test` 可带尚未保存的 `config` 试译一句。`POST /api/ai-translate` 接受 `{"texts": [...], "target": "zh-CN"}`，返回与输入对齐的 `translations` 以及 `cached`、`refused`、`failed` 计数；全部失败时返回非 2xx 与中文 `error`。

## 调用约定

- 搜索进度通过 SSE 或持久会话状态读取。停止读取连接不等于已经取消后台任务；使用对应 action/cancel 接口。
- 分页、筛选只读取已保存结果；续搜、重试和提交是明确操作，详见[架构契约](architecture.md#搜索和提交边界)。
- 队列排序、选择快照、设置保存使用各自 revision，不能混用。冲突应重新读取并让用户复核。
- 历史清理及元数据发布先生成 preview，再携带短期 token 或摘要执行；过期或状态变化必须重新预览。
- 配置中的密码和通知目标是写入型字段，读取仅返回已配置状态。不能从公开配置响应恢复秘密。
- 时间、行数、页数、请求体和并发边界由服务端验证；不要依靠前端表单限制构建自定义客户端。
