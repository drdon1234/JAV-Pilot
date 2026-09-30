import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Braces, ChevronDown, LockKeyhole, Play, Plus, RefreshCw, Save, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { ParserRulesEditor, siteParserErrorCount } from '../components/ParserRulesEditor'
import { SettingsSaveError } from '../components/SettingsSaveError'
import { useSettingsDraft } from '../lib/settingsDraft'
import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, Field, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { errorCodeLabel } from '../lib/presentation'
import { METADATA_PROFILES, PROFILE_LABELS, SEARCH_CAPABILITIES, profileCapabilities } from '../lib/sources'
import '../styles/sites.css'
import type {
  AppSettings,
  FilterOption,
  SiteDiagnosticProbePayload,
  SiteDiagnosticSite,
  SiteDiagnosticStage,
  SiteDiagnosticStatus,
  SiteDiagnosticsPayload,
  SiteCapability,
  SiteFilter,
  SiteSettings,
} from '../types'
import { t } from '../lib/i18n'

const BUILTIN_WEB_SITE_IDS = ['jable', 'supjav', 'missav', 'kissjav', 'javnoni'] as const
const BUILTIN_SUBTITLE_SITE_IDS = ['xunlei', 'subtitlecat'] as const
const DEFAULT_METADATA_SEARCH_PRIORITY = ['javdb', 'javbus', 'fc2'] as const
const DEFAULT_METADATA_SCRAPER_PRIORITY = ['javbus', 'javdb', 'fc2'] as const
const METADATA_SEARCH_CAPABILITY: SiteCapability = 'metadata_search'
type SitePriorityField =
  | 'metadata_search_site_priority'
  | 'web_resource_search_provider_priority'
  | 'web_download_provider_priority'
  | 'metadata_scraper_site_priority'
const CAPABILITY_LABELS: Record<SiteCapability, string> = {
  metadata_search: t('元数据搜索'),
  metadata_detail: t('番号详情'),
  torrent_search: t('种子搜索'),
  resource_search: t('资源搜索'),
  web_download: t('Web 下载'),
  description: t('简介'),
  subtitle: t('字幕'),
}

const DIAGNOSTIC_SITES: Array<{ id: SiteDiagnosticSite; label: string; stages: SiteDiagnosticStage[] }> = [
  { id: 'javbus', label: 'JavBus', stages: ['configuration', 'dns', 'connection', 'search', 'detail', 'image'] },
  { id: 'javdb', label: 'JavDB', stages: ['configuration', 'dns', 'connection', 'search', 'detail', 'image'] },
  ...(['fc2', 'fanza', 'mgs', 'avbase', 'fc2db', 'javten'] as const).map((id) => ({
    id, label: PROFILE_LABELS[id],
    stages: ['configuration', 'dns', 'connection', 'detail', 'image'] as SiteDiagnosticStage[],
  })),
  { id: 'jable', label: 'JableTV', stages: ['configuration', 'dns', 'connection', 'search', 'detail', 'manifest'] },
  { id: 'supjav', label: 'SupJav', stages: ['configuration', 'dns', 'connection', 'search', 'detail', 'manifest'] },
  { id: 'missav', label: 'MissAV', stages: ['configuration', 'dns', 'connection', 'quality', 'manifest'] },
  { id: 'kissjav', label: 'KissJAV', stages: ['configuration', 'dns', 'connection', 'search'] },
  { id: 'javnoni', label: 'JAV-NONI', stages: ['configuration', 'dns', 'connection', 'search'] },
  { id: 'xunlei', label: t('迅雷字幕'), stages: ['configuration', 'dns', 'connection', 'search'] },
  { id: 'subtitlecat', label: 'SubtitleCat', stages: ['configuration', 'dns', 'connection', 'search'] },
]

const DIAGNOSTIC_STAGE_LABELS: Record<SiteDiagnosticStage, string> = {
  configuration: t('配置'),
  dns: 'DNS',
  connection: t('连接'),
  search: t('搜索'),
  detail: t('详情'),
  image: t('图片'),
  quality: t('画质'),
  manifest: t('清单'),
}

type DiagnosticRunRequest = {
  mode: 'single' | 'all'
  sites: SiteDiagnosticSite[]
}

type DiagnosticRunProgress = {
  mode: DiagnosticRunRequest['mode']
  running: SiteDiagnosticSite[]
  done: number
  total: number
}

// Sites are tested in parallel; a small pool keeps one run from flooding the
// upstream sites or the MissAV browser gate.
const DIAGNOSTIC_CONCURRENCY = 4
const FC2_DIAGNOSTIC_SITES = new Set<SiteDiagnosticSite>(['fc2', 'fc2db', 'javten'])

type DiagnosticRunOutcome = {
  site: SiteDiagnosticSite
  payload?: SiteDiagnosticProbePayload
  error?: Error
}

