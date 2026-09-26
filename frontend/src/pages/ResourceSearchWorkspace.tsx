import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckSquare2,
  Download,
  Filter,
  RefreshCw,
  Save,
  ScanSearch,
  Search,
  Settings,
  Trash2,
  X,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { ResultPagination } from '../components/ResultPagination'
import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, Field, IconButton, InlineNotice, ProgressBar, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api, ApiError, retryTransientApiRequest } from '../lib/api'
import { downloadHistoryBadge, downloadHistorySummary } from '../lib/downloadHistory'
import { loadResourcePreferences, saveResourcePreferences } from '../lib/resourcePreferences'
import { diagnosticStageLabel, errorCodeLabel, serviceErrorMessage } from '../lib/presentation'
import { normalizeSearchQuery, searchTerms } from '../lib/searchTerms'
import { WEB_DOWNLOAD_VARIANTS, webDownloadVariantLabel } from '../lib/webDownloads'
import type {
  ResourceSearchDownloadsRequest,
  ResourceSearchSession,
  ResourceSearchStatus,
  ResourceSearchVariant,
  SiteDiagnosticStatus,
  WebDownloadBatch,
  WebDownloadBatchQualityStrategy,
  WebDownloadBatchRule,
  WebDownloadBatchRuleSelection,
  WebDownloadExistingPolicy,
  WebDownloadVariant,
} from '../types'

import '../styles/resourceSearch.css'

const RESOURCE_PAGE_SIZES = [10, 20, 50, 100] as const
const RESOURCE_KEYWORD_ERROR_ID = 'resource-keyword-error'
const RESOURCE_SESSION_ID_PATTERN = /^[0-9a-f]{32}$/i
const RESULT_LIMIT_ERROR = '请输入 1 到 999 之间的整数'

const RULE_ID_PATTERN = /^[0-9a-f]{32}$/i
const ACTIVE_RESOURCE_STATUSES = new Set<ResourceSearchStatus>(['queued', 'running'])
const QUALITY_LIMITS = [
  { value: 4320, label: '8K' },
  { value: 2160, label: '4K' },
  { value: 1440, label: '1440p' },
  { value: 1080, label: '1080p' },
  { value: 720, label: '720p' },
  { value: 480, label: '480p' },
] as const
const EXISTING_POLICIES: Array<{ value: WebDownloadExistingPolicy; label: string }> = [
  { value: 'higher_quality', label: '仅更高画质' },
  { value: 'overwrite', label: '始终覆盖' },
  { value: 'skip', label: '不重复下载' },
]
const VARIANT_PRIORITIES: WebDownloadVariant[][] = [
  ['original', 'chinese_subtitle', 'uncensored_leak'],
  ['original', 'uncensored_leak', 'chinese_subtitle'],
  ['chinese_subtitle', 'original', 'uncensored_leak'],
  ['chinese_subtitle', 'uncensored_leak', 'original'],
  ['uncensored_leak', 'original', 'chinese_subtitle'],
  ['uncensored_leak', 'chinese_subtitle', 'original'],
]

const RESOURCE_STATUS_LABELS: Record<ResourceSearchStatus, string> = {
  queued: '等待扫描',
  running: '正在扫描',
  limit_reached: '已达到上限',
  completed: '扫描完成',
  failed: '扫描失败',
  cancelled: '已取消',
}

type ResourceDiagnosticState = 'healthy' | 'failed' | 'unknown'

function resourceStatusTone(session: ResourceSearchSession, diagnosticState: ResourceDiagnosticState) {
  if (session.status !== 'failed' && !ACTIVE_RESOURCE_STATUSES.has(session.status) && session.sources.some((source) => source.status === 'failed')) return 'warning' as const
  if (session.status === 'completed' && session.item_count === 0) return diagnosticState === 'healthy' ? 'info' as const : 'warning' as const
  if (session.status === 'completed') return 'success' as const
  if (session.status === 'failed') return 'danger' as const
  if (session.status === 'cancelled' || session.status === 'limit_reached') return 'warning' as const
  return 'info' as const
}

function resourceStatusLabel(session: ResourceSearchSession, diagnosticState: ResourceDiagnosticState): string {
  if (session.status === 'completed' && session.sources.some((source) => source.status === 'failed')) return '部分来源失败'
  if (session.status === 'completed' && session.item_count === 0) {
    if (diagnosticState === 'failed') return '结果可能不完整'
    if (diagnosticState === 'unknown') return '结果待确认'
    return '未找到结果'
  }
  return RESOURCE_STATUS_LABELS[session.status]
}

function diagnosticFailureLabels(
  statuses: SiteDiagnosticStatus[],
  siteIds: string[],
): string[] {
  return Array.from(new Set(
    statuses
      .filter((status) => (
        siteIds.includes(status.site)
        && status.last_error_code
        && status.consecutive_failures >= 3
      ))
      .map((status) => errorCodeLabel(
        status.last_error_code,
        `${diagnosticStageLabel(status.stage)}检查失败`,
      )),
  ))
}

function resourceDiagnosticState(
  statuses: SiteDiagnosticStatus[],
  siteIds: string[],
  diagnosticsAvailable: boolean,
): ResourceDiagnosticState {
  if (!diagnosticsAvailable || !siteIds.length) return 'unknown'
  const siteStatuses = statuses.filter((status) => siteIds.includes(status.site))
  if (siteStatuses.some((status) => (
    status.last_error_code && status.consecutive_failures >= 3
  ))) return 'failed'
  return siteIds.every((siteId) => siteStatuses.some((status) => status.site === siteId)) ? 'healthy' : 'unknown'
}

function variantPriorityLabel(priority: readonly WebDownloadVariant[]): string {
  return priority.map(webDownloadVariantLabel).join(' > ')
}

function progressValue(session: ResourceSearchSession): number {
  const value = Number(session.progress.percent)
  if (!Number.isFinite(value) || value <= 0) return 0
  return Math.min(1, value / 100)
}

function resourceErrorLabel(code: string | null): string {
  const labels: Record<string, string> = {
    dependency_unavailable: '资源搜索依赖暂不可用',
    transient_browser_failure: '页面访问暂时失败，可从当前进度重试',
    discovery_unavailable: '资源页暂时无法解析',
    invalid_instruction: '搜索条件无效',
    internal_failure: '资源扫描发生内部错误',
  }
  return code ? labels[code] ?? '资源扫描未完成' : '资源扫描未完成'
}

function backgroundSubmissionMessage(batch: WebDownloadBatch): string {
  const outcomes = [`新建 ${batch.created_count} 项`]
  if (batch.reused_count) outcomes.push(`复用 ${batch.reused_count} 项`)
  if (batch.skipped_count) outcomes.push(`已有作品跳过 ${batch.skipped_count} 项`)
  if (batch.excluded_count) outcomes.push(`规则排除 ${batch.excluded_count} 项`)
  return `提交结果：${outcomes.join('，')}`
}

function routeInteger(params: URLSearchParams, key: string, fallback: number, minimum: number, maximum: number): number {
  const value = Number(params.get(key))
  return Number.isInteger(value) && value >= minimum && value <= maximum ? value : fallback
}

function parseResultLimit(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 999 ? parsed : null
}

function routeQualityLimit(params: URLSearchParams): number {
  const value = Number(params.get('max_height'))
  return QUALITY_LIMITS.some((option) => option.value === value) ? value : 2160
}

