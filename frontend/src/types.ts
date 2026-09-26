export type FilterType = 'select' | 'text'

export interface SettingsSnapshot {
  settings: AppSettings
  revision: string
}

export interface FilterOption {
  label: string
  value: string
}

export interface SiteFilter {
  id: string
  label: string
  type: FilterType
  default: string
  options: FilterOption[]
}

export type ParserRuleAttribute =
  | 'text'
  | 'href'
  | 'src'
  | 'title'
  | 'alt'
  | 'datetime'
  | 'content'
  | 'poster'
  | 'data-src'
  | 'data-href'
  | 'data-url'
  | 'data-original'
  | 'data-lazy-src'

export interface ParserValueRule {
  selector: string
  attributes: ParserRuleAttribute[]
  index?: number
}

export interface SiteParserRules {
  schema_version: number
  search: {
    item_selector: string
    empty_selector: string
    ready_selector: string
    detail_url: ParserValueRule
    title: ParserValueRule
    code: ParserValueRule
    date: ParserValueRule
    rating: ParserValueRule
    cover: ParserValueRule
    magnet_available_selector: string
    default_magnet_hint: 'available' | 'unavailable' | 'unknown'
  }
  detail: {
    fields: {
      title: ParserValueRule
      original_title: ParserValueRule
      release_date: ParserValueRule
      duration: ParserValueRule
      rating: ParserValueRule
      maker: ParserValueRule
      publisher: ParserValueRule
      series: ParserValueRule
      director: ParserValueRule
      actor: ParserValueRule
      tag: ParserValueRule
    }
    images: {
      cover: ParserValueRule
      backdrop: ParserValueRule
      sample: ParserValueRule
    }
    magnets: {
      item_selector: string
      uri: ParserValueRule
      name: ParserValueRule
      size: ParserValueRule
      badges: ParserValueRule
    }
  }
}

export type SiteCapability =
  | 'metadata_search'
  | 'metadata_detail'
  | 'torrent_search'
  | 'resource_search'
  | 'web_download'
  | 'description'

export interface SiteSettings {
  id: string
  name: string
  capabilities: SiteCapability[]
  enabled: boolean
  base_url: string
  parser_profile: 'javbus' | 'javdb' | string
  search?: {
    url_template: string
  }
  filters?: SiteFilter[]
  parser_rules_mode?: 'inherit' | 'custom'
  parser_rules?: SiteParserRules
  torznab?: {
    endpoint: string
    api_key?: string
    api_key_configured?: boolean
    pinned_addresses: string[]
    categories: number[]
  }
}

export type SiteDiagnosticSite = 'javbus' | 'javdb' | 'fc2' | 'fanza' | 'mgs' | 'avbase' | 'fc2db' | 'javten' | 'jable' | 'supjav' | 'missav' | 'kissjav' | 'javnoni'

export type SiteDiagnosticStage =
  | 'configuration'
  | 'dns'
  | 'connection'
  | 'search'
  | 'detail'
  | 'image'
  | 'quality'
  | 'manifest'

export interface SiteDiagnosticStatus {
  site: SiteDiagnosticSite
  stage: SiteDiagnosticStage
  last_checked_at: number
  last_success_at: number | null
  last_latency_ms: number
  consecutive_failures: number
  last_error_code: string | null
}

export interface SiteDiagnosticResult {
  site: SiteDiagnosticSite
  stage: SiteDiagnosticStage
  status: 'ok' | 'failed' | 'deferred'
  ok: boolean
  checked_at: number
  latency_ms: number | null
  error_code: string | null
}

export interface SiteDiagnosticsPayload {
  ok: boolean
  statuses: SiteDiagnosticStatus[]
  codes?: { jav: string; fc2: string }
}

export interface SiteDiagnosticProbePayload extends SiteDiagnosticsPayload {
  results: SiteDiagnosticResult[]
}

export interface OrganizerRule {
  id: string
  name: string
  enabled: boolean
  priority: number
  match: {
    sources: string[]
    title_contains: string[]
    magnet_name_contains: string[]
    code_regex: string
  }
  actions: {
    media_type: 'movie' | 'series' | 'other'
    category: string
    save_path: string
    tags: string
  }
}

export interface WorkflowSearchDefaults {
  site_mode: 'all' | 'custom'
  sources: string[]
  result_limit: number
  page_size: number
  fetch_magnets: boolean
  exact_match: boolean
  search_kind: SearchKind
}

export interface WorkflowResourceSearchDefaults {
  result_limit: number
  exact_match: boolean
  max_height: number
  default_quality: 'highest' | number
  variant_priority: WebDownloadVariant[]
  existing_policy: WebDownloadExistingPolicy
}

export interface WorkflowTranslationDefaults {
  enabled: boolean
  show_original: boolean
}

export type AiTranslationProtocol = 'openai' | 'azure_openai' | 'anthropic' | 'gemini' | 'ollama'

export interface AiTranslationProvider {
  id: string
  label: string
  protocol: AiTranslationProtocol
  default_base_url: string
  default_api_version: string
  requires_api_key: boolean
}

export interface AiTranslationConfig {
  provider: string
  base_url: string
  model: string
  api_version: string
  allow_private_network: boolean
  instructions: string
  daily_limit: number
  api_key_configured: boolean
}

/** Saving: omit api_key to keep the stored key, send '' to clear it. */
export type AiTranslationConfigUpdate = Omit<AiTranslationConfig, 'api_key_configured'> & { api_key?: string }

export interface AiTranslationSnapshot {
  ok: boolean
  config: AiTranslationConfig
  configured: boolean
  missing: string[]
  providers: AiTranslationProvider[]
  usage_today: number
}

export interface AiTranslatePayload {
  ok: boolean
  translations: Array<string | null>
  cached: number
  refused: number
  failed: number
  error: string | null
  code: string | null
}

export interface WorkflowDefaults {
  search: WorkflowSearchDefaults
  resource_search: WorkflowResourceSearchDefaults
  translation: WorkflowTranslationDefaults
  search_history_limit: number
  metadata_auto_fallback: boolean
  metadata_auto_complete: boolean
}

export interface SiteDiagnosticCodes {
  jav: string
  fc2: string
}