function mergeDiagnosticStatuses(
  current: SiteDiagnosticsPayload | undefined,
  incoming: SiteDiagnosticsPayload,
): SiteDiagnosticsPayload {
  const statuses = new Map(
    (current?.statuses ?? []).map((status) => [`${status.site}:${status.stage}`, status]),
  )
  incoming.statuses.forEach((status) => statuses.set(`${status.site}:${status.stage}`, status))
  return { ok: incoming.ok, statuses: [...statuses.values()] }
}

function createSite(index: number): SiteSettings {
  const id = `site-${Date.now().toString(36)}-${index}`
  return {
    id,
    name: t('新站点'),
    capabilities: [METADATA_SEARCH_CAPABILITY],
    enabled: false,
    base_url: 'https://example.com',
    parser_profile: 'javbus',
    search: { url_template: '{base_url}/search/{query}{page_path}' },
    filters: [],
    parser_rules_mode: 'inherit',
  }
}

function createFilter(index: number): SiteFilter {
  return {
    id: `filter-${index + 1}`,
    label: t('筛选条件'),
    type: 'select',
    default: '',
    options: [{ label: t('默认'), value: '' }],
  }
}

function isBuiltInWebSite(site: SiteSettings | undefined): boolean {
  return Boolean(site && BUILTIN_WEB_SITE_IDS.includes(site.id as typeof BUILTIN_WEB_SITE_IDS[number]) && site.parser_profile === site.id)
}

function isBuiltInSubtitleSite(site: SiteSettings | undefined): boolean {
  return Boolean(site && BUILTIN_SUBTITLE_SITE_IDS.includes(site.id as typeof BUILTIN_SUBTITLE_SITE_IDS[number]) && site.parser_profile === site.id)
}

function orderedSitePriority(
  value: readonly string[] | undefined,
  sites: readonly SiteSettings[],
  defaults: readonly string[],
): string[] {
  const available = new Set(sites.map((site) => site.id))
  const ordered: string[] = []
  for (const siteId of [...(value ?? defaults), ...defaults, ...available]) {
    if (available.has(siteId) && !ordered.includes(siteId)) ordered.push(siteId)
  }
  return ordered
}

function PriorityOrderField({
  label,
  sites,
  value,
  onChange,
}: {
  label: string
  sites: SiteSettings[]
  value: string[]
  onChange: (index: number, siteId: string) => void
}) {
  const names = new Map(sites.map((site) => [site.id, site.name]))
  return (
    <Field label={label} className="site-priority-field">
      <div className="site-priority-control">
        <StatusBadge tone="info">{t('首选')} {names.get(value[0]) ?? t('未配置')}</StatusBadge>
        <div className="site-priority-selects" role="group" aria-label={t('{label}优先级', { label })}>
          {value.map((siteId, index) => (
            <select
              aria-label={t('{label}第 {value} 优先站点', { label, value: index + 1 })}
              value={siteId}
              onChange={(event) => onChange(index, event.target.value)}
              key={index}
            >
              {sites.map((site) => (
                <option value={site.id} key={site.id}>
                  {index + 1}. {site.name}{site.enabled ? '' : t('（已停用）')}
                </option>
              ))}
            </select>
          ))}
        </div>
      </div>
    </Field>
  )
}