function resourceKeywordFilter(value: string): { keyword: string; error: string } {
  const keyword = normalizeSearchQuery(value)
  if (!keyword) return { keyword: '', error: '' }
  if (searchTerms(keyword).length > 16) {
    return { keyword: '', error: '筛选最多支持 16 个关键词；当前输入未应用。' }
  }
  if (Array.from(keyword).length > 80 || Array.from(keyword).some((character) => {
    const codePoint = character.codePointAt(0) ?? 0
    return codePoint < 32 || (codePoint >= 127 && codePoint <= 159)
  })) {
    return {
      keyword: '',
      error: '关键词最长 80 个字符，且不能包含控制字符；当前输入未应用。',
    }
  }
  return { keyword, error: '' }
}

function routeExistingPolicy(params: URLSearchParams): WebDownloadExistingPolicy {
  const value = params.get('existing_policy')
  return value === 'overwrite' || value === 'skip' || value === 'higher_quality' ? value : 'higher_quality'
}

function routeVariantPriority(params: URLSearchParams): WebDownloadVariant[] {
  const values = params.get('variant_priority')?.split(',') ?? []
  return values.length === WEB_DOWNLOAD_VARIANTS.length
    && new Set(values).size === WEB_DOWNLOAD_VARIANTS.length
    && values.every((value) => WEB_DOWNLOAD_VARIANTS.includes(value as WebDownloadVariant))
    ? values as WebDownloadVariant[]
    : [...WEB_DOWNLOAD_VARIANTS]
}

interface AppliedRuleBinding {
  ruleId: string
  revision: number
  name: string
}

interface QueueAttempt {
  fingerprint: string
  request: ResourceSearchDownloadsRequest
}