export interface AppSettings {
  schema_version: number
  workflow_defaults?: WorkflowDefaults
  site_diagnostic_codes?: SiteDiagnosticCodes
  detail_default_site_id?: string
  metadata_search_site_priority?: string[]
  web_resource_search_provider_priority?: Array<'jable' | 'supjav' | 'missav' | 'kissjav' | 'javnoni'>
  web_download_provider_priority?: Array<'jable' | 'supjav' | 'missav'>
  metadata_scraper_site_priority?: string[]
  sites: SiteSettings[]
  organizer: {
    enabled: boolean
    mode: 'qbittorrent'
    rules: OrganizerRule[]
  }
  _config_error?: string
}

export type DetailPrefetchItemStatus = 'queued' | 'running' | 'completed' | 'failed'

export interface DetailPrefetchItem {
  work_id: string
  status: DetailPrefetchItemStatus
  attempts: number
  error: string | null
}

export interface DetailPrefetchBatch {
  batch_id: string
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed'
  total: number
  queued: number
  running: number
  completed: number
  failed: number
  created_at: number
  updated_at: number
  items?: DetailPrefetchItem[]
}

export interface QbittorrentPublicConfig {
  configured: boolean
  url: string
  username: string
  has_password: boolean
  category: string
  save_path: string
  library_path: string
  app_library_path: string
  tags: string
}

export interface RuntimePayload {
  app: string
  version: string
  config: {
    qbittorrent: QbittorrentPublicConfig
  }
  cache: {
    items: number
    max_items: number
    ttl_seconds: number
    weight?: number
    max_weight?: number
  }
  javdb_fetcher: string
  settings: AppSettings
}

export type NotificationChannel = 'webhook' | 'gotify' | 'telegram' | 'nas'
export type NotificationEventType = 'completed' | 'failed' | 'disk_low' | 'site_failure' | 'test'
export type NotificationDeliveryState = 'pending' | 'inflight' | 'delivered' | 'dead'
export type NotificationDeliveryOutcome = 'delivered' | 'retry' | 'dead' | 'lease_expired' | 'manual_retry'

export interface NotificationChannelPublicConfig {
  enabled: boolean
  target_configured: boolean
  credential_configured: boolean
  private_target: boolean
  pinned_address_count: number
  priority?: number
}

export type NotificationPublicConfig = Record<NotificationChannel, NotificationChannelPublicConfig>

export interface NotificationEvent {
  event_id: string
  event_type?: NotificationEventType
  type?: NotificationEventType
  source: string
  code: string | null
  subject_kind?: string | null
  subject_id?: string | null
  stage?: string | null
  status: string
  error_code: string | null
  occurrence_count: number
  first_occurred_at?: number
  last_occurred_at?: number
  occurred_at?: number
  created_at?: number
}

export interface NotificationDelivery {
  event_id: string
  adapter: NotificationChannel
  state: NotificationDeliveryState
  attempt_count: number
  next_attempt_at: number
  last_error_code: string | null
  last_http_status: number | null
  last_attempt_at: number | null
  delivered_at: number | null
  manual_retry_count: number
  created_at: number
  updated_at: number
}

export interface NotificationDeliveryAttempt {
  event_id: string
  adapter: NotificationChannel
  attempt_number: number
  outcome: NotificationDeliveryOutcome
  error_code: string | null
  http_status: number | null
  attempted_at: number
  next_attempt_at: number | null
}

export interface NotificationListPayload {
  ok: boolean
  events: NotificationEvent[]
  config: NotificationPublicConfig
}

export interface NotificationDetailPayload {
  ok: boolean
  event: NotificationEvent
  deliveries: NotificationDelivery[]
  history: NotificationDeliveryAttempt[]
  config: NotificationPublicConfig
}

export interface NotificationConfigUpdate {
  webhook?: {
    enabled?: boolean
    endpoint?: string
    signing_secret?: string
    private_origin?: string
    pinned_addresses?: string[]
    clear_fields?: Array<'endpoint' | 'signing_secret' | 'private_origin' | 'pinned_addresses'>
  }
  gotify?: {
    enabled?: boolean
    origin?: string
    app_token?: string
    private_origin?: string
    pinned_addresses?: string[]
    priority?: number
    clear_fields?: Array<'origin' | 'app_token' | 'private_origin' | 'pinned_addresses'>
  }
  telegram?: {
    enabled?: boolean
    bot_token?: string
    chat_id?: string
    api_origin?: string
    private_origin?: string
    pinned_addresses?: string[]
    clear_fields?: Array<'bot_token' | 'chat_id' | 'api_origin' | 'private_origin' | 'pinned_addresses'>
  }
  nas?: {
    enabled?: boolean
    endpoint?: string
    signing_secret?: string
    private_origin?: string
    pinned_addresses?: string[]
    clear_fields?: Array<'endpoint' | 'signing_secret' | 'private_origin' | 'pinned_addresses'>
  }
}

export interface AuthStatus {
  enabled: boolean
  configured: boolean
  secret_configured: boolean
  username: string
  authenticated: boolean
  security: {
    safe: boolean
    startup_allowed: boolean
    override_active: boolean
    findings: Array<{
      code: string
      severity: 'critical' | 'warning'
      message: string
      remediation: string
    }>
  }
}

export type SearchSort = 'relevance' | 'release_date_desc' | 'release_date_asc' | 'code_asc' | 'code_desc'
export type SearchMatch = 'auto' | 'exact' | 'fuzzy'
export type SearchKind = 'keyword' | 'code' | 'actor' | 'tag' | 'series' | 'maker' | 'publisher' | 'director'
export type MagnetHint = 'available' | 'unavailable' | 'unknown'

export interface RelatedRef {
  kind: SearchKind
  label: string
  url: string | null
}

export interface WorkRating {
  value: number | null
  votes: number | null
  text: string | null
}

export interface WorkSourceDetails {
  duration_minutes: number | null
  duration_text: string | null
  rating: WorkRating | null
  makers: RelatedRef[]
  publishers: RelatedRef[]
  series: RelatedRef[]
  directors: RelatedRef[]
  actors: RelatedRef[]
  tags: RelatedRef[]
}

export type WorkImageKind = 'cover' | 'backdrop' | 'sample'

export interface WorkImage {
  kind: WorkImageKind
  url: string
  thumbnail_url: string | null
  width: number | null
  height: number | null
}

export interface WorkCover extends WorkImage {
  source_id: string
  kind: 'cover'
}

export interface FieldSourceOrigin {
  source_id: string
  provider?: string
  url: string
  upstream_source?: string
  upstream_url?: string
}

