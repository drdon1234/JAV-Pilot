import { useMutation } from '@tanstack/react-query'
import { Clapperboard, FileImage, History, Languages, RotateCcw, Save, Search, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'

import { SettingsSaveError } from '../components/SettingsSaveError'
import { useToast } from '../components/ToastProvider'
import { Button, Field, InlineNotice, PageHeader, SkeletonRows, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { useSettingsDraft } from '../lib/settingsDraft'
import { METADATA_PROFILES, SEARCH_CAPABILITIES, SEARCH_PARSER_PROFILES } from '../lib/sources'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import type { AppSettings, SearchKind, WebDownloadExistingPolicy, WebDownloadVariant, WorkflowDefaults } from '../types'
import { AiTranslationSettings } from './AiTranslationSettings'

import '../styles/settings.css'
import '../styles/workflowDefaults.css'

const SEARCH_KIND_OPTIONS: Array<{ value: SearchKind; label: string }> = [
  { value: 'keyword', label: '全部内容' },
  { value: 'code', label: '番号' },
  { value: 'actor', label: '演员' },
  { value: 'tag', label: '标签' },
  { value: 'series', label: '系列' },
  { value: 'maker', label: '制作商' },
  { value: 'publisher', label: '发行商' },
  { value: 'director', label: '导演' },
]
const QUALITY_OPTIONS = [
  { value: 4320, label: '8K' },
  { value: 2160, label: '4K' },
  { value: 1440, label: '1440p' },
  { value: 1080, label: '1080p' },
  { value: 720, label: '720p' },
  { value: 480, label: '480p' },
]
const EXISTING_POLICIES: Array<{ value: WebDownloadExistingPolicy; label: string }> = [
  { value: 'higher_quality', label: '仅在画质更高时下载' },
  { value: 'skip', label: '已有作品不再下载' },
  { value: 'overwrite', label: '总是重新下载并覆盖' },
]
const VARIANT_PRIORITIES: WebDownloadVariant[][] = [
  ['original', 'chinese_subtitle', 'uncensored_leak'],
  ['original', 'uncensored_leak', 'chinese_subtitle'],
  ['chinese_subtitle', 'original', 'uncensored_leak'],
  ['chinese_subtitle', 'uncensored_leak', 'original'],
  ['uncensored_leak', 'original', 'chinese_subtitle'],
  ['uncensored_leak', 'chinese_subtitle', 'original'],
]

function Section({ id, icon, title, description, children }: { id?: string; icon: ReactNode; title: string; description: string; children: ReactNode }) {
  return (
    <section className="settings-section workflow-defaults-section" id={id}>
      <div className="settings-section-header">
        <div>
          {icon}
          <div>
            <h2>{title}</h2>
            <span>{description}</span>
          </div>
        </div>
      </div>
      {children}
    </section>
  )
}

/**
 * 工作流默认参数: every default a daily workflow starts from, in one place.
 * Forms still remember the last values used in each browser; these values are
 * the first-use state and what “恢复默认条件” returns to.
 */
export function WorkflowDefaultsPage() {
  const toast = useToast()
  const { settings, draft, setDraft, loadedRevision, editRevision, acceptSaved, reloadSaved } = useSettingsDraft()
  const save = useMutation({
    mutationFn: ({ value, expectedRevision }: { value: AppSettings; revision: number; expectedRevision: string }) => (
      api.saveSettings(value, expectedRevision)
    ),
    onSuccess: (snapshot, submission) => {
      acceptSaved(snapshot, submission.revision)
      toast.push('工作流默认参数已保存', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  if (settings.isLoading || !draft) {
    return <div className="page"><PageHeader title="工作流默认参数" description="搜索、下载、元数据与翻译的默认行为" /><SkeletonRows count={6} /></div>
  }
  const defaults = draft.workflow_defaults as WorkflowDefaults
  const searchSites = draft.sites.filter((site) => (
    site.enabled
    && SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability))
    && SEARCH_PARSER_PROFILES.has(site.parser_profile)
  ))
  const detailSites = draft.sites.filter((site) => (
    (site.capabilities.includes('metadata_search') || site.capabilities.includes('metadata_detail'))
    && METADATA_PROFILES.includes(site.parser_profile as typeof METADATA_PROFILES[number])
  ))

  function update(next: Partial<WorkflowDefaults>) {
    setDraft((current) => current ? { ...current, workflow_defaults: { ...(current.workflow_defaults as WorkflowDefaults), ...next } } : current)
  }
  function updateSearch(next: Partial<WorkflowDefaults['search']>) {
    update({ search: { ...defaults.search, ...next } })
  }
  function updateResource(next: Partial<WorkflowDefaults['resource_search']>) {
    update({ resource_search: { ...defaults.resource_search, ...next } })
  }
  function toggleSearchSource(id: string) {
    const current = defaults.search.site_mode === 'all' ? searchSites.map((site) => site.id) : defaults.search.sources
    const next = current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
    const all = searchSites.every((site) => next.includes(site.id))
    updateSearch({ site_mode: all ? 'all' : 'custom', sources: all ? [] : next })
  }
  const dirty = settings.data ? JSON.stringify(settings.data.settings) !== JSON.stringify(draft) : false
  const selectedSearchSources = defaults.search.site_mode === 'all' ? searchSites.map((site) => site.id) : defaults.search.sources

  return (
    <div className="page settings-page workflow-defaults-page">
      <PageHeader
        title="工作流默认参数"
        description="搜索、下载、元数据与翻译的默认行为集中在这里设置"
        actions={(
          <>
            <Button variant="ghost" onClick={() => void reloadSaved()} disabled={!dirty || save.isPending}>
              <RotateCcw aria-hidden="true" />
              放弃修改
            </Button>
            <Button
              variant="primary"
              onClick={() => save.mutate({ value: draft, revision: editRevision.current, expectedRevision: loadedRevision })}
              disabled={!dirty || save.isPending}
            >
              <Save aria-hidden="true" />
              {save.isPending ? '正在保存' : '保存'}
            </Button>
          </>
        )}
      />
      <InlineNotice tone="info">
        各页面会记住你在当前浏览器上次使用的条件；这里的值用于第一次使用，以及在搜索页点击“恢复默认条件”时。
      </InlineNotice>
      <SettingsSaveError error={save.error} onReload={async () => { await reloadSaved() }} />

      <div className="workflow-defaults-grid">
        <Section icon={<Search aria-hidden="true" />} title="资料与磁链搜索" description="作品搜索页的初始条件">
          <fieldset className="workflow-defaults-sources">
            <legend>默认搜索站点</legend>
            <label className="toggle-inline">
              <input
                type="checkbox"
                checked={defaults.search.site_mode === 'all'}
                onChange={(event) => updateSearch({ site_mode: event.target.checked ? 'all' : 'custom', sources: event.target.checked ? [] : searchSites.map((site) => site.id) })}
              />
              <span>全选（以后新启用的站点也会自动加入）</span>
            </label>
            <div>
              {searchSites.map((site) => (
                <label className="toggle-inline" key={site.id}>
                  <input type="checkbox" checked={selectedSearchSources.includes(site.id)} onChange={() => toggleSearchSource(site.id)} />
                  <span>{site.name}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="settings-form-grid">
            <Field label="搜索类别">
              <select value={defaults.search.search_kind} onChange={(event) => updateSearch({ search_kind: event.target.value as SearchKind })}>
                {SEARCH_KIND_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <Field label="结果上限" hint="单次搜索最多聚合的作品数，1–999">
              <input type="number" min={1} max={999} value={defaults.search.result_limit} onChange={(event) => updateSearch({ result_limit: Math.min(999, Math.max(1, Number(event.target.value) || 1)) })} />
            </Field>
            <Field label="每页显示">
              <select value={defaults.search.page_size} onChange={(event) => updateSearch({ page_size: Number(event.target.value) })}>
                {[10, 20, 50, 100].map((value) => <option value={value} key={value}>{value}</option>)}
              </select>
            </Field>
            <Field label="详情页默认站点" hint="打开作品详情时先显示哪个站点的截图与资料">
              <select value={draft.detail_default_site_id ?? ''} onChange={(event) => setDraft((current) => current ? { ...current, detail_default_site_id: event.target.value } : current)}>
                {detailSites.map((site) => <option value={site.id} key={site.id}>{site.name}</option>)}
              </select>
            </Field>
          </div>
          <div className="workflow-defaults-toggles">
            <Toggle label="解析磁链" checked={defaults.search.fetch_magnets} onChange={(event) => updateSearch({ fetch_magnets: event.target.checked })} />
            <span>开启后搜索时逐个打开作品详情获取磁链并查询种子站；关闭可让搜索更快，磁链在详情页再解析。</span>
            <Toggle label="精确匹配" checked={defaults.search.exact_match} onChange={(event) => updateSearch({ exact_match: event.target.checked })} />
            <span>只保留番号前缀与输入完全一致的作品，排除站点模糊匹配出的相近番号；结果上限按匹配后的数量计算。</span>
          </div>
        </Section>

        <Section icon={<Clapperboard aria-hidden="true" />} title="Web 视频资源" description="Web 搜索与批量下载的初始条件">
          <div className="settings-form-grid">
            <Field label="结果上限">
              <input type="number" min={1} max={999} value={defaults.resource_search.result_limit} onChange={(event) => updateResource({ result_limit: Math.min(999, Math.max(1, Number(event.target.value) || 1)) })} />
            </Field>
            <Field label="画质上限" hint="超过该画质的版本不会下载">
              <select
                value={defaults.resource_search.max_height}
                onChange={(event) => {
                  const height = Number(event.target.value)
                  const quality = defaults.resource_search.default_quality
                  updateResource({ max_height: height, default_quality: quality !== 'highest' && quality > height ? height : quality })
                }}
              >
                {QUALITY_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <Field label="默认画质" hint="“最高可用”会在画质上限内选最清晰的版本">
              <select value={String(defaults.resource_search.default_quality)} onChange={(event) => updateResource({ default_quality: event.target.value === 'highest' ? 'highest' : Number(event.target.value) })}>
                <option value="highest">最高可用</option>
                {QUALITY_OPTIONS.filter((option) => option.value <= defaults.resource_search.max_height).map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <Field label="分类优先级" hint="同一作品有多个版本时的选择顺序">
              <select value={defaults.resource_search.variant_priority.join(',')} onChange={(event) => { const next = VARIANT_PRIORITIES.find((item) => item.join(',') === event.target.value); if (next) updateResource({ variant_priority: [...next] }) }}>
                {VARIANT_PRIORITIES.map((priority) => <option value={priority.join(',')} key={priority.join(',')}>{priority.map(webDownloadVariantLabel).join(' > ')}</option>)}
              </select>
            </Field>
            <Field label="已有作品" hint="媒体库或下载记录中已有同番号作品时">
              <select value={defaults.resource_search.existing_policy} onChange={(event) => updateResource({ existing_policy: event.target.value as WebDownloadExistingPolicy })}>
                {EXISTING_POLICIES.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </Field>
          </div>
          <div className="workflow-defaults-toggles">
            <Toggle label="精确匹配" checked={defaults.resource_search.exact_match} onChange={(event) => updateResource({ exact_match: event.target.checked })} />
            <span>Web 搜索同样只保留番号前缀完全一致的结果。</span>
          </div>
        </Section>

        <Section icon={<FileImage aria-hidden="true" />} title="元数据" description="NFO、封面与背景图的自动补全">
          <div className="workflow-defaults-toggles">
            <Toggle label="新入库作品自动补全" checked={defaults.metadata_auto_complete} onChange={(event) => update({ metadata_auto_complete: event.target.checked })} />
            <span>媒体库发现新作品（包括你手动放入的文件夹）时，自动为缺少 NFO 或图片的作品创建补全任务。</span>
            <Toggle label="默认来源失败时自动换源" checked={defaults.metadata_auto_fallback} onChange={(event) => update({ metadata_auto_fallback: event.target.checked })} />
            <span>已启用的来源查不到资料或没有封面时，再尝试其他内置站点（JAV 用 FANZA、MGS、AVBase，FC2 用 FC2DB、JAVTEN）。</span>
          </div>
        </Section>

        <Section icon={<Languages aria-hidden="true" />} title="翻译" description="搜索结果标题与简介的翻译">
          <div className="workflow-defaults-toggles">
            <Toggle label="默认翻译标题和简介" checked={defaults.translation.enabled} onChange={(event) => update({ translation: { ...defaults.translation, enabled: event.target.checked } })} />
            <span>标题和简介会发送到免费的公共翻译服务后显示中文译文；关闭后仍可在结果页手动翻译。</span>
            <Toggle label="同时显示原文" checked={defaults.translation.show_original} onChange={(event) => update({ translation: { ...defaults.translation, show_original: event.target.checked } })} />
            <span>在译文下方以较小字号保留原文，方便核对。</span>
          </div>
        </Section>

        <Section id="ai-translation" icon={<Sparkles aria-hidden="true" />} title="AI 翻译" description="按需调用你自己的 AI 服务，结果显示在普通译文下方（独立保存）">
          <AiTranslationSettings />
        </Section>

        <Section icon={<History aria-hidden="true" />} title="搜索记录" description="自动保存的站点搜索与 Web 搜索">
          <div className="settings-form-grid">
            <Field label="保留条数" hint="超过后自动删除最早的记录，10–1000">
              <input type="number" min={10} max={1000} value={defaults.search_history_limit} onChange={(event) => update({ search_history_limit: Math.min(1000, Math.max(10, Number(event.target.value) || 10)) })} />
            </Field>
          </div>
        </Section>
      </div>
    </div>
  )
}