export function SitesPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const { settings, draft, setDraft, loadedRevision, editRevision, acceptSaved, reloadSaved } = useSettingsDraft((value) => {
    setSiteEditorKeys(value.sites.map(() => nextSiteEditorKey()))
  })
  const [advancedText, setAdvancedText] = useState('')
  const [advancedDirty, setAdvancedDirty] = useState(false)
  const [advancedError, setAdvancedError] = useState('')
  const [advancedApplying, setAdvancedApplying] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [diagnosticCodes, setDiagnosticCodes] = useState({ jav: '', fc2: '' })
  const diagnosticCodesLoaded = useRef(false)
  const [diagnosticProgress, setDiagnosticProgress] = useState<DiagnosticRunProgress | null>(null)
  const [diagnosticRequestErrors, setDiagnosticRequestErrors] = useState<Partial<Record<SiteDiagnosticSite, string>>>({})
  const [siteEditorKeys, setSiteEditorKeys] = useState<string[]>([])
  const editorKeySequence = useRef(0)
  const diagnostics = useQuery({
    queryKey: ['site-diagnostics'],
    queryFn: () => api.siteDiagnostics(),
    enabled: settings.isSuccess,
  })
  useEffect(() => {
    const saved = diagnostics.data?.codes
    if (!saved || diagnosticCodesLoaded.current) return
    diagnosticCodesLoaded.current = true
    setDiagnosticCodes({ jav: saved.jav ?? '', fc2: saved.fc2 ?? '' })
  }, [diagnostics.data?.codes])
  const probe = useMutation({
    mutationFn: async ({ mode, sites }: DiagnosticRunRequest) => {
      const outcomes: DiagnosticRunOutcome[] = []
      const codes = { ...diagnosticCodes }
      const queue = [...sites]
      const running = new Set<SiteDiagnosticSite>()
      let done = 0
      const report = () => setDiagnosticProgress({ mode, running: Array.from(running), done, total: sites.length })
      report()
      const runOne = async (site: SiteDiagnosticSite) => {
        try {
          const payload = await api.probeSiteDiagnostics(site, codes)
          setDiagnosticRequestErrors((current) => {
            if (!current[site]) return current
            const next = { ...current }
            delete next[site]
            return next
          })
          queryClient.setQueryData<SiteDiagnosticsPayload>(['site-diagnostics'], (current) =>
            mergeDiagnosticStatuses(current, payload),
          )
          outcomes.push({ site, payload })
        } catch (error) {
          const requestError = error as Error
          setDiagnosticRequestErrors((current) => ({
            ...current,
            [site]: requestError.message || t('诊断请求失败'),
          }))
          outcomes.push({ site, error: requestError })
        }
      }
      const worker = async () => {
        for (let site = queue.shift(); site; site = queue.shift()) {
          running.add(site)
          report()
          await runOne(site)
          running.delete(site)
          done += 1
          report()
        }
      }
      await Promise.all(Array.from({ length: Math.min(DIAGNOSTIC_CONCURRENCY, sites.length) }, worker))
      return outcomes
    },
    onSuccess: (outcomes, request) => {
      const failedSites = outcomes.filter(
        (outcome) => outcome.error || outcome.payload?.results.some((result) => !result.ok),
      )
      if (request.mode === 'all') {
        toast.push(
          failedSites.length
            ? t('全部站点诊断完成，{count} 个站点异常', { count: failedSites.length })
            : t('全部站点诊断通过'),
          failedSites.length ? 'error' : 'success',
        )
        return
      }
      const outcome = outcomes[0]
      if (!outcome) return
      if (outcome.error) {
        toast.push(outcome.error.message, 'error')
        return
      }
      const failedStages = outcome.payload?.results.filter((result) => !result.ok).length ?? 0
      toast.push(
        failedStages
          ? t('{value} 诊断完成，{failedStages} 个阶段异常', { value: siteLabel(outcome.site), failedStages })
          : t('{value} 诊断通过', { value: siteLabel(outcome.site) }),
        failedStages ? 'error' : 'success',
      )
    },
    onSettled: () => setDiagnosticProgress(null),
  })

  useEffect(() => {
    if (draft && !advancedDirty) setAdvancedText(JSON.stringify(draft.sites, null, 2))
  }, [advancedDirty, draft])

  const save = useMutation({
    mutationFn: ({ value, expectedRevision }: { value: AppSettings; revision: number; expectedRevision: string }) => api.saveSettings(value, expectedRevision),
    onSuccess: (snapshot, submission) => {
      const current = acceptSaved(snapshot, submission.revision)
      const value = snapshot.settings
      void queryClient.invalidateQueries({ queryKey: ['runtime'] })
      if (!current) {
        toast.push(t('站点设置已保存，当前仍有未保存修改'), 'success')
        return
      }
      setSiteEditorKeys((current) => value.sites.map((_site, index) => current[index] ?? nextSiteEditorKey()))
      setAdvancedDirty(false)
      setSaveError('')
      toast.push(t('站点设置已保存'), 'success')
    },
    onError: (error) => {
      const message = (error as Error).message
      setSaveError(message)
      toast.push(message, 'error')
    },
  })
  const enabledCount = useMemo(() => draft?.sites.filter((site) => site.enabled).length ?? 0, [draft])
  const parserErrorCount = useMemo(() => draft?.sites.reduce((count, site) => count + siteParserErrorCount(site), 0) ?? 0, [draft])
  const detailDefaultSites = useMemo(
    () => draft?.sites.filter(
      (site) => (site.capabilities.includes(METADATA_SEARCH_CAPABILITY) || site.capabilities.includes('metadata_detail'))
        && METADATA_PROFILES.includes(site.parser_profile as typeof METADATA_PROFILES[number]),
    ) ?? [],
    [draft],
  )
  const webResourceSites = useMemo(
    () => draft?.sites.filter(
      (site) => BUILTIN_WEB_SITE_IDS.includes(site.id as typeof BUILTIN_WEB_SITE_IDS[number])
        && site.capabilities.includes('resource_search'),
    ) ?? [],
    [draft],
  )
  const webDownloadSites = useMemo(
    () => draft?.sites.filter(
      (site) => BUILTIN_WEB_SITE_IDS.includes(site.id as typeof BUILTIN_WEB_SITE_IDS[number])
        && site.capabilities.includes('web_download'),
    ) ?? [],
    [draft],
  )
  const metadataSearchPriority = orderedSitePriority(
    draft?.metadata_search_site_priority,
    draft?.sites.filter((site) => SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability))) ?? [],
    DEFAULT_METADATA_SEARCH_PRIORITY,
  )
  const webResourcePriority = orderedSitePriority(
    draft?.web_resource_search_provider_priority,
    webResourceSites,
    BUILTIN_WEB_SITE_IDS,
  )
  const webDownloadPriority = orderedSitePriority(
    draft?.web_download_provider_priority,
    webDownloadSites,
    BUILTIN_WEB_SITE_IDS,
  )
  const metadataScraperPriority = orderedSitePriority(
    draft?.metadata_scraper_site_priority,
    detailDefaultSites,
    DEFAULT_METADATA_SCRAPER_PRIORITY,
  )

  if (settings.isError && !draft)
    return (
      <div className="page">
        <EmptyState
          role="alert"
          title={t('无法加载站点设置')}
          description={t('读取站点配置时发生错误：{message}', { message: (settings.error as Error).message })}
          action={
            <Button onClick={() => void settings.refetch()} disabled={settings.isFetching}>
              <RefreshCw className={settings.isFetching ? 'spin' : ''} aria-hidden="true" />
              {settings.isFetching ? t('重试中') : t('重新加载')}
            </Button>
          }
        />
      </div>
    )
  if (settings.isLoading || !draft)
    return (
      <div className="page">
        <SkeletonRows count={5} />
      </div>
    )

  function nextSiteEditorKey() {
    const key = `site-editor-${editorKeySequence.current}`
    editorKeySequence.current += 1
    return key
  }

  function updateSite(index: number, site: SiteSettings) {
    setSaveError('')
    setDraft((current) => {
      if (!current) return current
      const copy = structuredClone(current)
      copy.sites[index] = site
      return copy
    })
  }

  function removeSite(index: number) {
    const site = draft?.sites[index]
    if (isBuiltInWebSite(site) || isBuiltInSubtitleSite(site)) return
    setSaveError('')
    setSiteEditorKeys((current) => current.filter((_key, itemIndex) => itemIndex !== index))
    setDraft((current) => {
      if (!current) return current
      const copy = structuredClone(current)
      copy.sites.splice(index, 1)
      return copy
    })
  }

  function addSite() {
    if (!draft) return
    setSaveError('')
    setSiteEditorKeys((current) => [...current, nextSiteEditorKey()])
    setDraft({
      ...draft,
      sites: [...draft.sites, createSite(draft.sites.length)],
    })
  }

  function updateSitePriority(
    field: SitePriorityField,
    index: number,
    siteId: string,
    sites: SiteSettings[],
    defaults: readonly string[],
  ) {
    setSaveError('')
    setDraft((current) => {
      if (!current) return current
      const priority = orderedSitePriority(current[field], sites, defaults)
      const previousIndex = priority.indexOf(siteId)
      if (previousIndex >= 0) [priority[index], priority[previousIndex]] = [priority[previousIndex], priority[index]]
      else priority[index] = siteId
      return { ...current, [field]: priority }
    })
  }

  async function applyAdvancedJson() {
    try {
      if (!draft) throw new Error(t('设置尚未加载完成'))
      const parsed = JSON.parse(advancedText)
      if (!Array.isArray(parsed)) throw new Error(t('高级 JSON 必须是站点数组'))
      const validationRevision = editRevision.current
      setAdvancedApplying(true)
      const normalized = await api.validateSettings({ ...draft, sites: parsed })
      if (editRevision.current !== validationRevision) {
        setAdvancedError(t('高级 JSON 在校验期间已变化，请重新应用'))
        return
      }
      setDraft(normalized)
      setSiteEditorKeys((current) => normalized.sites.map((_site, index) => current[index] ?? nextSiteEditorKey()))
      setAdvancedDirty(false)
      setAdvancedError('')
      setSaveError('')
      toast.push(t('高级 JSON 已应用到草稿'), 'success')
    } catch (error) {
      setAdvancedError((error as Error).message)
    } finally {
      setAdvancedApplying(false)
    }
  }

  function submitDraft() {
    if (!draft) return
    if (advancedDirty) {
      const message = t('高级 JSON 有未应用修改，请先应用到草稿后再保存')
      setSaveError(message)
      toast.push(message, 'error')
      return
    }
    if (parserErrorCount) {
      const message = t('有 {parserErrorCount} 项解析规则需要修正后才能保存', { parserErrorCount })
      setSaveError(message)
      toast.push(message, 'error')
      return
    }
    save.mutate({ value: structuredClone(draft), revision: editRevision.current, expectedRevision: loadedRevision })
  }

  return (
    <div className="page sites-page">
      <PageHeader
        title={t('站点与解析')}
        description={t('站点域名、解析规则与筛选字段')}
        actions={
          <>
            <StatusBadge tone="info">
              {t('{enabledCount} / {count} 已启用', { enabledCount, count: draft.sites.length })}
            </StatusBadge>
            <Button variant="primary" onClick={submitDraft} disabled={save.isPending}>
              <Save aria-hidden="true" />
              {save.isPending ? t('保存中') : t('保存站点')}
            </Button>
          </>
        }
      />

      <section className="site-priority-panel" aria-labelledby="site-priority-title">
        <header>
          <h2 id="site-priority-title">{t('首选站点与优先级')}</h2>
        </header>
        <div className="site-priority-grid">
          <p className="site-priority-note">
            {t('详情页默认站点、搜索与下载的默认条件已统一到')}
            <Link to="/workflow-defaults">{t('默认参数')}</Link>
            {t('页面。下面的顺序决定多个站点同时可用时先使用哪一个。')}
          </p>
          <PriorityOrderField
            label={t('资料与种子搜索')}
            sites={draft.sites.filter((site) => SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability)))}
            value={metadataSearchPriority}
            onChange={(index, siteId) => updateSitePriority(
              'metadata_search_site_priority',
              index,
              siteId,
              draft.sites.filter((site) => SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability))),
              DEFAULT_METADATA_SEARCH_PRIORITY,
            )}
          />
          <PriorityOrderField
            label={t('Web 资源搜索')}
            sites={webResourceSites}
            value={webResourcePriority}
            onChange={(index, siteId) => updateSitePriority(
              'web_resource_search_provider_priority',
              index,
              siteId,
              webResourceSites,
              BUILTIN_WEB_SITE_IDS,
            )}
          />
          <PriorityOrderField
            label={t('Web 下载')}
            sites={webDownloadSites}
            value={webDownloadPriority}
            onChange={(index, siteId) => updateSitePriority(
              'web_download_provider_priority',
              index,
              siteId,
              webDownloadSites,
              BUILTIN_WEB_SITE_IDS,
            )}
          />
          <PriorityOrderField
            label={t('元数据与海报')}
            sites={detailDefaultSites}
            value={metadataScraperPriority}
            onChange={(index, siteId) => updateSitePriority(
              'metadata_scraper_site_priority',
              index,
              siteId,
              detailDefaultSites,
              DEFAULT_METADATA_SCRAPER_PRIORITY,
            )}
          />
        </div>
      </section>

      <SiteDiagnosticsPanel
        codes={diagnosticCodes}
        onCodesChange={setDiagnosticCodes}
        statuses={diagnostics.data?.statuses ?? []}
        loading={diagnostics.isLoading}
        error={diagnostics.isError ? (diagnostics.error as Error).message : ''}
        progress={diagnosticProgress}
        busy={probe.isPending}
        historyPending={diagnostics.isFetching}
        requestErrors={diagnosticRequestErrors}
        onProbe={(site) => probe.mutate({ mode: 'single', sites: [site] })}
        onProbeAll={() => probe.mutate({ mode: 'all', sites: DIAGNOSTIC_SITES.filter((site) => settings.data?.settings.sites.some((saved) => saved.id === site.id && saved.enabled)).map((site) => site.id) })}
        onRetry={() => void diagnostics.refetch()}
      />

      <section className="editor-workspace" aria-label={t('站点配置编辑器')}>
        <div className="sites-toolbar">
          <div className="sites-toolbar-context">
            <span>{t('按来源能力配置；新增来源默认关闭，启用前请核验可用性。')}</span>
          </div>
          <Button
            onClick={addSite}
          >
            <Plus aria-hidden="true" />
            {t('添加站点')}
          </Button>
        </div>

        <SettingsSaveError error={save.error ?? (saveError ? new Error(saveError) : null)} onReload={async () => {
          const value = await reloadSaved()
          setSiteEditorKeys(value.sites.map(() => nextSiteEditorKey()))
          setAdvancedDirty(false)
          setAdvancedError('')
          setSaveError('')
          save.reset()
        }} />

        <section className="site-list" aria-label={t('站点配置')}>
          {!draft.sites.length ? <EmptyState title={t('暂无站点')} /> : null}
          {draft.sites.map((site, index) => (
            <SiteEditor site={site} onChange={(next) => updateSite(index, next)} onRemove={() => removeSite(index)} key={siteEditorKeys[index] ?? `site-editor-fallback-${index}`} />
          ))}
        </section>
      </section>

      <details className="advanced-editor">
        <summary>
          <Braces aria-hidden="true" />
          {t('高级 JSON')}
          {advancedDirty ? <StatusBadge tone="warning">{t('未应用')}</StatusBadge> : null}
          <ChevronDown className="disclosure-chevron" aria-hidden="true" />
        </summary>
        <Field label={t('站点设置 JSON')} error={advancedError}>
          <textarea
            value={advancedText}
            onChange={(event) => {
              editRevision.current += 1
              setAdvancedText(event.target.value)
              setAdvancedDirty(true)
              setAdvancedError('')
              setSaveError('')
            }}
            rows={20}
            spellCheck={false}
          />
        </Field>
        <Button onClick={() => void applyAdvancedJson()} disabled={advancedApplying}>
          {advancedApplying ? t('校验中') : t('应用到草稿')}
        </Button>
      </details>
    </div>
  )
}