export interface FieldSource extends FieldSourceOrigin {
  contributors?: FieldSourceOrigin[]
}

export interface WorkSource {
  source_id: string
  title: string
  detail_url: string | null
  raw_code: string | null
  release_date: string | null
  images: WorkImage[]
  details?: WorkSourceDetails
  magnet_hint?: MagnetHint
  parse_status: string
  error: string | null
  details_error?: string | null
  image_error?: string | null
  magnet_error?: string | null
  field_sources?: Record<string, FieldSource>
  detail_provider?: string | null
  detail_identity_verified?: boolean
}

export interface MagnetSourceRef {
  source_id: string
  uri: string
  display_name: string | null
  reported_size_text: string | null
  reported_size_bytes: number | null
  badges: string[]
  trackers: string[]
  reported_seeders?: number | null
  reported_leechers?: number | null
  reported_at?: string | null
}

export interface WorkMagnet {
  info_hash: string
  display_name: string | null
  size_bytes: number | null
  size_is_exact?: boolean
  source_refs: MagnetSourceRef[]
}

export interface WorkResult {
  work_id: string
  canonical_code: string | null
  code: string | null
  title: string
  release_date: string | null
  release_date_conflict: boolean
  actors: string[]
  tags: string[]
  magnet_hint?: MagnetHint
  cover: WorkCover | null
  sources: WorkSource[]
  magnets: WorkMagnet[]
}

export interface SearchBasePayload {
  request_id: string
  query: string
  page?: number
  limit?: number
  result_limit?: number
  pages_scanned?: number
  pages_total?: number | null
  found_count?: number
  can_continue?: boolean
  continuation_token?: string | null
  continuation_mode?: 'retry' | 'extend' | null
  results: WorkResult[]
  errors: Record<string, string>
  skipped?: Record<string, string>
}

export type SearchStreamEvent =
  | {
      event: 'source'
      payload: SearchBasePayload & {
        source_id: string
        completed_sources: number
        total_sources: number
      }
    }
  | {
      event: 'delta'
      payload: {
        request_id: string
        source_id: string
        upstream_page: number
        delta: WorkResult[]
        pages_scanned: number
        pages_total: number | null
        found_count: number
        result_limit: number
      }
    }
  | { event: 'base'; payload: SearchBasePayload }
  | {
      event: 'result'
      payload: {
        request_id: string
        work_id: string
        done: number
        total: number
        result: WorkResult
      }
    }
  | {
      event: 'done' | 'cancelled'
      payload: {
        request_id: string
        done: number
        total: number
        errors?: Record<string, string>
        skipped?: Record<string, string>
        pages_scanned?: number
        pages_total?: number | null
        found_count?: number
        result_limit?: number
        can_continue?: boolean
        continuation_token?: string | null
        continuation_mode?: 'retry' | 'extend' | null
      }
    }
  | { event: 'error'; payload: { request_id?: string; error: string; code?: string } }

export interface SearchRequest {
  requestId: string
  query: string
  sources: string[]
  resultLimit: number
  fetchMagnets: boolean
  filters: Record<string, string>
  sort: SearchSort
  match: SearchMatch
  searchKind?: SearchKind
  semanticRefs?: Record<string, string>
  continuationToken?: string
}

export type ResourceSearchStatus =
  | 'queued'
  | 'running'
  | 'limit_reached'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type ResourceSearchVariant = WebDownloadVariant

export interface ResourceSearchItem {
  item_id: string
  code: string
  title: string | null
  available_variants: ResourceSearchVariant[]
  source_ids: string[]
}

export interface ResourceSearchSource {
  source_id: string
  status: ResourceSearchStatus
  item_count: number
  error_code: string | null
  retryable: boolean
}

export interface ResourceSearchPagination {
  limit: number
  offset: number
  total: number
  has_more: boolean
  keyword: string | null
  variant: ResourceSearchVariant | null
}

export interface ResourceSearchProgress {
  percent: number
  items_found: number
  result_limit: number
  scanned_pages: number
  total_pages: number | null
  next_page: number | null
  pending_total: number
  pending_cursor: number
  pending_remaining: number
  determinate: boolean
}

export interface ResourceSearchSession {
  session_id: string
  source_id: string
  source_ids: string[]
  sources: ResourceSearchSource[]
  query: string
  exact_match?: boolean
  suffix_width: number | null
  start: number | null
  end: number | null
  status: ResourceSearchStatus
  revision: number
  result_limit: number
  item_count: number
  error_code: string | null
  retryable: boolean
  created_at: number
  updated_at: number
  started_at: number | null
  heartbeat_at: number | null
  finished_at: number | null
  items: ResourceSearchItem[]
  pagination: ResourceSearchPagination
  progress: ResourceSearchProgress
  can_continue: boolean
  can_retry: boolean
  can_cancel: boolean
  can_remove: boolean
}

export interface ResourceSearchPayload {
  ok: boolean
  search: ResourceSearchSession
}

export interface ResourceSearchCreateRequest {
  source_id: string
  query: string
  result_limit: number
  exact_match?: boolean
  start?: string
  end?: string
  suffix_width?: number
}

export interface ResourceSearchListParams {
  sessionId: string
  limit: number
  offset: number
  keyword?: string
  variant?: ResourceSearchVariant | ''
}

export interface ResourceSearchActionRequest {
  session_id: string
  expected_revision: number
  action: 'continue' | 'retry' | 'cancel' | 'remove'
  result_limit?: number
}

export interface ResourceSearchRemovalPayload {
  ok: boolean
  session_id: string
  removed: true
}

export interface ResourceSearchDownloadsRequest {
  session_id: string
  expected_revision: number
  item_ids: string[]
  max_height: number
  existing_policy: WebDownloadExistingPolicy
  variant_priority: WebDownloadVariant[]
  default_quality_strategy: WebDownloadBatchQualityStrategy
  default_height?: number
  rule_id?: string
  rule_revision?: number
  idempotency_key: string
}

export interface DownloadRequest {
  magnet: string
  name: string
  category: string
  save_path: string
  tags: string
  auto_organize: boolean
  replacement_id?: string
  idempotency_key?: string
  result: WorkResult
  magnet_info: {
    uri: string
    info_hash: string
    display_name: string | null
    trackers: string[]
    exact_length: number | null
    params: Record<string, string[]>
    source_id: string
  }
}

