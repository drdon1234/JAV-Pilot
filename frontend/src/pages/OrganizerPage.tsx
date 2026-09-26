import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Braces, ChevronRight, Globe2, Plus, RefreshCw, Save, SlidersHorizontal, TestTube2, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { SettingsSaveError } from '../components/SettingsSaveError'
import { useSettingsDraft } from '../lib/settingsDraft'
import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, Field, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api } from '../lib/api'
import '../styles/organizer.css'
import { splitTerms } from '../lib/format'
import { summarizeRule } from '../lib/organizer'
import type { AppSettings, OrganizerRule } from '../types'

function newRule(index: number): OrganizerRule {
  return {
    id: `rule-${Date.now().toString(36)}-${index}`,
    name: '新整理规则',
    enabled: true,
    priority: Math.min(99_999, (index + 1) * 100),
    match: {
      sources: [],
      title_contains: [],
      magnet_name_contains: [],
      code_regex: '',
    },
    actions: {
      media_type: 'movie',
      category: '',
      save_path: '',
      tags: 'jav-pilot',
    },
  }
}

export function OrganizerPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const { settings, draft, setDraft, loadedRevision, editRevision, acceptSaved, reloadSaved } = useSettingsDraft()
  const [advancedText, setAdvancedText] = useState('')
  const [advancedDirty, setAdvancedDirty] = useState(false)
  const [advancedError, setAdvancedError] = useState('')
  const [advancedApplying, setAdvancedApplying] = useState(false)
  const previewRevision = useRef(0)
  const [sample, setSample] = useState({
    source: 'javbus',
    code: '',
    title: '',
    name: '',
  })
  const [previewResult, setPreviewResult] = useState<{
    matched: boolean
    name?: string
    destination?: {
      category: string
      save_path: string
    }
  } | null>(null)

  useEffect(() => {
    const sites = settings.data?.settings.sites ?? []
    if (sites.some((site) => site.id === sample.source && site.enabled)) return
    const source = sites.find((site) => site.enabled && site.capabilities.includes('metadata_search'))?.id
    if (source) setSample((current) => ({ ...current, source }))
  }, [sample.source, settings.data?.settings.sites])
  useEffect(() => {
    if (draft && !advancedDirty) setAdvancedText(JSON.stringify(draft.organizer.rules, null, 2))
  }, [advancedDirty, draft])
  useEffect(() => {
    previewRevision.current += 1
    setPreviewResult(null)
  }, [draft, sample])

  const save = useMutation({
    mutationFn: ({ value, expectedRevision }: { value: AppSettings; expectedRevision: string; revision: number }) => api.saveSettings(value, expectedRevision),
    onSuccess: (snapshot, submission) => {
      const current = acceptSaved(snapshot, submission.revision)
      if (current) setAdvancedDirty(false)
      void queryClient.invalidateQueries({ queryKey: ['runtime'] })
      toast.push(current ? '整理规则已保存' : '整理规则已保存，当前仍有未保存修改', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const preview = useMutation({
    mutationFn: ({ settings, sample: previewSample }: {
      revision: number
      settings: AppSettings
      sample: typeof sample
    }) =>
      api.organizerPreview({
        settings,
        sample: {
          name: previewSample.name,
          result: {
            source: previewSample.source,
            code: previewSample.code,
            title: previewSample.title,
          },
          magnet_info: { display_name: previewSample.name },
        },
      }),
    onMutate: () => setPreviewResult(null),
    onSuccess: (value, variables) => {
      if (variables.revision !== previewRevision.current) return
      setPreviewResult({
        matched: value.matched,
        name: value.rule?.name,
        destination: value.destination ?? undefined,
      })
    },
    onError: (error, variables) => {
      if (variables.revision === previewRevision.current) toast.push((error as Error).message, 'error')
    },
  })

  const enabledCount = useMemo(() => draft?.organizer.rules.filter((rule) => rule.enabled).length ?? 0, [draft])

  if (settings.isError)
    return (
      <div className="page">
        <EmptyState
          role="alert"
          title="无法加载整理规则"
          description={`读取整理配置时发生错误：${(settings.error as Error).message}`}
          action={
            <Button onClick={() => void settings.refetch()} disabled={settings.isFetching}>
              <RefreshCw className={settings.isFetching ? 'spin' : ''} aria-hidden="true" />
              {settings.isFetching ? '重试中' : '重新加载'}
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

  const rules = draft.organizer.rules
  const savedRuleIds = new Set((settings.data?.settings.organizer.rules ?? []).map((rule) => rule.id))
  const updateRule = (index: number, next: OrganizerRule) => {
    setDraft((current) => {
      if (!current) return current
      const copy = structuredClone(current)
      copy.organizer.rules[index] = next
      return copy
    })
  }
  const moveRule = (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= rules.length) return
    setDraft((current) => {
      if (!current) return current
      const copy = structuredClone(current)
      const [item] = copy.organizer.rules.splice(index, 1)
      copy.organizer.rules.splice(target, 0, item)
      return copy
    })
  }
  const removeRule = (index: number) => {
    setDraft((current) => {
      if (!current) return current
      const copy = structuredClone(current)
      copy.organizer.rules.splice(index, 1)
      return copy
    })
  }

  async function applyAdvancedJson() {
    try {
      if (!draft) throw new Error('设置尚未加载完成')
      const parsed = JSON.parse(advancedText)
      if (!Array.isArray(parsed)) throw new Error('高级 JSON 必须是规则数组')
      const validationRevision = editRevision.current
      setAdvancedApplying(true)
      const normalized = await api.validateSettings({
        ...draft,
        organizer: { ...draft.organizer, rules: parsed },
      })
      if (editRevision.current !== validationRevision) {
        setAdvancedError('校验期间又有新的修改，请重新应用高级 JSON')
        return
      }
      setDraft(normalized)
      setAdvancedDirty(false)
      setAdvancedError('')
      toast.push('高级 JSON 已应用到草稿', 'success')
    } catch (error) {
      setAdvancedError((error as Error).message)
    } finally {
      setAdvancedApplying(false)
    }
  }

  return (
    <div className="page organizer-page">
      <PageHeader
        title="分类整理"
        description="下载完成后自动归档到媒体库"
        actions={
          <>
            <StatusBadge tone={draft.organizer.enabled ? 'success' : 'warning'}>{enabledCount} 条规则启用</StatusBadge>
            <Button variant="primary" onClick={() => save.mutate({ value: structuredClone(draft), expectedRevision: loadedRevision, revision: editRevision.current })} disabled={save.isPending}>
              <Save aria-hidden="true" />
              {save.isPending ? '保存中' : '保存规则'}
            </Button>
          </>
        }
      />

      <SettingsSaveError error={save.error} onReload={async () => {
        await reloadSaved()
        setAdvancedDirty(false)
        setAdvancedError('')
        save.reset()
      }} />
      <InlineNotice tone="info">
        <strong>整理做什么</strong>
        <span>
          通过搜索页加入 qBittorrent 的作品会先下载到 JAV 暂存目录，完成后自动移入媒体库并补全元数据。
          大多数情况下只需打开“启用自动整理”；只有想把不同作品放进不同子文件夹或加上不同标签时，才需要添加规则。
          规则从上到下依次匹配，第一条命中的生效，都不命中时使用暂存根目录。
        </span>
      </InlineNotice>
      <section className="editor-workspace" aria-label="整理规则编辑器">
        <div className="organizer-toolbar">
          <Toggle
            label="启用自动整理"
            checked={draft.organizer.enabled}
            onChange={(event) =>
              setDraft({
                ...draft,
                organizer: {
                  ...draft.organizer,
                  enabled: event.target.checked,
                },
              })
            }
          />
          <Button
            type="button"
            onClick={() =>
              setDraft({
                ...draft,
                organizer: {
                  ...draft.organizer,
                  rules: [...rules, newRule(rules.length)],
                },
              })
            }
            disabled={rules.length >= 80}
          >
            <Plus aria-hidden="true" />
            添加规则
          </Button>
        </div>

        {draft._config_error ? <InlineNotice tone="danger">{draft._config_error}</InlineNotice> : null}

        <section className="rule-list" aria-label="整理规则">
          {!rules.length ? (
            <EmptyState
              title="暂无整理规则"
              action={
                <Button
                  onClick={() =>
                    setDraft({
                      ...draft,
                      organizer: { ...draft.organizer, rules: [newRule(0)] },
                    })
                  }
                >
                  <Plus aria-hidden="true" />
                  添加规则
                </Button>
              }
            />
          ) : null}
          {rules.map((rule, index) => (
            <RuleEditor
              rule={rule}
              index={index}
              count={rules.length}
              siteOptions={draft.sites}
              initiallyExpanded={!savedRuleIds.has(rule.id)}
              onChange={(next) => updateRule(index, next)}
              onMove={(direction) => moveRule(index, direction)}
              onRemove={() => removeRule(index)}
              key={rule.id}
            />
          ))}
        </section>
      </section>

      <details className="preview-section organizer-preview">
        <summary className="section-toolbar">
          <div>
            <h2>测试规则</h2>
            <span>输入一个作品的信息，看看会保存到哪里（使用未保存的草稿）</span>
          </div>
          {previewResult ? <StatusBadge tone={previewResult.matched ? 'success' : 'warning'}>{previewResult.matched ? `命中 ${previewResult.name || ''}` : '未命中'}</StatusBadge> : null}
        </summary>
        <div className="preview-form">
          <Field label="站点">
            <select value={sample.source} onChange={(event) => setSample({ ...sample, source: event.target.value })}>
              {draft.sites.map((site) => (
                <option value={site.id} key={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="番号">
            <input value={sample.code} onChange={(event) => setSample({ ...sample, code: event.target.value })} />
          </Field>
          <Field label="标题">
            <input value={sample.title} onChange={(event) => setSample({ ...sample, title: event.target.value })} />
          </Field>
          <Field label="磁链名称">
            <input value={sample.name} onChange={(event) => setSample({ ...sample, name: event.target.value })} />
          </Field>
          <Button
            variant="secondary"
            onClick={() => preview.mutate({
              revision: previewRevision.current,
              settings: structuredClone(draft),
              sample: { ...sample },
            })}
            disabled={preview.isPending}
          >
            <TestTube2 aria-hidden="true" />
            {preview.isPending ? '测试中' : '测试匹配'}
          </Button>
        </div>
        {previewResult?.matched && previewResult.destination ? (
          <InlineNotice tone="success" role="status">
            最终下载目标：分类 {previewResult.destination.category} · {previewResult.destination.save_path}
          </InlineNotice>
        ) : null}
      </details>

      <details className="advanced-editor">
        <summary>
          <Braces aria-hidden="true" />
          高级：直接编辑规则 JSON
        </summary>
        <Field label="整理规则 JSON" error={advancedError}>
          <textarea
            value={advancedText}
            onChange={(event) => {
              editRevision.current += 1
              setAdvancedText(event.target.value)
              setAdvancedDirty(true)
              setAdvancedError('')
            }}
            rows={18}
            spellCheck={false}
          />
        </Field>
        <Button onClick={() => void applyAdvancedJson()} disabled={advancedApplying}>
          {advancedApplying ? '校验中' : '应用到草稿'}
        </Button>
      </details>
    </div>
  )
}

function RuleEditor({
  rule,
  index,
  count,
  siteOptions,
  initiallyExpanded,
  onChange,
  onMove,
  onRemove,
}: {
  rule: OrganizerRule
  index: number
  count: number
  siteOptions: AppSettings['sites']
  initiallyExpanded: boolean
  onChange: (rule: OrganizerRule) => void
  onMove: (direction: -1 | 1) => void
  onRemove: () => void
}) {
  const sources = rule.match.sources ?? []
  const titleTerms = rule.match.title_contains ?? []
  const magnetTerms = rule.match.magnet_name_contains ?? []
  const set = <K extends keyof OrganizerRule>(key: K, value: OrganizerRule[K]) => onChange({ ...rule, [key]: value })
  const setMatch = <K extends keyof OrganizerRule['match']>(key: K, value: OrganizerRule['match'][K]) => onChange({ ...rule, match: { ...rule.match, [key]: value } })
  const setAction = <K extends keyof OrganizerRule['actions']>(key: K, value: OrganizerRule['actions'][K]) => onChange({ ...rule, actions: { ...rule.actions, [key]: value } })
  const accessibleRuleName = `${rule.name.trim() || '未命名规则'}（第 ${index + 1} 条）`
  // Saved rules collapse to one summary line; new rules open for editing.
  const [expanded, setExpanded] = useState(initiallyExpanded)

  return (
    <article className={`rule-item ${rule.enabled ? '' : 'disabled'}`}>
      <header className="rule-item-header">
        <Toggle label={`启用规则 ${accessibleRuleName}`} checked={rule.enabled} onChange={(event) => set('enabled', event.target.checked)} />
        <div className="rule-title-input">
          <input aria-label={`规则名称：${accessibleRuleName}`} value={rule.name} onChange={(event) => set('name', event.target.value)} maxLength={100} />
          <span>{summarizeRule(rule)}</span>
        </div>
        <div className="rule-order-actions">
          <IconButton label={`上移规则 ${accessibleRuleName}`} size="small" onClick={() => onMove(-1)} disabled={index === 0}>
            <ArrowUp aria-hidden="true" />
          </IconButton>
          <IconButton label={`下移规则 ${accessibleRuleName}`} size="small" onClick={() => onMove(1)} disabled={index === count - 1}>
            <ArrowDown aria-hidden="true" />
          </IconButton>
          <IconButton label={`删除规则 ${accessibleRuleName}`} size="small" className="danger-icon" onClick={onRemove}>
            <Trash2 aria-hidden="true" />
          </IconButton>
          <Button type="button" size="small" variant="ghost" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起' : '编辑'}
          </Button>
        </div>
      </header>

      {expanded ? <>
      <section className="rule-section">
        <div className="rule-section-heading">
          <h3>匹配哪些作品</h3>
          <span>留空表示不限制；多个关键词用逗号分隔，命中任意一个即可</span>
        </div>
        <div className="rule-form-grid rule-match-grid">
          <Field label="标题包含">
            <TermsInput value={titleTerms} onChange={(value) => setMatch('title_contains', value)} />
          </Field>
          <Field label="磁链名称包含">
            <TermsInput value={magnetTerms} onChange={(value) => setMatch('magnet_name_contains', value)} />
          </Field>
        </div>
      </section>

      <section className="rule-section rule-destination-section">
        <div className="rule-section-heading">
          <h3>保存到哪里</h3>
          <span>只能是 JAV 暂存目录或其子目录；留空使用暂存根目录</span>
        </div>
        <div className="rule-action-grid">
          <Field label="保存路径">
            <input value={rule.actions.save_path} onChange={(event) => setAction('save_path', event.target.value)} placeholder="留空使用暂存根目录" />
          </Field>
          <Field label="标签" hint="写入 qBittorrent，便于筛选">
            <input value={rule.actions.tags} onChange={(event) => setAction('tags', event.target.value)} />
          </Field>
        </div>
      </section>

      <details className="rule-advanced-settings">
        <summary>
          <span className="rule-advanced-title">
            <SlidersHorizontal aria-hidden="true" />
            高级设置
          </span>
          <span>番号正则、适用来源、优先级、媒体类型与分类（通常无需修改）</span>
          <ChevronRight className="rule-advanced-chevron" aria-hidden="true" />
        </summary>
        <div className="rule-form-grid rule-advanced-grid">
          <Field label="规则 ID">
            <input value={rule.id} onChange={(event) => set('id', event.target.value.toLowerCase())} maxLength={64} />
          </Field>
          <Field label="优先级" hint="数值越小越先匹配">
            <input type="number" min={0} max={99_999} value={rule.priority} onChange={(event) => set('priority', Number(event.target.value))} />
          </Field>
          <Field label="番号正则">
            <input value={rule.match.code_regex} onChange={(event) => setMatch('code_regex', event.target.value)} placeholder="正则表达式，留空表示不限" />
          </Field>
          <Field label="媒体类型">
            <select value={rule.actions.media_type} onChange={(event) => setAction('media_type', event.target.value as OrganizerRule['actions']['media_type'])}>
              <option value="movie">影片</option>
              <option value="series">系列</option>
              <option value="other">其他</option>
            </select>
          </Field>
          <Field label="JAV 分类" hint="留空继承设置页的 JAV 分类；填写时必须完全一致">
            <input value={rule.actions.category} onChange={(event) => setAction('category', event.target.value)} placeholder="留空继承当前 JAV 分类" />
          </Field>
          <SourceScopeEditor sources={sources} siteOptions={siteOptions} onChange={(next) => setMatch('sources', next)} />
        </div>
      </details>
      </> : null}
    </article>
  )
}

export function SourceScopeEditor({
  sources,
  siteOptions,
  onChange,
}: {
  sources: string[]
  siteOptions: AppSettings['sites']
  onChange: (sources: string[]) => void
}) {
  const scopeLabel = sources.length ? `已限定 ${sources.length} 个站点` : '全部来源'

  return (
    <details className="source-scope-editor">
      <summary>
        <span className="source-scope-title">
          <Globe2 aria-hidden="true" />
          <span>适用来源</span>
        </span>
        <StatusBadge tone={sources.length ? 'info' : 'neutral'}>{scopeLabel}</StatusBadge>
        <ChevronRight className="source-scope-chevron" aria-hidden="true" />
      </summary>
      <div className="source-scope-content">
        <div className="source-scope-options" role="group" aria-label="限定来源站点">
          {siteOptions.length ? (
            siteOptions.map((site) => (
              <label key={site.id}>
                <input
                  type="checkbox"
                  checked={sources.includes(site.id)}
                  onChange={(event) => onChange(event.target.checked ? [...sources, site.id] : sources.filter((item) => item !== site.id))}
                />
                <span>{site.name}</span>
              </label>
            ))
          ) : (
            <span className="source-scope-empty">暂无可用站点</span>
          )}
        </div>
        {sources.length ? (
          <Button type="button" size="small" variant="ghost" onClick={() => onChange([])}>
            <X aria-hidden="true" />
            取消限制
          </Button>
        ) : null}
      </div>
    </details>
  )
}

function TermsInput({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const [raw, setRaw] = useState(value.join(', '))
  const focused = useRef(false)
  const serialized = value.join(', ')

  useEffect(() => {
    if (!focused.current) setRaw(serialized)
  }, [serialized])

  return (
    <input
      value={raw}
      onFocus={() => {
        focused.current = true
      }}
      onChange={(event) => {
        setRaw(event.target.value)
        onChange(splitTerms(event.target.value))
      }}
      onBlur={() => {
        focused.current = false
        setRaw(splitTerms(raw).join(', '))
      }}
    />
  )
}