function siteLabel(site: SiteDiagnosticSite) {
  return DIAGNOSTIC_SITES.find((item) => item.id === site)?.label ?? site
}

function SiteDiagnosticsPanel({
  codes,
  onCodesChange,
  statuses,
  loading,
  error,
  progress,
  busy,
  historyPending,
  requestErrors,
  onProbe,
  onProbeAll,
  onRetry,
}: {
  codes: { jav: string; fc2: string }
  onCodesChange: (value: { jav: string; fc2: string }) => void
  statuses: SiteDiagnosticStatus[]
  loading: boolean
  error: string
  progress: DiagnosticRunProgress | null
  busy: boolean
  historyPending: boolean
  requestErrors: Partial<Record<SiteDiagnosticSite, string>>
  onProbe: (site: SiteDiagnosticSite) => void
  onProbeAll: () => void
  onRetry: () => void
}) {
  const byStage = useMemo(
    () => new Map(statuses.map((status) => [`${status.site}:${status.stage}`, status])),
    [statuses],
  )
  return (
    <section className="site-diagnostics" aria-labelledby="site-diagnostics-title">
      <header className="site-diagnostics-header">
        <div>
          <Activity aria-hidden="true" />
          <div>
            <h2 id="site-diagnostics-title">{t('站点诊断')}</h2>
            <p>{t('分阶段检查已保存的站点配置，不创建下载任务。全部站点会同时测试。')}</p>
          </div>
        </div>
        <div className="site-diagnostic-actions">
          <Field label={t('JAV 验收番号')} className="site-diagnostic-code">
            <input
              value={codes.jav}
              onChange={(event) => onCodesChange({ ...codes, jav: event.target.value })}
              placeholder={t('请输入 JAV 番号')}
              maxLength={40}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </Field>
          <Field label={t('FC2 验收番号')} className="site-diagnostic-code">
            <input
              value={codes.fc2}
              onChange={(event) => onCodesChange({ ...codes, fc2: event.target.value })}
              placeholder={t('请输入 FC2 番号')}
              maxLength={40}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </Field>
          <Button onClick={onProbeAll} disabled={busy || historyPending}>
            <RefreshCw className={progress?.mode === 'all' ? 'spin' : ''} aria-hidden="true" />
            {progress?.mode === 'all'
              ? t('测试全部 {done} / {total}', { done: progress.done, total: progress.total })
              : t('测试全部站点')}
          </Button>
        </div>
      </header>
      <p className="site-diagnostic-hint">
        {t('验收番号用来确认站点能查到一部真实作品：JAV 番号用于 JavBus、JavDB、FANZA 和 Web 视频站点，FC2 番号用于 FC2、FC2DB 与 JAVTEN。 留空的一项只检查配置、域名和连接。输入的番号会在测试时自动保存，后台定时诊断也会使用。')}
      </p>
      {error ? (
        <InlineNotice tone="danger" role="alert">
          <span>{t('无法读取诊断历史：{error}', { error })}</span>
          <Button size="small" onClick={onRetry}>{t('重试')}</Button>
        </InlineNotice>
      ) : null}
      {loading ? <SkeletonRows count={3} /> : (
        <div className="site-diagnostic-list" role="list" aria-label={t('站点诊断状态')}>
          {DIAGNOSTIC_SITES.map((site) => (
            <div className="site-diagnostic-row" role="listitem" key={site.id}>
              <span className="site-diagnostic-name">{site.label}</span>
              <div className="site-diagnostic-stages" role="group" aria-label={t('{label} 分阶段状态', { label: site.label })}>
                {requestErrors[site.id] ? (
                  <span className="site-diagnostic-request-error" role="alert">
                    {t('本次请求失败：{value}', { value: requestErrors[site.id] })}
                  </span>
                ) : null}
                {site.stages.map((stage) => (
                  <DiagnosticStageBadge
                    label={DIAGNOSTIC_STAGE_LABELS[stage]}
                    status={byStage.get(`${site.id}:${stage}`)}
                    key={stage}
                  />
                ))}
              </div>
              <Button
                size="small"
                onClick={() => onProbe(site.id)}
                disabled={busy || historyPending}
                title={!(FC2_DIAGNOSTIC_SITES.has(site.id) ? codes.fc2 : codes.jav).trim() ? t('未填写对应验收番号，仅检查连接') : undefined}
              >
                {progress?.running.includes(site.id) ? (
                  <RefreshCw className="spin" aria-hidden="true" />
                ) : <Play aria-hidden="true" />}
                {progress?.running.includes(site.id) ? t('测试中') : t('测试')}
              </Button>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function DiagnosticStageBadge({ label, status }: { label: string; status?: SiteDiagnosticStatus }) {
  const tone = !status ? 'neutral' : status.last_error_code ? 'danger' : 'success'
  const errorLabel = status?.last_error_code ? errorCodeLabel(status.last_error_code, t('站点检查失败')) : ''
  const detail = !status
    ? t('尚未测试')
    : status.last_error_code
      ? t('{errorLabel}，连续失败 {consecutive_failures} 次', { errorLabel, consecutive_failures: status.consecutive_failures })
      : t('通过，{last_latency_ms} ms', { last_latency_ms: status.last_latency_ms })
  return (
    <span className="site-diagnostic-stage" title={t('{label}：{detail}', { label, detail })}>
      <StatusBadge tone={tone}>{label}</StatusBadge>
      <span>{!status ? t('未测') : errorLabel || `${status.last_latency_ms} ms`}</span>
    </span>
  )
}

function SiteEditor({ site, onChange, onRemove }: { site: SiteSettings; onChange: (site: SiteSettings) => void; onRemove: () => void }) {
  const subtitleSource = isBuiltInSubtitleSite(site)
  const special = isBuiltInWebSite(site) || subtitleSource
  const supportsMetadataSearch = site.capabilities.includes(METADATA_SEARCH_CAPABILITY)
  const ruleBased = ['javbus', 'javdb'].includes(site.parser_profile)
  const torznab = site.torznab ?? { endpoint: '', pinned_addresses: [], categories: [] }
  const filters = site.filters ?? []
  const search = site.search ?? { url_template: '' }
  const set = <K extends keyof SiteSettings>(key: K, value: SiteSettings[K]) => {
    if (special && (key === 'id' || key === 'capabilities' || key === 'parser_profile')) return
    onChange({ ...site, [key]: value })
  }
  const updateFilter = (index: number, filter: SiteFilter) => {
    const nextFilters = [...filters]
    nextFilters[index] = filter
    set('filters', nextFilters)
  }
  const removeFilter = (index: number) =>
    set(
      'filters',
      filters.filter((_item, itemIndex) => itemIndex !== index),
    )

  return (
    <article className={`site-item ${site.enabled ? '' : 'disabled'}`}>
      <header className="site-item-header">
        <div>
          <Toggle label={t('启用')} checked={site.enabled} onChange={(event) => set('enabled', event.target.checked)} />
          <strong>{site.name || site.id}</strong>
          {special ? (
            <>
              <StatusBadge tone="info">{t('内置站点')}</StatusBadge>
              <StatusBadge>{t('{count} 项能力', { count: site.capabilities.length })}</StatusBadge>
            </>
          ) : <StatusBadge>{site.parser_profile}</StatusBadge>}
        </div>
        <IconButton
          label={special
            ? subtitleSource
              ? t('{name} 是内置字幕来源，不能删除', { name: site.name })
              : t('{name} 是内置 Web 下载站点，不能删除', { name: site.name })
            : t('删除站点')}
          className="danger-icon"
          onClick={onRemove}
          disabled={special}
        >
          {special ? <LockKeyhole aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
        </IconButton>
      </header>

      <div className="site-form-grid">
        <Field label={t('站点 ID')}>
          <input value={site.id} onChange={(event) => set('id', event.target.value.toLowerCase())} maxLength={64} disabled={special} />
        </Field>
        <Field label={t('显示名称')}>
          <input value={site.name} onChange={(event) => set('name', event.target.value)} maxLength={80} />
        </Field>
        <Field label={t('站点能力')}>
          <input
            value={site.capabilities.map((capability) => CAPABILITY_LABELS[capability]).join(' / ')}
            readOnly
            disabled
          />
        </Field>
        <Field label={t('解析器')}>
          <select
            value={site.parser_profile}
            onChange={(event) => {
              const parserProfile = event.target.value
              if (parserProfile === site.parser_profile) return
              if (
                (site.parser_rules_mode ?? 'inherit') === 'custom'
                && !window.confirm(t('切换解析器会清除当前自定义解析规则，且无法撤销。是否继续？'))
              ) {
                event.currentTarget.value = site.parser_profile
                return
              }
              onChange({
                ...site,
                parser_profile: parserProfile,
                capabilities: profileCapabilities(parserProfile),
                torznab: parserProfile === 'torznab' ? torznab : undefined,
                parser_rules_mode: 'inherit',
                parser_rules: undefined,
              })
            }}
            disabled={special}
          >
            {special ? <option value={site.parser_profile}>{site.name}</option> : (
              <>
                {Object.entries(PROFILE_LABELS).map(([profile, name]) => <option value={profile} key={profile}>{name}</option>)}
              </>
            )}
          </select>
        </Field>
        <Field label={t('站点域名')} className="field-span-2">
          <input type="url" value={site.base_url} onChange={(event) => set('base_url', event.target.value)} maxLength={260} />
        </Field>
        {supportsMetadataSearch && ruleBased ? (
          <Field label={t('搜索 URL 模板')} className="field-span-full">
            <textarea value={search.url_template} onChange={(event) => set('search', { url_template: event.target.value })} rows={3} spellCheck={false} maxLength={1024} />
          </Field>
        ) : null}
        {site.parser_profile === 'torznab' ? <>
          <Field label={t('Torznab API 地址')} className="field-span-full">
            <input type="url" value={torznab.endpoint} maxLength={1024}
              onChange={(event) => set('torznab', { ...torznab, endpoint: event.target.value })}
              placeholder={torznab.bundled_jackett ? t('留空使用内置 Jackett') : t('http://索引服务:9117/api/v2.0/indexers/站点/results/torznab/api')} />
          </Field>
          <Field label={torznab.api_key_configured ? t('API 密钥（已保存，留空保留）') : t('API 密钥')}>
            <input type="password" autoComplete="new-password" value={torznab.api_key ?? ''} maxLength={512}
              onChange={(event) => set('torznab', { ...torznab, api_key: event.target.value || undefined })} />
          </Field>
          <Field label={t('固定 IP（可选）')}>
            <input value={torznab.pinned_addresses.join(', ')}
              onChange={(event) => set('torznab', { ...torznab, pinned_addresses: event.target.value.split(/[,\s]+/).filter(Boolean) })}
              placeholder="192.168.1.2" />
          </Field>
          <Field label={t('分类编号（可选，逗号分隔）')}>
            <input value={torznab.categories.join(', ')} inputMode="numeric"
              onChange={(event) => set('torznab', { ...torznab, categories: event.target.value.split(/[,\s]+/).filter(Boolean).map(Number) })} />
          </Field>
        </> : null}
      </div>
      {site.parser_profile === 'torznab' && torznab.bundled_jackett ? (
        <InlineNotice>{t('地址和 API 密钥都留空时使用内置 Jackett：自动读取密钥、添加索引，就绪前搜索会跳过此来源。')}</InlineNotice>
      ) : null}
      {site.id === 'subtitlecat' && subtitleSource ? (
        <InlineNotice>{t('SubtitleCat 的字幕由机器翻译生成，只在找不到人工字幕时使用。')}</InlineNotice>
      ) : null}
      {site.id === 'xunlei' && subtitleSource ? (
        <InlineNotice>{t('字幕文件从迅雷的下载服务器获取。')}</InlineNotice>
      ) : null}
      {site.id === 'kissjav' || site.id === 'javnoni' ? (
        <InlineNotice>{t('此来源提供资源发现。完整片源与版本尚未验收，暂不用于自动下载。')}</InlineNotice>
      ) : null}
      {site.parser_profile === 'fc2db' || site.parser_profile === 'javten' ? (
        <InlineNotice>{t('仅按带 FC2 前缀的番号查询详情，不参与自由关键词发现。')}</InlineNotice>
      ) : null}

      {supportsMetadataSearch && ruleBased ? <section className="filter-editor">
        <div className="section-toolbar compact-toolbar">
          <div>
            <h3>{t('筛选字段')}</h3>
            <span>{t('{count} 项', { count: filters.length })}</span>
          </div>
          <Button size="small" onClick={() => set('filters', [...filters, createFilter(filters.length)])} disabled={filters.length >= 32}>
            <Plus aria-hidden="true" />
            {t('添加字段')}
          </Button>
        </div>
        {!filters.length ? <div className="compact-empty">{t('无筛选字段')}</div> : null}
        {filters.map((filter, index) => (
          <FilterEditor filter={filter} onChange={(next) => updateFilter(index, next)} onRemove={() => removeFilter(index)} key={index} />
        ))}
      </section> : null}

      {supportsMetadataSearch && ruleBased
        ? <ParserRulesEditor site={site} onChange={onChange} />
        : null}
    </article>
  )
}

function FilterEditor({ filter, onChange, onRemove }: { filter: SiteFilter; onChange: (filter: SiteFilter) => void; onRemove: () => void }) {
  const set = <K extends keyof SiteFilter>(key: K, value: SiteFilter[K]) => onChange({ ...filter, [key]: value })
  const updateOption = (index: number, option: FilterOption) => {
    const options = [...filter.options]
    options[index] = option
    set('options', options)
  }

  return (
    <div className="filter-item">
      <div className="filter-fields">
        <Field label={t('字段 ID')}>
          <input value={filter.id} onChange={(event) => set('id', event.target.value.toLowerCase())} maxLength={64} />
        </Field>
        <Field label={t('标签')}>
          <input value={filter.label} onChange={(event) => set('label', event.target.value)} maxLength={80} />
        </Field>
        <Field label={t('类型')}>
          <select value={filter.type} onChange={(event) => set('type', event.target.value as SiteFilter['type'])}>
            <option value="select">{t('选择')}</option>
            <option value="text">{t('文本')}</option>
          </select>
        </Field>
        <Field label={t('默认值')}>
          <input value={filter.default} onChange={(event) => set('default', event.target.value)} maxLength={120} />
        </Field>
        <IconButton label={t('删除筛选字段')} className="danger-icon filter-delete" onClick={onRemove}>
          <Trash2 aria-hidden="true" />
        </IconButton>
      </div>
      {filter.type === 'select' ? (
        <div className="option-list">
          {filter.options.map((option, index) => (
            <div className="option-row" key={index}>
              <input aria-label={t('选项标签')} value={option.label} onChange={(event) => updateOption(index, { ...option, label: event.target.value })} placeholder={t('标签')} />
              <input aria-label={t('选项值')} value={option.value} onChange={(event) => updateOption(index, { ...option, value: event.target.value })} placeholder={t('值')} />
              <IconButton
                label={t('删除选项')}
                size="small"
                onClick={() =>
                  set(
                    'options',
                    filter.options.filter((_item, itemIndex) => itemIndex !== index),
                  )
                }
              >
                <Trash2 aria-hidden="true" />
              </IconButton>
            </div>
          ))}
          <Button size="small" variant="ghost" onClick={() => set('options', [...filter.options, { label: t('选项'), value: '' }])} disabled={filter.options.length >= 80}>
            <Plus aria-hidden="true" />
            {t('添加选项')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}