export interface DownloadResult {
  ok: boolean
  info_hash: string
  display_name: string
  category: string
  save_path: string
  tags: string
  metadata_warning?: string
  replacement_warning?: string
  replacement?: DownloadReplacementOutcome
  organize?: {
    id: string
    name: string
    priority: number
    actions: OrganizerRule['actions']
  }
}

export type DownloadReplacementSourceKind = 'qb' | 'web_job' | 'web_intent'
export type DownloadRecoveryMode = 'smart_magnet' | 'web' | 'manual_magnet'

export interface DownloadReselection {
  source_kind: DownloadReplacementSourceKind
  source_id: string
  code: string
  recovery?: DownloadReplacement
}

export type DownloadReplacementStatus =
  | 'open'
  | 'submitting'
  | 'replacement_created'
  | 'completed'
  | 'cleanup_failed'
  | 'discarding'
  | 'discarded'
  | 'expired'

export interface DownloadReplacement {
  replacement_id: string
  idempotency_key: string
  source_kind: DownloadReplacementSourceKind
  source_id: string
  code: string
  status: DownloadReplacementStatus
  created_at: number
  updated_at: number
  expires_at: number
  target_kind: 'qb' | 'web_job' | null
  target_id: string | null
  cleanup_error: string | null
  smart_selection_id: string | null
  smart_selection_outcome: 'running' | 'selected' | 'not_found' | 'inconclusive' | 'cancelled' | 'failed' | null
  smart_selection_cleanup_status: 'pending' | 'complete' | 'not_required' | 'incomplete' | 'unknown' | null
  smart_selection_finished_at: number | null
  disposition: 'archive' | 'delete' | null
  recovery_mode: 'idle' | DownloadRecoveryMode
  discovery_status: 'idle' | 'queued' | 'running' | 'available' | 'not_found' | 'inconclusive'
  magnet_status: 'pending' | 'available' | 'not_found' | 'unavailable'
  magnet_count: number
  magnets?: WorkMagnet[]
  web_status: 'pending' | 'available' | 'not_found' | 'unavailable'
  web_provider_ids: Array<'missav' | 'jable' | 'supjav' | string>
  web_variant: WebDownloadVariant | null
  magnet_error_code: string | null
  web_error_code: string | null
  discovery_started_at: number | null
  discovery_finished_at: number | null
}

export interface DownloadReplacementPayload {
  ok: boolean
  replacement: DownloadReplacement
}

export interface FailedDownloadArchiveItem {
  code: string
  archived_at: number
}

export interface FailedDownloadArchivePayload {
  ok: boolean
  items: FailedDownloadArchiveItem[]
  count: number
  offset: number
  limit: number
  has_more: boolean
}

export interface FailedDownloadDispositionPreviewPayload {
  ok: boolean
  snapshot_token: string
  count: number
  source_counts: {
    bt: number
    web: number
  }
  expires_in_seconds: number
}

export type FailedDownloadCleanupErrorCode =
  | 'source_changed'
  | 'qb_cleanup_failed'
  | 'web_cleanup_failed'
  | 'cleanup_storage_unavailable'
  | 'cleanup_failed'

export interface FailedDownloadDispositionPayload {
  ok: boolean
  archived: number
  deleted: number
  removed: number
  failed: number
  failures: Array<{
    replacement_id: string
    code: string
    error: FailedDownloadCleanupErrorCode
  }>
  truncated: boolean
}

export interface DownloadReplacementOutcome {
  replacement_id: string
  status: 'completed' | 'cleanup_failed'
  old_failure_removed: boolean
  replayed: boolean
  cleanup_error: string | null
}

export type DownloadImportSourceType = 'magnet' | 'thunder' | 'btih'

export type DownloadImportMetadataStatus =
  | 'not_requested'
  | 'pending'
  | 'ready'
  | 'unavailable'
  | 'restricted'

export interface DownloadImportFile {
  index: number
  name: string
  size: number
}

export interface DownloadImportContent {
  metadata_status?: DownloadImportMetadataStatus
  torrent_name?: string | null
  total_size?: number | null
  file_count?: number | null
  files?: DownloadImportFile[]
  files_truncated?: boolean
  content_error?: string | null
}

export interface DownloadImportPreviewItem extends DownloadImportContent {
  source_type: DownloadImportSourceType
  info_hash: string
  display_name: string | null
  catalog_code: string | null
  requires_confirmation: boolean
}

export interface DownloadImportError {
  input: string
  error: string
}

export interface DownloadImportPreviewPayload {
  ok: boolean
  count: number
  duplicate_count: number
  requires_confirmation: boolean
  items: DownloadImportPreviewItem[]
  errors: DownloadImportError[]
}

export interface DownloadImportInspectPayload extends DownloadImportPreviewPayload {
  probe: MagnetProbePayload
}

export interface DownloadImportResultItem extends DownloadImportPreviewItem {
  status: 'added' | 'failed'
  error?: string
  metadata_warning?: string
}

export interface DownloadImportPayload {
  ok: boolean
  added_count: number
  failed_count: number
  invalid_count: number
  items: DownloadImportResultItem[]
}

export type MagnetProbeStatus = 'queued' | 'running' | 'cleaning' | 'cancelling' | 'complete' | 'failed' | 'cancelled'

export interface MagnetProbeItem extends DownloadImportContent {
  info_hash: string
  catalog_code?: string | null
  requires_confirmation?: boolean
  origin: 'preexisting' | 'temporary' | 'external'
  state: string
  seed_status: 'available' | 'none_observed' | 'unknown'
  seeders: number | null
  connected_seeders: number | null
  leechers: number | null
  availability: number | null
  availability_status: 'unknown' | 'none' | 'partial' | 'complete_copy'
  metadata_received: boolean
}

export interface MagnetProbePayload {
  ok: boolean
  probe_id: string
  purpose?: 'seed' | 'metadata'
  status: MagnetProbeStatus
  total: number
  progress: {
    resolved: number
    total: number
    elapsed_ms: number
    timeout_ms: number
  }
  items: MagnetProbeItem[]
  cleanup: null | {
    status: 'not_required' | 'complete' | 'incomplete' | 'unknown'
    deleted?: number
    skipped?: number
    remaining?: string[]
    error?: string
  }
  error?: string
}