function createIdempotencyKey(): string {
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

export function ResourceSearchWorkspace() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const routeParams = new URLSearchParams(location.search)
  const routeSessionId = routeParams.get('resource_id')?.trim() ?? ''
  const sessionId = RESOURCE_SESSION_ID_PATTERN.test(routeSessionId) ? routeSessionId.toLowerCase() : ''
  const invalidSessionId = Boolean(routeSessionId && !sessionId)
  const routeRuleIdValue = routeParams.get('rule_id')?.trim() ?? ''
  const routeRuleRevisionValue = Number(routeParams.get('rule_revision'))
  const routeRuleId = RULE_ID_PATTERN.test(routeRuleIdValue) ? routeRuleIdValue.toLowerCase() : ''
  const routeRuleRevision = Number.isInteger(routeRuleRevisionValue) && routeRuleRevisionValue > 0
    ? routeRuleRevisionValue
    : 0
  const invalidRuleLink = Boolean(
    (routeRuleIdValue || routeParams.has('rule_revision'))
    && (!routeRuleId || !routeRuleRevision),
  )
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const diagnostics = useQuery({
    queryKey: ['site-diagnostics'],
    queryFn: () => api.siteDiagnostics(),
    enabled: settings.isSuccess,
    staleTime: 30_000,
    retry: false,
  })
  const unhealthyResourceSites = useMemo(() => new Set<string>(
    (diagnostics.data?.statuses ?? [])
      .filter((status) => status.last_error_code && status.consecutive_failures >= 3)
      .map((status) => status.site),
  ), [diagnostics.data?.statuses])
  const resourceSites = useMemo(
    () => {
      const priority = new Map<string, number>(
        (settings.data?.settings.web_resource_search_provider_priority ?? [])
          .map((siteId, index) => [siteId, index]),
      )
      return settings.data?.settings.sites
        .filter((site) => site.enabled && site.capabilities.includes('resource_search'))
        .map((site, index) => ({ site, index }))
        .sort((left, right) => (
          (priority.get(left.site.id) ?? priority.size + left.index)
          - (priority.get(right.site.id) ?? priority.size + right.index)
        ))
        .map(({ site }) => site) ?? []
    },
    [settings.data],
  )
  const [sourceId, setSourceId] = useState('all')
  const storedPreferences = useMemo(() => loadResourcePreferences(), [])
  const [exactMatch, setExactMatch] = useState(() => storedPreferences.exactMatch)
  const [historyConfirm, setHistoryConfirm] = useState<{ duplicateIds: string[]; summary: string } | null>(null)
  const [checkingHistory, setCheckingHistory] = useState(false)
  const [query, setQuery] = useState(routeParams.get('resource_query')?.trim() ?? '')
  const [resultLimit, setResultLimit] = useState(() => String(routeInteger(routeParams, 'result_limit', storedPreferences.resultLimit, 1, 999)))
  const [resultLimitError, setResultLimitError] = useState('')
  const [rangeStart, setRangeStart] = useState(routeParams.get('start')?.trim() ?? '')
  const [rangeEnd, setRangeEnd] = useState(routeParams.get('end')?.trim() ?? '')
  const [formError, setFormError] = useState('')
  const [keyword, setKeyword] = useState('')
  const [variant, setVariant] = useState<ResourceSearchVariant | ''>('')
  const [pageSize, setPageSize] = useState<(typeof RESOURCE_PAGE_SIZES)[number]>(20)
  const [page, setPage] = useState(1)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [continuationLimit, setContinuationLimit] = useState(200)
  const [continuationError, setContinuationError] = useState('')
  const [maxHeight, setMaxHeight] = useState(() => routeQualityLimit(routeParams))
  const [existingPolicy, setExistingPolicy] = useState<WebDownloadExistingPolicy>(() => routeExistingPolicy(routeParams))
  const [variantPriority, setVariantPriority] = useState<WebDownloadVariant[]>(() => routeVariantPriority(routeParams))
  const [defaultQualityStrategy, setDefaultQualityStrategy] = useState<WebDownloadBatchQualityStrategy>('highest')
  const [defaultHeight, setDefaultHeight] = useState(() => routeQualityLimit(routeParams))
  const [rulePanelOpen, setRulePanelOpen] = useState(false)
  const [ruleName, setRuleName] = useState('')
  const [ruleSelection, setRuleSelection] = useState<WebDownloadBatchRuleSelection>('all')
  const [appliedRule, setAppliedRule] = useState<AppliedRuleBinding | null>(null)
  const [editRule, setEditRule] = useState<AppliedRuleBinding | null>(null)
  const [routeRuleApplied, setRouteRuleApplied] = useState(false)
  const [removeRuleCandidateId, setRemoveRuleCandidateId] = useState('')
  const hydratedSessionRef = useRef('')
  const workflowDefaultsAppliedRef = useRef(false)
  const queueAttemptRef = useRef<QueueAttempt | null>(null)
  const queueErrorToastRef = useRef('')
  const resultStartRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (sessionId) return
    const params = new URLSearchParams(location.search)
    setSourceId('all')
    setQuery(params.get('resource_query')?.trim() ?? '')
    setResultLimit(String(routeInteger(params, 'result_limit', loadResourcePreferences().resultLimit, 1, 999)))
    setRangeStart(params.get('start')?.trim() ?? '')
    setRangeEnd(params.get('end')?.trim() ?? '')
    setMaxHeight(routeQualityLimit(params))
    setExistingPolicy(routeExistingPolicy(params))
    setVariantPriority(routeVariantPriority(params))
    setDefaultHeight(routeQualityLimit(params))
    setResultLimitError('')
    setFormError('')
  }, [location.search, sessionId])

  const resourceDefaults = settings.data?.settings.workflow_defaults?.resource_search
  useEffect(() => {
    // Configured defaults seed the batch options once, unless the link or a
    // saved rule already specifies them.
    if (!resourceDefaults || workflowDefaultsAppliedRef.current || routeRuleId) return
    workflowDefaultsAppliedRef.current = true
    const params = new URLSearchParams(location.search)
    if (!params.has('max_height')) {
      setMaxHeight(resourceDefaults.max_height)
      setDefaultHeight(resourceDefaults.default_quality === 'highest' ? resourceDefaults.max_height : resourceDefaults.default_quality)
    }
    if (!params.has('existing_policy')) setExistingPolicy(resourceDefaults.existing_policy)
    if (!params.has('variant_priority')) setVariantPriority([...resourceDefaults.variant_priority])
    setDefaultQualityStrategy(resourceDefaults.default_quality === 'highest' ? 'highest' : 'selected')
    if (!params.has('result_limit') && !sessionId) {
      setResultLimit(String(loadResourcePreferences({ resultLimit: resourceDefaults.result_limit, exactMatch: resourceDefaults.exact_match }).resultLimit))
    }
    if (!sessionId) setExactMatch(loadResourcePreferences({ resultLimit: resourceDefaults.result_limit, exactMatch: resourceDefaults.exact_match }).exactMatch)
  }, [resourceDefaults, routeRuleId])

  useEffect(() => {
    if (sessionId || !settings.isSuccess || sourceId === 'all') return
    if (!resourceSites.some((site) => site.id === sourceId)) setSourceId('all')
  }, [resourceSites, sessionId, settings.isSuccess, sourceId])

  useEffect(() => {
    if (!sessionId) {
      hydratedSessionRef.current = ''
    }
    setSelectedIds(new Set())
    setHistoryConfirm(null)
    setPage(1)
    setKeyword('')
    setVariant('')
    setContinuationError('')
    queueAttemptRef.current = null
    queueErrorToastRef.current = ''
  }, [sessionId])

  const keywordFilter = useMemo(() => resourceKeywordFilter(keyword), [keyword])

  const searchQuery = useQuery({
    queryKey: ['resource-search', sessionId, pageSize, page, keywordFilter.keyword, variant],
    queryFn: () => api.resourceSearch({
      sessionId,
      limit: pageSize,
      offset: (page - 1) * pageSize,
      keyword: keywordFilter.keyword,
      variant,
    }),
    enabled: Boolean(sessionId),
    retry: retryTransientApiRequest,
    placeholderData: (previous) => previous?.search.session_id === sessionId ? previous : undefined,
    refetchInterval: (state) => {
      const status = state.state.data?.search.status
      return status && ACTIVE_RESOURCE_STATUSES.has(status) ? 1_000 : false
    },
    refetchIntervalInBackground: false,
  })
  const session = searchQuery.data?.search ?? null
  const resourceSearchActive = Boolean(
    session && ACTIVE_RESOURCE_STATUSES.has(session.status),
  )

  useEffect(() => {
    if (!session || hydratedSessionRef.current === session.session_id) return
    hydratedSessionRef.current = session.session_id
    setSourceId(session.source_id)
    setQuery(session.query)
    setResultLimit(String(session.result_limit))
    if (typeof session.exact_match === 'boolean') setExactMatch(session.exact_match)
    setRangeStart(session.start === null ? '' : String(session.start).padStart(session.suffix_width ?? 0, '0'))
    setRangeEnd(session.end === null ? '' : String(session.end).padStart(session.suffix_width ?? 0, '0'))
    setResultLimitError('')
    setFormError('')
  }, [session])

  useEffect(() => {
    if (!session) return
    setContinuationLimit((current) => (
      current > session.result_limit
        ? current
        : Math.min(999, session.result_limit + Math.max(50, Math.min(200, session.result_limit)))
    ))
  }, [session?.result_limit, session?.session_id])

  useEffect(() => {
    if (!session) return
    const pageCount = Math.max(1, Math.ceil(session.pagination.total / pageSize))
    if (page > pageCount) setPage(pageCount)
  }, [page, pageSize, session?.pagination.total])

  const startSearch = useMutation({
    mutationFn: api.createResourceSearch,
    onSuccess: (payload, variables) => {
      const params = new URLSearchParams({
        workspace: 'resource',
        resource_id: payload.search.session_id,
        resource_query: variables.query,
        result_limit: String(variables.result_limit),
      })
      if (variables.start && variables.end) {
        params.set('start', variables.start)
        params.set('end', variables.end)
      }
      setContinuationError('')
      navigate(`/search?${params.toString()}`, { replace: true })
      toast.push('资源扫描已开始', 'success')
    },
    onError: (error) => setFormError((error as Error).message),
  })

  const resourceAction = useMutation({
    mutationFn: api.resourceSearchAction,
    onSuccess: async (payload, variables) => {
      setContinuationError('')
      if ('removed' in payload) {
        queryClient.removeQueries({ queryKey: ['resource-search', variables.session_id] })
        navigate('/search?workspace=resource', { replace: true })
        toast.push('资源搜索记录已清除', 'success')
        return
      }
      await queryClient.invalidateQueries({ queryKey: ['resource-search', variables.session_id] })
      if (variables.action === 'continue' || variables.action === 'retry') queueAttemptRef.current = null
      if (variables.action === 'continue') toast.push('已从保存的进度继续搜索', 'success')
      if (variables.action === 'retry') toast.push('正在从失败页重试', 'success')
      if (variables.action === 'cancel') toast.push('已取消资源扫描', 'success')
    },
    onError: (error, variables) => {
      const message = (error as Error).message
      if (variables.action === 'continue') setContinuationError(message)
      else toast.push(message, 'error')
    },
  })

  const service = useQuery({
    queryKey: ['web-download-service'],
    queryFn: () => api.webDownloads({ limit: 1 }),
    retry: false,
    staleTime: 15_000,
  })
  const serviceReady = service.data?.configured === true && service.data?.available !== false && service.data?.enabled !== false

  const rulesQuery = useQuery({
    queryKey: ['web-download-batch-rules'],
    queryFn: api.webDownloadBatchRules,
    enabled: rulePanelOpen || Boolean(routeRuleId && routeRuleRevision),
    retry: false,
  })
  const routeRuleMatch = rulesQuery.data?.rules.find((candidate) => (
    candidate.rule_id === routeRuleId && candidate.revision === routeRuleRevision
  ))

  const saveRule = useMutation({
    mutationFn: api.saveWebDownloadBatchRule,
    onSuccess: (payload, variables) => {
      setRuleName('')
      setEditRule(null)
      setAppliedRule({ ruleId: payload.rule.rule_id, revision: payload.rule.revision, name: payload.rule.name })
      toast.push(`${variables.rule_id ? '已更新' : '已新建'}规则“${payload.rule.name}”`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-download-batch-rules'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  const removeRule = useMutation({
    mutationFn: (rule: WebDownloadBatchRule) => api.removeWebDownloadBatchRule(rule.rule_id, rule.revision),
    onSuccess: (_payload, rule) => {
      setRemoveRuleCandidateId('')
      if (appliedRule?.ruleId === rule.rule_id) setAppliedRule(null)
      if (editRule?.ruleId === rule.rule_id) {
        setEditRule(null)
        setRuleName('')
      }
      toast.push('批量规则已删除', 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-download-batch-rules'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  useEffect(() => {
    if (!routeRuleId || !routeRuleRevision || routeRuleApplied || !rulesQuery.data) return
    const rule = routeRuleMatch
    if (!rule) return
    setQuery(rule.code_or_prefix)
    setRangeStart(rule.start ?? '')
    setRangeEnd(rule.end ?? '')
    setMaxHeight(rule.max_height)
    setVariantPriority([...rule.variant_priority])
    setExistingPolicy(rule.existing_policy)
    setDefaultQualityStrategy(rule.default_quality_strategy)
    setDefaultHeight(rule.default_height ?? rule.max_height)
    setRuleSelection(rule.selection_mode)
    setRuleName(rule.name)
    setEditRule({ ruleId: rule.rule_id, revision: rule.revision, name: rule.name })
    setAppliedRule({ ruleId: rule.rule_id, revision: rule.revision, name: rule.name })
    setRouteRuleApplied(true)
    setFormError('')
  }, [routeRuleApplied, routeRuleId, routeRuleMatch, routeRuleRevision])

  const queueDownloads = useMutation({
    mutationFn: api.createResourceSearchDownloads,
    onSuccess: (payload) => {
      queueAttemptRef.current = null
      queueErrorToastRef.current = ''
      setSelectedIds(new Set())
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      toast.push(backgroundSubmissionMessage(payload.batch), 'success')
      navigate('/downloads?view=web', { state: { skipStoredWebDownloadBatchPreview: true } })
    },
    onError: (error, variables) => {
      if (queueErrorToastRef.current === variables.idempotency_key) return
      queueErrorToastRef.current = variables.idempotency_key
      toast.push(`添加后台任务失败：${(error as Error).message}。可直接重试，系统不会重复创建任务。`, 'error')
    },
  })

  useEffect(() => {
    queueDownloads.reset()
  }, [sessionId])

  function validatedDiscoveryInput(): { query: string; start: string; end: string } | null {
    const cleanQuery = normalizeSearchQuery(query)
    const cleanStart = rangeStart.trim()
    const cleanEnd = rangeEnd.trim()
    if (!cleanQuery) {
      setFormError('请输入番号或关键词')
      return null
    }
    if (searchTerms(cleanQuery).length > 16) {
      setFormError('搜索最多支持 16 个关键词')
      return null
    }
    if (Array.from(cleanQuery).length > 80 || Array.from(cleanQuery).some((character) => {
      const codePoint = character.codePointAt(0) ?? 0
      return codePoint < 32 || (codePoint >= 127 && codePoint <= 159)
    })) {
      setFormError('搜索内容最长 80 个字符，且不能包含控制字符')
      return null
    }
    if (Boolean(cleanStart) !== Boolean(cleanEnd)) {
      setFormError('起始和结束序号必须同时填写')
      return null
    }
    if (cleanStart && (!/^\d{1,9}$/.test(cleanStart) || !/^\d{1,9}$/.test(cleanEnd))) {
      setFormError('范围仅接受 1 到 9 位数字')
      return null
    }
    if (cleanStart && (
      cleanQuery.length > 24
      || !/^[A-Za-z0-9]+(?:[-._][A-Za-z0-9]+)*$/.test(cleanQuery)
      || (cleanQuery.match(/[A-Za-z]/g)?.length ?? 0) < 2
    )) {
      setFormError('指定范围时请输入可识别的系列番号')
      return null
    }
    if (cleanStart && BigInt(cleanStart) > BigInt(cleanEnd)) {
      setFormError('结束序号不能小于起始序号')
      return null
    }
    setFormError('')
    return { query: cleanQuery, start: cleanStart, end: cleanEnd }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (resourceSearchActive) return
    const input = validatedDiscoveryInput()
    if (!input) return
    const validatedResultLimit = parseResultLimit(resultLimit)
    if (validatedResultLimit === null) {
      setResultLimitError(RESULT_LIMIT_ERROR)
      return
    }
    if (!sourceAvailable) {
      setFormError('没有已启用的 Web 视频资源站点')
      return
    }
    saveResourcePreferences({ resultLimit: validatedResultLimit, exactMatch })
    startSearch.mutate({
      source_id: sourceId,
      query: input.query,
      result_limit: validatedResultLimit,
      exact_match: exactMatch,
      ...(input.start ? {
        start: input.start,
        end: input.end,
        suffix_width: Math.max(input.start.length, input.end.length),
      } : {}),
    })
  }

  function saveCurrentRule() {
    const input = validatedDiscoveryInput()
    if (!input) return
    const name = ruleName.trim()
    if (!name) {
      toast.push('请输入规则名称', 'error')
      return
    }
    saveRule.mutate({
      ...(editRule ? { rule_id: editRule.ruleId, expected_revision: editRule.revision } : {}),
      code_or_prefix: input.query,
      max_height: maxHeight,
      variant_priority: variantPriority,
      existing_policy: existingPolicy,
      ...(input.start ? { start: input.start, end: input.end } : {}),
      name,
      default_quality_strategy: defaultQualityStrategy,
      ...(defaultQualityStrategy === 'selected' ? { default_height: defaultHeight } : {}),
      selection_mode: ruleSelection,
    })
  }

  function clearAppliedRule() {
    queueDownloads.reset()
    if (appliedRule) setAppliedRule(null)
  }

  function applyRule(rule: WebDownloadBatchRule, announce = true) {
    queueDownloads.reset()
    setQuery(rule.code_or_prefix)
    setRangeStart(rule.start ?? '')
    setRangeEnd(rule.end ?? '')
    setMaxHeight(rule.max_height)
    setVariantPriority([...rule.variant_priority])
    setExistingPolicy(rule.existing_policy)
    setDefaultQualityStrategy(rule.default_quality_strategy)
    setDefaultHeight(rule.default_height ?? rule.max_height)
    setRuleSelection(rule.selection_mode)
    setRuleName(rule.name)
    setEditRule({ ruleId: rule.rule_id, revision: rule.revision, name: rule.name })
    setAppliedRule({ ruleId: rule.rule_id, revision: rule.revision, name: rule.name })
    setFormError('')
    if (announce) toast.push(`已载入规则“${rule.name}”，确认条件后开始搜索`, 'success')
  }

  function startNewRule() {
    setRuleName('')
    setEditRule(null)
    setAppliedRule(null)
  }

  function runResourceAction(action: 'continue' | 'retry' | 'cancel' | 'remove') {
    if (!session) return
    resourceAction.mutate({
      session_id: session.session_id,
      expected_revision: session.revision,
      action,
      ...(action === 'continue' ? { result_limit: continuationLimit } : {}),
    })
  }

  function toggleItem(itemId: string, checked: boolean) {
    queueDownloads.reset()
    setSelectedIds((current) => {
      const next = new Set(current)
      if (checked) next.add(itemId)
      else next.delete(itemId)
      return next
    })
  }

  function toggleCurrentPage() {
    if (!session) return
    queueDownloads.reset()
    const allSelected = session.items.length > 0 && session.items.every((item) => selectedIds.has(item.item_id))
    setSelectedIds((current) => {
      const next = new Set(current)
      session.items.forEach((item) => {
        if (allSelected) next.delete(item.item_id)
        else next.add(item.item_id)
      })
      return next
    })
  }

  async function requestSelectedDownloads() {
    if (!session || !selectedIds.size || checkingHistory) return
    setHistoryConfirm(null)
    const selected = Array.from(selectedIds)
    const codeById = new Map(session.items.map((item) => [item.item_id, item.code]))
    const codes = selected.map((id) => codeById.get(id)).filter((code): code is string => Boolean(code))
    if (codes.length === selected.length && codes.length) {
      setCheckingHistory(true)
      try {
        const lookup = await api.downloadHistoryLookup(codes)
        const flagged = new Map(lookup.items.filter((item) => item.state !== 'none').map((item) => [item.code, item]))
        const duplicateIds = selected.filter((id) => flagged.has(codeById.get(id) ?? ''))
        if (duplicateIds.length) {
          const examples = duplicateIds.slice(0, 3).map((id) => {
            const code = codeById.get(id) ?? ''
            return `${code}（${downloadHistorySummary(flagged.get(code))}）`
          })
          setHistoryConfirm({
            duplicateIds,
            summary: `${duplicateIds.length} 项已有下载记录：${examples.join('、')}${duplicateIds.length > 3 ? ' 等' : ''}`,
          })
          return
        }
      } catch {
        // Advisory only: the batch still applies the 已有作品 policy server-side.
      } finally {
        setCheckingHistory(false)
      }
    }
    queueSelectedDownloads()
  }

  function queueSelectedDownloads(excludedIds: readonly string[] = []) {
    setHistoryConfirm(null)
    if (!session || !selectedIds.size) return
    const excluded = new Set(excludedIds)
    const itemIds = Array.from(selectedIds).filter((id) => !excluded.has(id)).sort()
    if (!itemIds.length) {
      toast.push('选中的作品都已有下载记录，未创建新任务', 'info')
      return
    }
    const fingerprint = JSON.stringify({
      session_id: session.session_id,
      item_ids: itemIds,
      max_height: maxHeight,
      existing_policy: existingPolicy,
      variant_priority: variantPriority,
      default_quality_strategy: defaultQualityStrategy,
      default_height: defaultQualityStrategy === 'selected' ? defaultHeight : null,
      rule_id: appliedRule?.ruleId ?? null,
      rule_revision: appliedRule?.revision ?? null,
    })
    const previousAttempt = queueAttemptRef.current?.fingerprint === fingerprint
      ? queueAttemptRef.current
      : null
    const request: ResourceSearchDownloadsRequest = {
      session_id: session.session_id,
      expected_revision: previousAttempt?.request.expected_revision ?? session.revision,
      item_ids: itemIds,
      max_height: maxHeight,
      existing_policy: existingPolicy,
      variant_priority: variantPriority,
      default_quality_strategy: defaultQualityStrategy,
      ...(defaultQualityStrategy === 'selected' ? { default_height: defaultHeight } : {}),
      ...(appliedRule ? {
        rule_id: appliedRule.ruleId,
        rule_revision: appliedRule.revision,
      } : {}),
      idempotency_key: previousAttempt?.request.idempotency_key ?? createIdempotencyKey(),
    }
    queueAttemptRef.current = { fingerprint, request }
    queueDownloads.mutate(request)
  }

  const visibleCodes = useMemo(() => (session?.items ?? []).map((item) => item.code), [session?.items])
  const pageHistory = useQuery({
    queryKey: ['download-history', visibleCodes],
    queryFn: () => api.downloadHistoryLookup(visibleCodes),
    enabled: visibleCodes.length > 0,
    staleTime: 30_000,
    retry: false,
  })
  const historyByCode = useMemo(() => new Map(
    (pageHistory.data?.items ?? []).map((item) => [item.code, item]),
  ), [pageHistory.data])
  const pageAllSelected = Boolean(session?.items.length) && session?.items.every((item) => selectedIds.has(item.item_id))
  const terminalSearch = Boolean(session && !ACTIVE_RESOURCE_STATUSES.has(session.status))
  const resultPagePending = searchQuery.isPlaceholderData
  const resultPageCount = Math.max(1, Math.ceil((session?.pagination.total ?? 0) / pageSize))
  const pendingResourceAction = resourceAction.isPending ? resourceAction.variables?.action : undefined
  const canContinue = Boolean(session?.can_continue && session.result_limit < 999)
  const sourceAvailable = sourceId === 'all' ? resourceSites.length > 0 : resourceSites.some((site) => site.id === sourceId)
  const selectedSourceIds = sourceId === 'all' ? resourceSites.map((site) => site.id) : [sourceId]
  const selectedSourceDiagnosticFailures = diagnosticFailureLabels(diagnostics.data?.statuses ?? [], selectedSourceIds)
  const sessionSourceDiagnosticFailures = diagnosticFailureLabels(diagnostics.data?.statuses ?? [], session?.source_ids ?? [])
  const failedSources = session?.sources.filter((source) => source.status === 'failed') ?? []
  const sourceName = (siteId: string) => settings.data?.settings.sites.find((site) => site.id === siteId)?.name ?? siteId
  const selectedSourceDiagnosticState = resourceDiagnosticState(
    diagnostics.data?.statuses ?? [],
    selectedSourceIds,
    diagnostics.isSuccess,
  )
  const sessionSourceDiagnosticState = resourceDiagnosticState(
    diagnostics.data?.statuses ?? [],
    session?.source_ids ?? [],
    diagnostics.isSuccess,
  )

  return (
    <div className="resource-search-workspace">
      <form className="resource-search-form" onSubmit={submit}>
        <div className="resource-search-form-primary">
          <Field label="Web 视频资源站点">
            <select value={sourceId} onChange={(event) => setSourceId(event.target.value)} disabled={startSearch.isPending}>
              <option value="all">全部启用站点（聚合）</option>
              {sourceId !== 'all' && !sourceAvailable ? <option value={sourceId}>当前记录来源（已停用）</option> : null}
              {resourceSites.map((site) => <option value={site.id} key={site.id}>{site.name}{unhealthyResourceSites.has(site.id) ? '（近期诊断异常）' : ''}</option>)}
            </select>
          </Field>
          <Field label="番号或关键词" className="resource-search-query">
            <div className="input-with-icon">
              <Search aria-hidden="true" />
              <input value={query} onChange={(event) => { clearAppliedRule(); setQuery(event.target.value) }} placeholder="请输入番号或关键词" maxLength={80} autoComplete="off" enterKeyHint="search" aria-describedby="resource-search-query-hint" />
            </div>
          </Field>
          <Field label="总结果上限" error={resultLimitError} errorId="resource-search-result-limit-error">
            <input
              type="number"
              min={1}
              max={999}
              value={resultLimit}
              onChange={(event) => {
                setResultLimit(event.target.value)
                setResultLimitError('')
              }}
              aria-invalid={Boolean(resultLimitError)}
              aria-describedby={resultLimitError ? 'resource-search-result-limit-error' : undefined}
            />
          </Field>
          <Button type="submit" variant="primary" disabled={!query.trim() || !sourceAvailable || startSearch.isPending || resourceSearchActive}>
            <ScanSearch aria-hidden="true" />
            {startSearch.isPending ? '正在创建' : resourceSearchActive ? '扫描进行中' : '开始搜索'}
          </Button>
        </div>
        <div className="resource-search-options">
          <Toggle
            label="精确匹配"
            checked={exactMatch}
            onChange={(event) => { setExactMatch(event.target.checked); saveResourcePreferences({ exactMatch: event.target.checked }) }}
            aria-describedby="resource-search-query-hint"
          />
          <p className="resource-search-query-hint" id="resource-search-query-hint">
            精确匹配只保留与输入番号前缀完全一致的作品，结果上限按匹配后的数量计算。FC2 番号请保留 FC2 前缀；同一番号会合并并标明来源。
          </p>
        </div>
        <details className="resource-search-range">
          <summary>指定番号范围</summary>
          <div>
            <Field label="起始序号"><input inputMode="numeric" value={rangeStart} onChange={(event) => { clearAppliedRule(); setRangeStart(event.target.value) }} maxLength={9} /></Field>
            <Field label="结束序号"><input inputMode="numeric" value={rangeEnd} onChange={(event) => { clearAppliedRule(); setRangeEnd(event.target.value) }} maxLength={9} /></Field>
            <span>留空时按站点结果继续扫描；范围只限制本次资源发现。</span>
          </div>
        </details>
        {formError ? <span className="field-error" role="alert">{formError}</span> : null}
      </form>

      {settings.isError ? <InlineNotice tone="danger" role="alert">无法读取资源站点配置：{serviceErrorMessage(settings.error, '请检查服务连接后重试')}</InlineNotice> : null}
      {diagnostics.isError ? (
        <InlineNotice tone="warning" role="status">
          站点诊断状态暂时无法读取：{serviceErrorMessage(diagnostics.error, '请稍后重试')}。当前不会把来源视为健康站点。
          <Button type="button" size="small" variant="ghost" onClick={() => void diagnostics.refetch()} disabled={diagnostics.isFetching}>
            <RefreshCw className={diagnostics.isFetching ? 'spin' : ''} aria-hidden="true" />
            {diagnostics.isFetching ? '重试中' : '重新读取诊断'}
          </Button>
        </InlineNotice>
      ) : null}
      {!settings.isLoading && !resourceSites.length ? <InlineNotice tone="warning">没有启用资源搜索能力的站点。</InlineNotice> : null}
      {sourceId !== 'all' && !sourceAvailable ? <InlineNotice tone="warning">当前记录使用的来源已停用；请先选择其他站点再开始新的搜索。</InlineNotice> : null}
      {selectedSourceDiagnosticFailures.length ? (
        <InlineNotice tone="warning" role="status">
          所选来源近期出现异常：{selectedSourceDiagnosticFailures.join('、')}。搜索仍会尝试这些来源，并分别显示结果与失败原因。
          <Button type="button" size="small" variant="ghost" onClick={() => navigate('/sites')}>查看站点诊断</Button>
        </InlineNotice>
      ) : null}
      {sourceAvailable && selectedSourceDiagnosticState === 'unknown' && diagnostics.isSuccess ? (
        <InlineNotice tone="info" role="status">部分所选来源尚无可用诊断记录，搜索会按实际访问结果显示各站状态。</InlineNotice>
      ) : null}
      {invalidSessionId ? <InlineNotice tone="danger" role="alert">链接中的资源搜索记录编号无效，请重新开始搜索。</InlineNotice> : null}
      {invalidRuleLink ? <InlineNotice tone="warning" role="alert">链接中的批量规则编号或版本无效，未应用该规则。</InlineNotice> : null}
      {routeRuleId && routeRuleRevision && rulesQuery.isSuccess && !routeRuleMatch ? <InlineNotice tone="warning" role="alert">链接中的批量规则已不存在或版本已变化，请在规则列表中重新选择。</InlineNotice> : null}
      {sessionId && searchQuery.isError && !session ? (
        <EmptyState
          title={searchQuery.error instanceof ApiError && searchQuery.error.status === 404 ? '资源搜索记录不存在' : '暂时无法读取资源搜索记录'}
          description={searchQuery.error instanceof ApiError && searchQuery.error.status === 404 ? '该记录可能已经被清除，请重新开始搜索。' : '已保存的搜索进度不会丢失，可以稍后重试。'}
          action={<Button type="button" size="small" onClick={() => void searchQuery.refetch()} disabled={searchQuery.isFetching}><RefreshCw aria-hidden="true" />{searchQuery.isFetching ? '正在重试' : '重试'}</Button>}
        />
      ) : null}

      <section className="resource-rule-manager" aria-labelledby="resource-rule-title">
        <header>
          <div>
            <h2 id="resource-rule-title">批量规则</h2>
            <span>保存搜索范围、版本优先级、画质和已有作品处理方式。</span>
          </div>
          <Button type="button" size="small" variant="ghost" onClick={() => setRulePanelOpen((current) => !current)}>
            <Settings aria-hidden="true" />
            {rulePanelOpen ? '收起规则' : '管理规则'}
          </Button>
        </header>
        {rulePanelOpen ? (
          <div className="resource-rule-content">
            <div className="resource-rule-editor">
              {editRule ? <StatusBadge tone="info">正在编辑：{editRule.name} · v{editRule.revision}</StatusBadge> : null}
              <Field label="规则名称">
                <input value={ruleName} onChange={(event) => setRuleName(event.target.value)} placeholder="请输入规则名称" maxLength={120} />
              </Field>
              <Field label="选择条件">
                <select value={ruleSelection} onChange={(event) => { clearAppliedRule(); setRuleSelection(event.target.value as WebDownloadBatchRuleSelection) }}>
                  <option value="all">全部作品</option>
                  <option value="missing">仅媒体库缺失</option>
                  <option value="upgrades">仅可升级画质</option>
                </select>
              </Field>
              {editRule ? (
                <Button type="button" size="small" variant="ghost" onClick={startNewRule} disabled={saveRule.isPending}>
                  新建规则
                </Button>
              ) : null}
              <Button type="button" size="small" variant="primary" onClick={saveCurrentRule} disabled={saveRule.isPending}>
                <Save aria-hidden="true" />
                {saveRule.isPending ? (editRule ? '更新中' : '新建中') : editRule ? '更新规则' : '新建规则'}
              </Button>
              <span>保存和载入规则都不会访问站点或创建下载任务。</span>
            </div>
            {rulesQuery.isError ? <InlineNotice tone="warning" role="alert">批量规则暂不可用，可稍后重试。</InlineNotice> : null}
            {rulesQuery.data?.rules.length ? (
              <ul className="resource-rule-list" aria-label="已保存的批量规则">
                {rulesQuery.data.rules.map((rule) => (
                  <li key={rule.rule_id}>
                    <div>
                      <strong>{rule.name}</strong>
                      <span>
                        {rule.code_or_prefix} · {rule.start && rule.end ? `${rule.start}-${rule.end}` : '全部范围'} · {variantPriorityLabel(rule.variant_priority)} · {rule.default_quality_strategy === 'highest' ? '最高可用' : `${rule.default_height ?? rule.max_height}p`}
                      </span>
                    </div>
                    {appliedRule?.ruleId === rule.rule_id && appliedRule.revision === rule.revision ? <StatusBadge tone="success">已应用</StatusBadge> : null}
                    {editRule?.ruleId === rule.rule_id && editRule.revision === rule.revision ? <StatusBadge tone="info">编辑中</StatusBadge> : null}
                    <Button type="button" size="small" variant="ghost" onClick={() => applyRule(rule)} disabled={queueDownloads.isPending}>
                      应用条件
                    </Button>
                    {removeRuleCandidateId === rule.rule_id ? (
                      <>
                        <Button type="button" size="small" variant="danger" aria-label={`确认删除规则 ${rule.name}`} onClick={() => removeRule.mutate(rule)} disabled={removeRule.isPending}>
                          {removeRule.isPending ? '正在删除' : '确认删除'}
                        </Button>
                        <IconButton type="button" size="small" label={`取消删除规则 ${rule.name}`} onClick={() => setRemoveRuleCandidateId('')} disabled={removeRule.isPending}>
                          <X aria-hidden="true" />
                        </IconButton>
                      </>
                    ) : (
                      <IconButton type="button" size="small" label={`删除规则 ${rule.name}`} className="danger-icon" onClick={() => setRemoveRuleCandidateId(rule.rule_id)} disabled={removeRule.isPending}>
                        <Trash2 aria-hidden="true" />
                      </IconButton>
                    )}
                  </li>
                ))}
              </ul>
            ) : rulesQuery.isSuccess ? <span className="resource-rule-empty">尚未保存批量规则。</span> : null}
          </div>
        ) : null}
      </section>

      {session ? (
        <section className="resource-search-session" aria-labelledby="resource-search-session-title">
          <header className="resource-search-session-header">
            <div>
              <h2 id="resource-search-session-title">{session.query}</h2>
              <StatusBadge tone={resourceStatusTone(session, sessionSourceDiagnosticState)}>{resourceStatusLabel(session, sessionSourceDiagnosticState)}</StatusBadge>
              <span>{session.progress.items_found} / {session.result_limit} 部</span>
              <span>{session.progress.scanned_pages} 页已完成</span>
            </div>
            <div>
              {session.can_cancel ? <Button type="button" size="small" variant="ghost" onClick={() => runResourceAction('cancel')} disabled={resourceAction.isPending} aria-busy={pendingResourceAction === 'cancel'}><X aria-hidden="true" />{pendingResourceAction === 'cancel' ? '正在取消' : '取消'}</Button> : null}
              {session.can_retry ? <Button type="button" size="small" variant="ghost" onClick={() => runResourceAction('retry')} disabled={resourceAction.isPending} aria-busy={pendingResourceAction === 'retry'}><RefreshCw aria-hidden="true" />{pendingResourceAction === 'retry' ? '正在重试' : '从失败页重试'}</Button> : null}
              {session.can_remove ? <IconButton type="button" size="small" label={pendingResourceAction === 'remove' ? '正在清除资源搜索' : '清除资源搜索'} className="danger-icon" onClick={() => runResourceAction('remove')} disabled={resourceAction.isPending} aria-busy={pendingResourceAction === 'remove'}><Trash2 aria-hidden="true" /></IconButton> : null}
            </div>
          </header>
          <ul className="resource-source-statuses" aria-label="各来源搜索状态" aria-live="polite">
            {session.sources.map((source) => (
              <li key={source.source_id}>
                <strong>{sourceName(source.source_id)}</strong>
                <StatusBadge tone={source.status === 'failed' ? 'danger' : source.status === 'completed' && source.item_count > 0 ? 'success' : source.status === 'cancelled' || source.status === 'limit_reached' ? 'warning' : 'info'}>
                  {source.status === 'completed' && source.item_count === 0 ? '已扫描，0 部' : RESOURCE_STATUS_LABELS[source.status]}
                </StatusBadge>
                <span>{source.item_count} 部</span>
                {source.status === 'failed' ? <span className="resource-source-error">{resourceErrorLabel(source.error_code)}{source.retryable ? '（可重试）' : ''}</span> : null}
              </li>
            ))}
          </ul>
          {ACTIVE_RESOURCE_STATUSES.has(session.status) ? (
            <div className="resource-search-progress" aria-live="polite">
              <ProgressBar value={progressValue(session)} label="资源搜索进度" />
              <span>
                {session.progress.determinate && session.progress.total_pages
                  ? `${session.progress.scanned_pages} / ${session.progress.total_pages} 页`
                  : `已扫描 ${session.progress.scanned_pages} 页${session.source_ids.length === 1 && session.progress.next_page !== null ? `，下一页 ${session.progress.next_page}` : ''}`}
                {session.progress.pending_remaining ? `，${session.source_id === 'all' ? '待展示' : '本页剩余'} ${session.progress.pending_remaining}` : ''}
              </span>
            </div>
          ) : null}
          {session.status === 'failed' ? <InlineNotice tone="danger" role="alert">{resourceErrorLabel(session.error_code)}。已找到的 {session.item_count} 部作品和续搜位置均已保留。</InlineNotice> : null}
          {session.status !== 'failed' && failedSources.length > 0 ? (
            <InlineNotice tone="warning" role="status">
              {failedSources.map((source) => sourceName(source.source_id)).join('、')}扫描失败。{resourceSearchActive ? '其余来源仍在扫描；' : ''}已获取的 {session.item_count} 部作品仍可筛选、选择和下载。
            </InlineNotice>
          ) : null}
          {session.status === 'completed' && session.item_count === 0 && failedSources.length === 0 ? (
            <InlineNotice tone={sessionSourceDiagnosticState === 'healthy' ? 'info' : 'warning'} role="status">
              {sessionSourceDiagnosticState === 'failed'
                ? `来源站点近期连续异常（${sessionSourceDiagnosticFailures.join('、')}），本次 0 结果不能视为已确认无资源。请切换站点或完成诊断后重试。`
                : sessionSourceDiagnosticState === 'unknown'
                  ? '站点诊断状态未知，本次 0 结果尚不能确认代表无资源。请重新读取站点诊断或切换来源后重试。'
                  : '扫描已结束但未找到匹配资源。可调整关键词、切换站点或在站点诊断中确认页面结构仍然有效。'}
            </InlineNotice>
          ) : null}
          {session.status === 'cancelled' ? <InlineNotice tone="warning">扫描已取消，现有结果和续搜位置均已保留。</InlineNotice> : null}
          {session.status === 'limit_reached' ? (
            <InlineNotice tone="info">
              {canContinue
                ? '已达到本次总结果上限。提高累计上限即可从保存位置继续，无需重扫已完成页面。'
                : '已达到资源搜索的 999 部硬上限，可先用筛选缩小结果或新建更精确的搜索。'}
            </InlineNotice>
          ) : null}
          {canContinue ? (
            <div className="resource-search-continuation">
              <Field label="续搜累计上限">
                <input type="number" min={session.result_limit + 1} max={999} value={continuationLimit} disabled={resourceAction.isPending} onChange={(event) => setContinuationLimit(Math.min(999, Math.max(session.result_limit + 1, Number(event.target.value) || session.result_limit + 1)))} />
              </Field>
              <Button type="button" variant="primary" onClick={() => runResourceAction('continue')} disabled={resourceAction.isPending || continuationLimit <= session.result_limit || continuationLimit > 999} aria-busy={pendingResourceAction === 'continue'}>
                <ScanSearch aria-hidden="true" />
                {pendingResourceAction === 'continue' ? '正在续搜' : '继续搜索'}
              </Button>
              <span>{session.source_id === 'all' ? '从各来源保存的位置继续' : `从第 ${session.progress.next_page} 页继续`}，现有结果、筛选和选择保持不变。</span>
            </div>
          ) : null}
          {continuationError ? <InlineNotice tone="warning" role="alert">续搜暂时失败：{continuationError}。现有结果未受影响。</InlineNotice> : null}

          <div className="resource-result-toolbar" ref={resultStartRef}>
            <Field label="结果关键词" error={keywordFilter.error} errorId={RESOURCE_KEYWORD_ERROR_ID}>
              <div className="input-with-icon"><Filter aria-hidden="true" /><input value={keyword} onChange={(event) => { setKeyword(event.target.value); setPage(1) }} placeholder="空格分隔多个关键词" maxLength={80} aria-label="结果关键词" aria-invalid={Boolean(keywordFilter.error)} aria-describedby={keywordFilter.error ? RESOURCE_KEYWORD_ERROR_ID : undefined} /></div>
            </Field>
            <Field label="资源分类">
              <select value={variant} onChange={(event) => { setVariant(event.target.value as ResourceSearchVariant | ''); setPage(1) }}>
                <option value="">全部分类</option>
                {WEB_DOWNLOAD_VARIANTS.map((item) => <option value={item} key={item}>{webDownloadVariantLabel(item)}</option>)}
              </select>
            </Field>
            <Field label="每页显示">
              <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value) as (typeof RESOURCE_PAGE_SIZES)[number]); setPage(1) }}>
                {RESOURCE_PAGE_SIZES.map((value) => <option value={value} key={value}>{value}</option>)}
              </select>
            </Field>
            <div className="resource-selection-summary" aria-live="polite">
              <strong>{selectedIds.size}</strong>
              <span>项已选</span>
            </div>
          </div>

          {searchQuery.isFetching && !searchQuery.data ? <SkeletonRows count={5} /> : null}
          {searchQuery.isError && session ? <InlineNotice tone="warning" role="alert">暂时无法刷新结果：{(searchQuery.error as Error).message}</InlineNotice> : null}
          {session.items.length ? (
            <>
              <div className="resource-result-actions">
                <Button type="button" size="small" variant="ghost" onClick={toggleCurrentPage} disabled={resultPagePending}>
                  <CheckSquare2 aria-hidden="true" />
                  {pageAllSelected ? '取消本页' : '选择本页'}
                </Button>
                <span role="status" aria-live="polite">{resultPagePending ? '正在刷新结果' : `${session.pagination.total} 部符合当前筛选`}</span>
              </div>
              <ResultPagination
                ariaLabel="资源结果顶部分页"
                className="resource-pager"
                page={page}
                pageCount={resultPageCount}
                disabled={resultPagePending}
                onPageChange={setPage}
              />
              <ul className="resource-result-list" aria-label="Web 视频资源搜索结果" aria-busy={resultPagePending}>
                {session.items.map((item) => (
                  <li className={selectedIds.has(item.item_id) ? 'selected' : ''} key={item.item_id}>
                    <label>
                      <input type="checkbox" checked={selectedIds.has(item.item_id)} disabled={resultPagePending} onChange={(event) => toggleItem(item.item_id, event.target.checked)} />
                      <div className="resource-result-copy">
                        <code>{item.code}</code>
                        {item.title ? <span className="resource-result-title" title={item.title}>{item.title}</span> : null}
                        <span className="resource-result-sources">来源：{item.source_ids.map(sourceName).join('、')}</span>
                      </div>
                      {downloadHistoryBadge(historyByCode.get(item.code)) ? (
                        <StatusBadge tone={downloadHistoryBadge(historyByCode.get(item.code))!.tone}>
                          {downloadHistoryBadge(historyByCode.get(item.code))!.label}
                        </StatusBadge>
                      ) : null}
                      <span>{item.available_variants.map((itemVariant) => <StatusBadge key={itemVariant}>{webDownloadVariantLabel(itemVariant)}</StatusBadge>)}</span>
                    </label>
                  </li>
                ))}
              </ul>
              <ResultPagination
                ariaLabel="资源结果底部分页"
                className="resource-pager"
                page={page}
                pageCount={resultPageCount}
                disabled={resultPagePending}
                onPageChange={(nextPage) => {
                  setPage(nextPage)
                  resultStartRef.current?.scrollIntoView?.({ block: 'start' })
                }}
              />
            </>
          ) : terminalSearch ? <EmptyState title="当前筛选没有结果" description={session.item_count ? '调整关键词或分类即可恢复结果' : '可提高总结果上限或更换搜索条件'} /> : null}

          <div className="resource-batch-composer">
            {appliedRule ? <StatusBadge tone="success">规则：{appliedRule.name} · v{appliedRule.revision}</StatusBadge> : null}
            <div className="resource-batch-controls">
              <Field label="画质上限"><select value={maxHeight} onChange={(event) => { clearAppliedRule(); const height = Number(event.target.value); setMaxHeight(height); if (defaultHeight > height) setDefaultHeight(height) }} disabled={queueDownloads.isPending}>{QUALITY_LIMITS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></Field>
              <Field label="默认画质">
                <select
                  value={defaultQualityStrategy === 'highest' ? 'highest' : String(defaultHeight)}
                  onChange={(event) => {
                    clearAppliedRule()
                    if (event.target.value === 'highest') setDefaultQualityStrategy('highest')
                    else {
                      setDefaultQualityStrategy('selected')
                      setDefaultHeight(Number(event.target.value))
                    }
                  }}
                  disabled={queueDownloads.isPending}
                >
                  <option value="highest">最高可用</option>
                  {QUALITY_LIMITS.filter((item) => item.value <= maxHeight).map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
                </select>
              </Field>
              <Field label="分类优先级"><select value={variantPriority.join(',')} onChange={(event) => { clearAppliedRule(); const priority = VARIANT_PRIORITIES.find((item) => item.join(',') === event.target.value); if (priority) setVariantPriority([...priority]) }} disabled={queueDownloads.isPending}>{VARIANT_PRIORITIES.map((priority) => <option value={priority.join(',')} key={priority.join(',')}>{variantPriorityLabel(priority)}</option>)}</select></Field>
              <Field label="已有作品"><select value={existingPolicy} onChange={(event) => { clearAppliedRule(); setExistingPolicy(event.target.value as WebDownloadExistingPolicy) }} disabled={queueDownloads.isPending}>{EXISTING_POLICIES.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></Field>
              <Button type="button" variant="primary" onClick={() => void requestSelectedDownloads()} disabled={!selectedIds.size || queueDownloads.isPending || checkingHistory || !serviceReady}>
                <Download aria-hidden="true" />
                {checkingHistory ? '正在检查下载记录' : queueDownloads.isPending ? '正在加入后台' : `下载选中 ${selectedIds.size} 项`}
              </Button>
            </div>
            {historyConfirm ? (
              <div className="resource-history-confirm" role="group" aria-label="重复下载确认">
                <span>{historyConfirm.summary}。</span>
                <Button type="button" size="small" variant="primary" onClick={() => queueSelectedDownloads(historyConfirm.duplicateIds)}>
                  跳过这些，下载其余 {selectedIds.size - historyConfirm.duplicateIds.length} 项
                </Button>
                <Button type="button" size="small" variant="secondary" onClick={() => queueSelectedDownloads()}>全部下载</Button>
                <Button type="button" size="small" variant="ghost" onClick={() => setHistoryConfirm(null)}>取消</Button>
              </div>
            ) : null}
            <span>点击后立即加入后台；分类、画质与媒体地址会按当前规则自动解析。</span>
            {queueDownloads.isError ? <InlineNotice tone="danger" role="status">提交失败：{(queueDownloads.error as Error).message}。可直接重试，系统不会重复创建任务。</InlineNotice> : null}
            {service.isSuccess && !serviceReady ? <InlineNotice tone="warning">Web 下载服务当前不可用，资源搜索与筛选仍可继续，下载操作暂不可用。</InlineNotice> : null}
            {service.isError ? <InlineNotice tone="warning">暂时无法确认 Web 下载服务状态，下载操作已禁用。</InlineNotice> : null}
          </div>
        </section>
      ) : sessionId && searchQuery.isLoading ? <SkeletonRows count={6} /> : !invalidSessionId && !(sessionId && searchQuery.isError) ? (
        <EmptyState title="搜索 Web 视频资源" description="结果会按番号聚合，并标明原片、中文字幕和无码影片分类" />
      ) : null}

    </div>
  )
}