export type MagnetSelectionStatus = 'queued' | 'running' | 'cleaning' | 'cancelling' | 'complete' | 'failed' | 'cancelled'

export interface MagnetSelectionItem extends MagnetProbeItem {
  name?: string | null
  download_speed?: number | null
  peak_download_speed?: number | null
  progress?: number | null
  quality_rank?: number | null
  quality_label?: string | null
  selected?: boolean
  ledger_state?: 'pending' | 'submitted' | 'observing' | 'observed' | 'unavailable' | 'deferred' | 'selected' | 'discarded' | null
}

export interface MagnetSelectionPayload {
  ok: boolean
  selection_id: string
  purpose?: 'smart'
  status: MagnetSelectionStatus
  total: number
  progress: {
    resolved: number
    total: number
    elapsed_ms: number
    timeout_ms: number
  }
  items: MagnetSelectionItem[]
  selection: {
    status: 'pending' | 'selected' | 'not_found' | 'inconclusive'
    selected_info_hash: string | null
    selected_name: string | null
    selected_quality: string | null
    timed_out?: boolean
  }
  cleanup: null | {
    status: 'not_required' | 'complete' | 'incomplete' | 'unknown'
    deleted?: number
    skipped?: number
    remaining?: string[]
    error?: string
  }
  replacement?: DownloadReplacementOutcome
  replacement_error?: string
  error?: string
}

export type TorrentStage = 'queued' | 'downloading' | 'checking' | 'paused' | 'completed' | 'error'

export interface TorrentTask {
  hash: string
  name: string
  state: string
  stage: TorrentStage
  progress: number
  size: number
  downloaded: number
  amount_left: number
  dlspeed: number
  upspeed: number
  eta: number
  ratio: number
  category: string
  tags: string
  save_path: string
  added_on: number
  completion_on: number
  issue: string
  can_pause: boolean
  can_resume: boolean
  complete: boolean
  reselection?: DownloadReselection
}

export interface TorrentSummary {
  scope: 'category'
  category: string
  total: number
  downloading: number
  completed: number
  errors: number
  speed: number
}

export interface TorrentListPayload {
  configured: boolean
  ok: boolean
  tasks: TorrentTask[]
  count?: number
  offset?: number
  limit?: number
  has_more?: boolean
  category?: string
  query?: string
  summary?: TorrentSummary
  error_code?: string
  snapshot_limit?: number
  error?: string
  no_source_failure_count?: number
}

export type WebDownloadVariant = 'original' | 'chinese_subtitle' | 'uncensored_leak'

export type WebDownloadVariantStatus = 'available' | 'not_found' | 'failed'

export interface WebDownloadVariantOption {
  variant: WebDownloadVariant
  status: WebDownloadVariantStatus
  heights: number[]
}

export interface WebDownloadJob {
  job_id: string
  provider: 'auto' | 'missav' | 'jable' | 'supjav'
  resolved_provider?: 'missav' | 'jable' | 'supjav' | null
  code: string
  variant: WebDownloadVariant
  requested_height: number | null
  selected_height: number | null
  verified_height?: number | null
  quality_strategy: 'legacy' | 'selected' | 'highest'
  existing_policy?: 'keep_both' | WebDownloadExistingPolicy
  publication_outcome?: 'published' | 'replaced' | 'kept_existing' | null
  superseded_by_job_id?: string | null
  status: string
  progress: number
  downloaded_bytes: number
  total_bytes: number | null
  speed: number
  eta: number | null
  created_at: number | string
  updated_at: number | string
  error: string | null
  failure_stage?: string | null
  failure_code?: string | null
  output_path: string | null
  archive_status: 'available' | 'missing' | 'unknown' | 'replaced'
  priority?: number
  queue_position?: number
  retry_count?: number
  next_retry_at?: number | null
  can_pause?: boolean
  can_resume?: boolean
  can_cancel: boolean
  can_retry: boolean
  can_remove: boolean
  reselection?: DownloadReselection
  replacement?: DownloadReplacementOutcome
  replacement_warning?: string
}

export interface WebDownloadRemoval {
  job_id: string
  removed: true
}

export type WebDownloadActionResult = WebDownloadJob | WebDownloadRemoval

export interface WebDownloadScheduleWindow {
  days: number[]
  start: string
  end: string
}

export interface WebDownloadControl {
  queue_revision: number
  global_paused: boolean
  target_concurrency: number
  bandwidth_limit: number
  timezone: string
  schedule: WebDownloadScheduleWindow[]
  updated_at: number
}

export interface WebDownloadListPayload {
  ok: boolean
  configured: boolean
  enabled?: boolean
  available?: boolean
  reason?: string | null
  providers?: Array<{ id: 'jable' | 'supjav' | 'missav'; name: string; available: boolean }>
  tasks: WebDownloadJob[]
  intent?: WebDownloadBatch | null
  intents?: WebDownloadBatch[]
  failed_intent_count?: number
  queued_intent_count?: number
  max_concurrency?: number
  control?: WebDownloadControl
  count?: number
  offset?: number
  limit?: number
  has_more?: boolean
  summary?: {
    total: number
    running: number
    queued: number
    retrying?: number
    completed: number
    missing: number
    failed: number
    speed: number
  }
  error?: string
  no_source_failure_count?: number
}

export interface WebDownloadRetryFailedPayload {
  ok: boolean
  summary: {
    job_retried: number
    intent_retried: number
    job_failed: number
    intent_failed: number
    total_candidates: number
    limit: number
    truncated: boolean
  }
  failures: Array<{
    source_kind: 'web_job' | 'web_intent'
    source_id: string
    code: string
    error: string
  }>
}

export type WebDownloadBatchStatus =
  | 'queued'
  | 'discovering'
  | 'ready'
  | 'too_many'
  | 'incomplete'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | 'committed'

export type WebDownloadExistingPolicy = 'higher_quality' | 'overwrite' | 'skip'

export type WebDownloadBatchQualityStatus = 'pending' | 'ready' | 'failed' | 'legacy'
export type WebDownloadBatchQualityStrategy = 'highest' | 'selected'

export interface WebDownloadBatchItem {
  code: string
  variant: WebDownloadVariant
  status: 'discovered' | 'created' | 'reused' | 'skipped_completed'
  job_id: string | null
  selected: boolean
  quality_status: WebDownloadBatchQualityStatus
  available_heights: number[]
  default_height: number | null
  quality_strategy: WebDownloadBatchQualityStrategy
  requested_height: number | null
  quality_error_code: 'quality_unavailable' | 'no_eligible_quality' | null
}

export interface WebDownloadBatchItemIntent {
  code: string
  variant: WebDownloadVariant
  quality_strategy: WebDownloadBatchQualityStrategy
  requested_height: number
}

export interface WebDownloadBatch {
  batch_id: string
  root_chain_id: string
  provenance_type?: 'series_discovery' | 'resource_search_selection'
  status: WebDownloadBatchStatus
  mode: string
  code_or_prefix: string
  prefix: string
  suffix_width: number | null
  max_height: number
  variant_priority: WebDownloadVariant[]
  existing_policy: WebDownloadExistingPolicy
  page: number
  page_budget: number
  limit_reached: boolean
  resume_start: string | null
  quality_complete: boolean
  rule_id: string | null
  rule_revision: number | null
  start: string | number | null
  end: string | number | null
  count: number
  created_count: number
  reused_count: number
  skipped_count: number
  selected_count: number
  excluded_count: number
  created_at: number | string
  updated_at: number | string
  expires_at: number | string | null
  error: string | null
  can_cancel: boolean
  can_retry?: boolean
  can_commit: boolean
  has_more?: boolean
  continuation_batch_id?: string | null
  can_continue?: boolean
  can_remove: boolean
  reselection?: DownloadReselection
  items: WebDownloadBatchItem[]
}

export interface WebDownloadBatchPayload {
  ok: boolean
  batch: WebDownloadBatch
}

export interface WebDownloadBatchRuleCriteria {
  code_or_prefix: string
  max_height: number
  variant_priority: WebDownloadVariant[]
  existing_policy: WebDownloadExistingPolicy
  start?: string
  end?: string
}

export interface WebDownloadBatchFailureCursor {
  batch_id: string
  page: number
  start: string | null
  status: Extract<WebDownloadBatchStatus, 'failed' | 'incomplete' | 'too_many'>
}

export interface WebDownloadBatchChainSummary {
  root_chain_id: string
  status: WebDownloadBatchStatus
  code_or_prefix: string
  prefix: string
  start: string | null
  end: string | null
  max_height: number
  existing_policy: WebDownloadExistingPolicy
  page_budget: number
  pages_scanned: number
  last_page: number
  discovered_count: number
  selected_count: number
  created_count: number
  reused_count: number
  skipped_count: number
  failed_count: number
  failed_pages: number[]
  failed_cursors: WebDownloadBatchFailureCursor[]
  limit_reached: boolean
  resume_start: string | null
  can_continue: boolean
  created_at: number
  updated_at: number
  rule_id: string | null
}

export interface WebDownloadBatchChainsPayload {
  ok: boolean
  chains: WebDownloadBatchChainSummary[]
  count: number
  limit: number
  offset: number
  has_more: boolean
}

export interface WebDownloadBatchChainPayload extends WebDownloadBatchChainSummary {
  ok: boolean
  pages: WebDownloadBatch[]
  page_count: number
  page_limit: number
  page_offset: number
  has_more_pages: boolean
}

export interface WebDownloadBatchChainActionPayload extends WebDownloadBatchChainSummary {
  ok: boolean
}

export interface WebDownloadBatchChainExportPage {
  batch_id: string
  page: number
  status: WebDownloadBatchStatus
  start: string | null
  next_start: string | null
  items: Array<{
    code: string
    selected: boolean
    quality_strategy: WebDownloadBatchQualityStrategy
    requested_height: number | null
  }>
}

export interface WebDownloadBatchChainExport {
  schema: 'jav-pilot-web-batch-chain/v1'
  root_chain_id: string
  prefix: string
  suffix_width: number | null
  start: string | null
  end: string | null
  page_budget: number
  resume_start: string | null
  pages: WebDownloadBatchChainExportPage[]
}

export type WebDownloadBatchRuleSelection = 'all' | 'missing' | 'upgrades'

export interface WebDownloadBatchRule {
  rule_id: string
  name: string
  mode: 'exact' | 'all' | 'range'
  code_or_prefix: string
  prefix: string
  suffix_width: number | null
  start: string | null
  end: string | null
  max_height: number
  variant_priority: WebDownloadVariant[]
  existing_policy: WebDownloadExistingPolicy
  default_quality_strategy: WebDownloadBatchQualityStrategy
  default_height: number | null
  selection_mode: WebDownloadBatchRuleSelection
  revision: number
  created_at: number
  updated_at: number
}

export interface WebDownloadBatchRulesPayload {
  ok: boolean
  rules: WebDownloadBatchRule[]
}

export interface WebDownloadBatchRulePayload {
  ok: boolean
  rule: WebDownloadBatchRule
}

export interface WebDownloadBatchRuleSaveRequest extends WebDownloadBatchRuleCriteria {
  rule_id?: string
  name: string
  default_quality_strategy: WebDownloadBatchQualityStrategy
  default_height?: number
  selection_mode: WebDownloadBatchRuleSelection
  expected_revision?: number
}

export type MediaMetadataKind = 'qb' | 'web' | 'manual'

export type MediaMetadataStatus =
  | 'waiting_media'
  | 'queued'
  | 'running'
  | 'retry'
  | 'completed'
  | 'failed'

export interface MediaMetadataJob {
  job_id: string
  kind: MediaMetadataKind
  code: string
  status: MediaMetadataStatus
  relative_media_path: string | null
  attempts: number
  max_attempts?: number
  next_attempt_at?: number | null
  error: string | null
  assets: Record<string, {
    status: 'generated' | 'existing' | 'missing' | 'conflict'
    source_id?: string
    width?: number
    height?: number
  }>
  created_at: number
  updated_at: number
  can_retry: boolean
}

export interface MediaMetadataCompletePayload {
  ok: boolean
  queued: number
  retried: number
  skipped: number
  unidentified: number
  unidentified_examples: string[]
}

export interface MediaMetadataListPayload {
  ok: boolean
  jobs: MediaMetadataJob[]
  count: number
  offset?: number
  limit?: number
  has_more?: boolean
  summary?: {
    total: number
    waiting: number
    running: number
    completed: number
    failed: number
  }
  library_path: string
  error?: string
}

export interface MediaMetadataScanPayload {
  ok: boolean
  queued: number
  jobs: MediaMetadataJob[]
  error?: string
}

export type MediaMetadataMigrationStatus =
  | 'ready'
  | 'migrated'
  | 'current'
  | 'missing'
  | 'invalid'
  | 'unverified'
  | 'failed'

export interface MediaMetadataMigrationResult {
  code: string
  relative_media_path: string
  nfo_path: string
  provenance: 'generated' | 'tracked_existing' | null
  status: MediaMetadataMigrationStatus
  backup_path?: string
  error?: string
}

export interface MediaMetadataMigrationPayload {
  ok: boolean
  scanned: number
  migrated: number
  current: number
  skipped: number
  failed: number
  backup_path: string | null
  results: MediaMetadataMigrationResult[]
  error?: string
}

export interface MediaMetadataMigrationPreviewPayload {
  ok: boolean
  preview_id: string | null
  scanned: number
  ready: number
  current: number
  skipped: number
  failed: number
  tracked_existing: number
  results: MediaMetadataMigrationResult[]
  error?: string
}

export type MediaMetadataReviewFieldName =
  | 'title'
  | 'original_title'
  | 'release_date'
  | 'duration_minutes'
  | 'rating'
  | 'makers'
  | 'publishers'
  | 'series'
  | 'directors'
  | 'actors'
  | 'tags'
  | 'description'

export type MetadataSourceId = 'javbus' | 'javdb' | 'fc2' | 'fanza' | 'mgs' | 'avbase' | 'fc2db' | 'javten'
export type MediaMetadataReviewSource = 'nfo' | MetadataSourceId | 'missav'
export type MediaMetadataReviewImageKind = 'portrait' | 'landscape'
export type MediaMetadataReviewValue = string | number | string[] | null

export interface MediaMetadataReviewSourceValue {
  value: MediaMetadataReviewValue
  fetched_at: number
  snapshot_id: string
}

export interface MediaMetadataReviewField {
  sources: Partial<Record<MediaMetadataReviewSource, MediaMetadataReviewSourceValue>>
  manual_set: boolean
  manual_value: MediaMetadataReviewValue
  selected_source: MediaMetadataReviewSource | null
  locked: boolean
  final_value: MediaMetadataReviewValue
  final_source: MediaMetadataReviewSource | 'manual' | 'default' | 'locked'
  differs: boolean
}

export interface MediaMetadataReviewImage {
  image_id: string
  sha256: string
  width: number
  height: number
  fetched_at: number
}

export interface MediaMetadataReview {
  review_id: string
  code: string
  relative_media_path: string
  revision: number
  abandon_generation: number
  created_at: number
  updated_at: number
  abandoned: boolean
  abandoned_at: number | null
  fields: Record<MediaMetadataReviewFieldName, MediaMetadataReviewField>
  images: Partial<Record<
    MediaMetadataReviewImageKind,
    Partial<Record<'nfo' | MetadataSourceId | 'manual', MediaMetadataReviewImage>>
  >>
  local_assets?: Record<string, {
    status: 'generated' | 'existing' | 'missing' | 'conflict'
    source_id?: string
    width?: number
    height?: number
  }>
  nfo_snapshot_status?: 'captured' | 'missing' | 'unsafe' | 'unavailable'
}

export interface MediaMetadataReviewPayload {
  ok: boolean
  review: MediaMetadataReview
  publications?: MediaMetadataReviewPublication[]
}

export interface MediaMetadataReviewDraftRequest {
  review_id: string
  manual_values?: Partial<Record<MediaMetadataReviewFieldName, MediaMetadataReviewValue>>
  source_choices?: Partial<Record<MediaMetadataReviewFieldName, MediaMetadataReviewSource | 'auto'>>
  locks?: Partial<Record<MediaMetadataReviewFieldName, boolean>>
  clear_manual?: MediaMetadataReviewFieldName[]
  expected_revision: number
}

export interface MediaMetadataReviewRefetchRequest {
  review_id: string
  expected_revision: number
  sources: Array<MetadataSourceId | 'missav'>
  fields: MediaMetadataReviewFieldName[]
  images: MediaMetadataReviewImageKind[]
}

export interface MediaMetadataReviewRefetchPayload extends MediaMetadataReviewPayload {
  intent: {
    intent_id: string
    review_id: string
    base_revision: number
    base_abandon_generation: number
    sources: Array<MetadataSourceId | 'missav'>
    fields: MediaMetadataReviewFieldName[]
    images: MediaMetadataReviewImageKind[]
    status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
    error_code: string | null
    created_at: number
    updated_at: number
  }
  sources: Array<Record<string, unknown>>
  image_refs: Partial<Record<MediaMetadataReviewImageKind, string>>
  images: Partial<Record<MediaMetadataReviewImageKind, {
    kind: MediaMetadataReviewImageKind
    source_id: MetadataSourceId
    sha256: string
    width: number
    height: number
  }>>
}

export interface MediaMetadataReviewImagePayload extends MediaMetadataReviewPayload {
  image_ref: string
  image: {
    kind: MediaMetadataReviewImageKind
    source_id: 'manual'
    sha256: string
    width: number
    height: number
  }
}

export interface MediaMetadataReviewPreviewArtifact {
  relative_path: string
  kind: 'nfo' | MediaMetadataReviewImageKind
  source_id: 'draft' | MetadataSourceId | 'manual'
  action: 'create' | 'replace' | 'unchanged'
  current_sha256: string | null
  proposed_sha256: string
  proposed_bytes: number
}

export interface MediaMetadataReviewPreviewPayload {
  ok: boolean
  preview_token: string
  review_id: string
  revision: number
  expires_at: number
  artifacts: MediaMetadataReviewPreviewArtifact[]
}

export interface MediaMetadataReviewPublication {
  publication_id: string
  review_id: string
  review_revision: number
  draft: Record<string, unknown>
  artifacts: Array<{
    relative_path: string
    kind: 'nfo' | MediaMetadataReviewImageKind
    source_id: string
    action: 'create' | 'replace' | 'unchanged'
    sha256: string
    backup_path: string | null
  }>
  created_at: number
}

export interface MediaMetadataReviewPublishPayload extends MediaMetadataReviewPublication {
  ok: boolean
}

export type MediaLibraryPresence = 'present' | 'missing' | 'unknown'
export type MediaLibraryAssetStatus = 'present' | 'missing' | 'invalid' | 'unknown'

export interface MediaLibraryEntry {
  entry_id: string
  revision: number
  scope_path: string
  code: string | null
  code_key: string | null
  variant: WebDownloadVariant | null
  title: string | null
  release_date: string | null
  source: string
  presence: MediaLibraryPresence
  primary_media_path: string
  media_paths: string[]
  media_files: Array<{
    relative_path: string
    variant: WebDownloadVariant | null
  }>
  nfo_status: MediaLibraryAssetStatus
  nfo_path: string | null
  portrait_status: MediaLibraryAssetStatus
  landscape_status: MediaLibraryAssetStatus
  quality_height: number | null
  duplicate_count: number
  actors: string[]
  makers: string[]
  publishers: string[]
  tags: string[]
  series: string[]
  directors: string[]
}

export interface MediaLibraryListParams {
  query?: string
  actor?: string
  maker?: string
  tag?: string
  series?: string
  source?: string
  presence?: MediaLibraryPresence
  completeness?: 'complete' | 'incomplete'
  anomaly?: 'duplicate' | 'unidentified' | 'missing' | 'nfo' | 'portrait' | 'landscape'
  min_height?: number
  max_height?: number
  limit?: number
  offset?: number
}

export interface MediaLibraryListPayload {
  ok: boolean
  items: MediaLibraryEntry[]
  count: number
  limit: number
  offset: number
  has_more: boolean
  index_state?: 'available' | 'ready' | 'indexing' | 'initializing' | 'unknown'
  revision?: number
  last_error_code?: string | null
  last_completed_at?: number | null
  error?: string
}

export interface MediaLibraryActionPayload {
  ok: boolean
  scan_kind: 'full' | 'incremental'
  changed: boolean
  published: boolean
  present: number
  missing: number
  index_state: 'ready' | 'unknown'
  revision: number
}

export type HistoryTaskType = 'web' | 'batch' | 'metadata'
export type HistoryExportFormat = 'json' | 'csv'
export type HistoryVacuumTarget = 'web' | 'metadata' | 'library'

export interface HistoryFilters {
  task_types: HistoryTaskType[]
  statuses?: Partial<Record<HistoryTaskType, string[]>>
  created_after?: number
  created_before?: number
  updated_after?: number
  updated_before?: number
  code?: string
  limit: number
}

export interface HistorySkippedItem {
  task_type: HistoryTaskType
  id: string
  reason: string
}

export interface HistorySkippedReport {
  counts: Record<string, number>
  items: HistorySkippedItem[]
  details_truncated: boolean
}

export interface HistoryPreviewItem {
  task_type: HistoryTaskType
  id: string
  status: string
  code: string | null
  variant: WebDownloadVariant | null
  record_count: number
  estimated_bytes: number
}

export interface HistoryPreviewPayload {
  ok?: boolean
  preview_token: string
  created_at: number
  expires_at: number
  filters: HistoryFilters
  selected: {
    records: number
    groups: number
    estimated_bytes: number
    by_type: Partial<Record<HistoryTaskType, number>>
  }
  items: HistoryPreviewItem[]
  skipped: HistorySkippedReport
}

export interface HistoryCleanupPayload {
  ok?: boolean
  removed: {
    records: number
    groups: number
    by_type: Partial<Record<HistoryTaskType, number>>
    ids: string[]
    ids_truncated: boolean
  }
  skipped: HistorySkippedReport
  vacuum_required: boolean
}

export interface HistoryRetentionPolicy {
  web: number | null
  batch: number | null
  metadata: number | null
}

export interface HistoryRetentionScheduleConfig {
  auto_enabled: boolean
  timezone: string
  hour: number
}

export interface HistoryRetentionScheduleStatus extends HistoryRetentionScheduleConfig {
  active: boolean
  worker_alive: boolean
  ready: boolean
  last_started_at: number | null
  last_succeeded_at: number | null
  last_error_code: string | null
  outcome: 'never' | 'running' | 'succeeded' | 'failed' | 'interrupted' | 'unavailable'
  next_retry_at: number | null
  next_run_at: number | null
  last_removed_records: number
  last_batches: number
  batch_size: number
  max_batches_per_run: number
  max_records_per_run: number
  state_recovery_required: boolean
}

export interface HistoryStatusPayload {
  ok: boolean
  retention: HistoryRetentionPolicy
  retention_schedule: HistoryRetentionScheduleStatus
  maintenance_mode: boolean
  backup_verified: boolean
  backup_created_at?: number | null
  preview_ttl_seconds: number
  limits: {
    cleanup_records: number
    export_records: number
  }
}

export interface HistoryRetentionRecoveryPayload extends HistoryStatusPayload {
  state_recovery: {
    recovered: boolean
    backup_name: string | null
  }
}

export interface HistoryVacuumPayload {
  ok?: boolean
  target: HistoryVacuumTarget
  before_bytes: number
  after_bytes: number
  reclaimed_bytes: number
  before_integrity: 'ok'
  after_integrity: 'ok'
  backup_verified: true
}

export interface HistoryExportDownload {
  blob: Blob
  checksum: string
  filename: string
  format: HistoryExportFormat
}

export interface DownloadHistoryItem {
  code: string
  torrent: 'completed' | 'downloading' | 'paused' | 'error' | null
  web: 'completed' | 'active' | null
  library: boolean
  library_path: string | null
  state: 'downloaded' | 'active' | 'none'
}

export interface DownloadHistoryLookupPayload {
  ok: boolean
  items: DownloadHistoryItem[]
  unavailable: Array<'torrent' | 'web' | 'library'>
}

export interface SearchHistoryItem {
  id: string
  kind: 'metadata' | 'resource'
  query: string
  params: Record<string, unknown>
  result_count: number | null
  use_count: number
  created_at: number
  used_at: number
}

export interface SearchHistoryPayload {
  ok: boolean
  items: SearchHistoryItem[]
  total: number
  limit: number
}

export type RankingPeriod = 'daily' | 'weekly' | 'monthly'
export type RankingType = 'censored' | 'uncensored' | 'western' | 'fc2'

export interface RankingItem {
  rank: number
  code: string | null
  title: string
  release_date: string | null
  detail_url: string | null
  cover: string | null
  rating: number | null
  votes: number | null
  source_id: string
}

export interface RankingPayload {
  ok: boolean
  source_id: string
  period: RankingPeriod
  type: RankingType
  items: RankingItem[]
  fetched_at: number
}
