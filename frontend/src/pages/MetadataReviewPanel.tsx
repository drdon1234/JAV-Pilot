import { useMutation } from '@tanstack/react-query'
import { Ban, Check, FileCheck2, Image, LockKeyhole, RefreshCw, Save, Upload, X } from 'lucide-react'
import { type ChangeEvent, type FormEvent, useEffect, useMemo, useRef, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, IconButton, InlineNotice, StatusBadge } from '../components/ui'
import { ApiError, api } from '../lib/api'
import { formatBytes, formatDateTime } from '../lib/format'
import { METADATA_PROFILES, PROFILE_LABELS } from '../lib/sources'
import type {
  MediaMetadataReview,
  MediaMetadataReviewDraftRequest,
  MediaMetadataReviewField,
  MediaMetadataReviewFieldName,
  MediaMetadataReviewImageKind,
  MediaMetadataReviewPreviewPayload,
  MediaMetadataReviewSource,
  MediaMetadataReviewValue,
} from '../types'

const FIELD_DEFINITIONS: Array<{
  name: MediaMetadataReviewFieldName
  label: string
  kind: 'text' | 'long-text' | 'list' | 'integer' | 'rating' | 'date'
}> = [
  { name: 'title', label: '标题', kind: 'text' },
  { name: 'original_title', label: '原始标题', kind: 'text' },
  { name: 'release_date', label: '发行日期', kind: 'date' },
  { name: 'duration_minutes', label: '时长（分钟）', kind: 'integer' },
  { name: 'rating', label: '评分', kind: 'rating' },
  { name: 'makers', label: '制作商', kind: 'list' },
  { name: 'publishers', label: '发行商', kind: 'list' },
  { name: 'series', label: '系列', kind: 'list' },
  { name: 'directors', label: '导演', kind: 'list' },
  { name: 'actors', label: '演员', kind: 'list' },
  { name: 'tags', label: '标签', kind: 'list' },
  { name: 'description', label: '简介', kind: 'long-text' },
]

const SOURCE_DEFINITIONS: Array<{ id: MediaMetadataReviewSource; label: string }> = [
  { id: 'nfo', label: '当前 NFO' },
  ...METADATA_PROFILES.map((id) => ({ id, label: id === 'fc2' ? 'FC2' : PROFILE_LABELS[id] })),
  { id: 'missav', label: 'MissAV' },
]

const REMOTE_SOURCES = [
  ...METADATA_PROFILES.map((id) => ({ id, label: id === 'fc2' ? 'FC2' : PROFILE_LABELS[id] })),
  { id: 'missav' as const, label: 'MissAV' },
]

const IMAGE_DEFINITIONS = [
  { id: 'portrait' as const, label: '竖版海报' },
  { id: 'landscape' as const, label: '横版海报' },
]

type RemoteSource = (typeof REMOTE_SOURCES)[number]['id']
type FieldDefinition = (typeof FIELD_DEFINITIONS)[number]

function sourceLabel(source: string | null): string {
  if (!source || source === 'default') return '自动默认'
  if (source === 'manual') return '人工值'
  if (source === 'locked') return '已锁定值'
  return SOURCE_DEFINITIONS.find((item) => item.id === source)?.label || source
}

function displayValue(value: MediaMetadataReviewValue): string {
  if (Array.isArray(value)) return value.length ? value.join('、') : '空列表'
  if (value === null || value === '') return '未获取'
  return String(value)
}

function editorValue(value: MediaMetadataReviewValue): string {
  if (Array.isArray(value)) return value.join('、')
  if (value === null) return ''
  return String(value)
}

function sourceSupportsField(source: MediaMetadataReviewSource, fieldName: MediaMetadataReviewFieldName): boolean {
  if (source === 'nfo') return true
  if (source === 'missav') return fieldName === 'description'
  return fieldName !== 'description' || !['javbus', 'javdb', 'fc2'].includes(source)
}

function reviewErrorMessage(error: unknown, action: 'open' | 'save' | 'abandon' | 'refetch' | 'upload' | 'preview' | 'publish'): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return '内容已变化，请刷新审校内容并重新预览后再发布。'
    if (error.status === 404) return action === 'open' ? '媒体文件或审校记录不存在，请刷新任务后重试。' : '审校记录不存在，请关闭后重新打开。'
    if (error.status === 422) return '所选字段、来源或图片无效，请检查后重试。'
    if (error.status === 502) return '所选站点暂时无法完成解析，已有字段不会被清除。'
    if (error.status === 503) return '元数据审校服务暂不可用，请稍后重试。'
  }
  return action === 'publish' ? '发布未完成，未确认的文件不会写入媒体库。' : '操作未完成，请稍后重试。'
}

function parseManualValue(definition: FieldDefinition, raw: string): { value?: MediaMetadataReviewValue; error?: string } {
  const clean = raw.trim()
  if (definition.kind === 'list') {
    const values = raw
      .split(/[,，\n]/)
      .map((item) => item.trim())
      .filter(Boolean)
    return { value: Array.from(new Set(values)) }
  }
  if (definition.kind === 'integer') {
    if (!clean) return { value: null }
    const value = Number(clean)
    return Number.isInteger(value) && value >= 1 && value <= 1440
      ? { value }
      : { error: '请输入 1 到 1440 的整数' }
  }
  if (definition.kind === 'rating') {
    if (!clean) return { value: null }
    const value = Number(clean)
    return Number.isFinite(value) && value >= 0 && value <= 10
      ? { value }
      : { error: '请输入 0 到 10 的数字' }
  }
  if (definition.kind === 'date' && clean && !/^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(clean)) {
    return { error: '使用 YYYY、YYYY-MM 或 YYYY-MM-DD 格式' }
  }
  if (definition.name === 'title' && !clean) return { error: '标题不能为空' }
  return { value: clean || null }
}

interface MetadataFieldDraft {
  useManual: boolean
  manualValue: string
  selectedSource: MediaMetadataReviewSource | 'auto'
  locked: boolean
}

function createFieldDraft(field: MediaMetadataReviewField): MetadataFieldDraft {
  return {
    useManual: field.manual_set,
    manualValue: editorValue(field.manual_set ? field.manual_value : field.final_value),
    selectedSource: field.selected_source || 'auto',
    locked: field.locked,
  }
}

function fieldDraftVersion(draft: MetadataFieldDraft): string {
  return JSON.stringify(draft)
}

function fieldChoiceVersion(definition: FieldDefinition, draft: MetadataFieldDraft): string {
  const parsed = draft.useManual ? parseManualValue(definition, draft.manualValue) : {}
  return JSON.stringify({
    useManual: draft.useManual,
    manualValue: draft.useManual
      ? parsed.error ? draft.manualValue : parsed.value ?? null
      : null,
    selectedSource: draft.selectedSource,
    locked: draft.locked,
  })
}

function readFileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('file read failed'))
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const separator = result.indexOf(',')
      if (separator < 0) {
        reject(new Error('file encoding failed'))
        return
      }
      resolve(result.slice(separator + 1))
    }
    reader.readAsDataURL(file)
  })
}

function toggleItem<T extends string>(items: T[], item: T, checked: boolean): T[] {
  return checked ? Array.from(new Set([...items, item])) : items.filter((value) => value !== item)
}

function MetadataReviewFieldEditor({
  definition,
  field,
  saving,
  disabled,
  onSave,
}: {
  definition: FieldDefinition
  field: MediaMetadataReviewField
  saving: boolean
  disabled: boolean
  onSave: (request: Omit<MediaMetadataReviewDraftRequest, 'review_id' | 'expected_revision'>) => void
}) {
  const [draft, setDraft] = useState<MetadataFieldDraft>(() => createFieldDraft(field))
  const [baseline, setBaseline] = useState<MetadataFieldDraft>(() => createFieldDraft(field))
  const [validationError, setValidationError] = useState<string | null>(null)

  const { useManual, manualValue, selectedSource, locked } = draft
  const incomingDraft = createFieldDraft(field)
  const incomingVersion = fieldDraftVersion(incomingDraft)
  const baselineVersion = fieldDraftVersion(baseline)
  const currentChoiceVersion = fieldChoiceVersion(definition, draft)
  const incomingChoiceVersion = fieldChoiceVersion(definition, incomingDraft)
  const dirty = useManual !== baseline.useManual
    || (useManual && manualValue !== baseline.manualValue)
    || selectedSource !== baseline.selectedSource
    || locked !== baseline.locked

  useEffect(() => {
    if (incomingVersion === baselineVersion) return
    if (dirty && currentChoiceVersion !== incomingChoiceVersion) return
    const nextDraft = createFieldDraft(field)
    setDraft(nextDraft)
    setBaseline(nextDraft)
    setValidationError(null)
  }, [baselineVersion, currentChoiceVersion, dirty, field, incomingChoiceVersion, incomingVersion])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const parsed = parseManualValue(definition, manualValue)
    if (useManual && parsed.error) {
      setValidationError(parsed.error)
      return
    }
    setValidationError(null)
    onSave({
      ...(useManual ? { manual_values: { [definition.name]: parsed.value ?? null } } : {}),
      ...(!useManual && field.manual_set ? { clear_manual: [definition.name] } : {}),
      source_choices: { [definition.name]: selectedSource },
      locks: { [definition.name]: locked },
    })
  }

  const inputId = `metadata-review-manual-${definition.name}`
  const errorId = `${inputId}-error`
  const sourceOptions = SOURCE_DEFINITIONS.filter((item) => sourceSupportsField(item.id, definition.name))

  return (
    <article className={`metadata-review-field${field.differs ? ' has-difference' : ''}`}>
      <header>
        <div>
          <h3>{definition.label}</h3>
          {field.differs ? <StatusBadge tone="warning">来源有差异</StatusBadge> : null}
          {field.locked ? <StatusBadge><LockKeyhole aria-hidden="true" />已锁定</StatusBadge> : null}
        </div>
        <p title={displayValue(field.final_value)}>
          <span>{sourceLabel(field.final_source)}</span>
          <strong>{displayValue(field.final_value)}</strong>
        </p>
      </header>

      <details className="metadata-review-source-details" open={field.differs ? true : undefined}>
        <summary>比较 {SOURCE_DEFINITIONS.length} 个来源</summary>
        <div className="metadata-review-source-table" role="table" aria-label={`${definition.label}来源比较`}>
          {SOURCE_DEFINITIONS.map((source) => {
            const sourceValue = field.sources[source.id]
            const supported = sourceSupportsField(source.id, definition.name)
            return (
              <div role="row" key={source.id}>
                <strong role="rowheader">{source.label}</strong>
                <span role="cell" title={sourceValue ? displayValue(sourceValue.value) : undefined}>
                  {supported ? sourceValue ? displayValue(sourceValue.value) : '未获取' : '不提供此字段'}
                </span>
              </div>
            )
          })}
        </div>
      </details>

      <form className="metadata-review-field-form" onSubmit={submit}>
        <label>
          <span>采用来源</span>
          <select
            aria-label={`${definition.label}采用来源`}
            value={selectedSource}
            onChange={(event) => setDraft((current) => ({ ...current, selectedSource: event.target.value as MediaMetadataReviewSource | 'auto' }))}
            disabled={disabled || useManual}
          >
            <option value="auto">自动优先级</option>
            {sourceOptions.map((source) => (
              <option
                value={source.id}
                disabled={!field.sources[source.id] && field.selected_source !== source.id}
                key={source.id}
              >
                {source.label}{field.sources[source.id] ? '' : '（未获取）'}
              </option>
            ))}
          </select>
        </label>
        <label className="metadata-review-check-option">
          <input
            type="checkbox"
            checked={useManual}
            onChange={(event) => setDraft((current) => ({ ...current, useManual: event.target.checked }))}
            disabled={disabled}
          />
          <span>使用人工值</span>
        </label>
        <label className="metadata-review-check-option">
          <input
            type="checkbox"
            checked={locked}
            onChange={(event) => setDraft((current) => ({ ...current, locked: event.target.checked }))}
            disabled={disabled}
          />
          <span>锁定字段</span>
        </label>
        <label className="metadata-review-manual-field">
          <span>{definition.label}人工值</span>
          {definition.kind === 'long-text' || definition.kind === 'list' ? (
            <textarea
              id={inputId}
              aria-invalid={validationError ? true : undefined}
              aria-describedby={validationError ? errorId : undefined}
              rows={definition.kind === 'long-text' ? 4 : 2}
              value={manualValue}
              placeholder={definition.kind === 'list' ? '使用逗号或换行分隔' : undefined}
              onChange={(event) => setDraft((current) => ({ ...current, manualValue: event.target.value }))}
              disabled={disabled || !useManual}
            />
          ) : (
            <input
              id={inputId}
              type={definition.kind === 'integer' || definition.kind === 'rating' ? 'number' : 'text'}
              min={definition.kind === 'rating' ? 0 : definition.kind === 'integer' ? 1 : undefined}
              max={definition.kind === 'rating' ? 10 : definition.kind === 'integer' ? 1440 : undefined}
              step={definition.kind === 'rating' ? '0.1' : definition.kind === 'integer' ? '1' : undefined}
              value={manualValue}
              aria-invalid={validationError ? true : undefined}
              aria-describedby={validationError ? errorId : undefined}
              onChange={(event) => setDraft((current) => ({ ...current, manualValue: event.target.value }))}
              disabled={disabled || !useManual}
            />
          )}
          {validationError ? <span className="field-error" id={errorId}>{validationError}</span> : null}
        </label>
        <Button type="submit" size="small" disabled={disabled || !dirty}>
          <Save aria-hidden="true" />
          {saving ? '保存中' : `保存${definition.label}`}
        </Button>
      </form>
    </article>
  )
}

export function MetadataReviewPanel({
  initialReview,
  onClose,
  onPublished,
}: {
  initialReview: MediaMetadataReview
  onClose: () => void
  onPublished: () => void
}) {
  const toast = useToast()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [review, setReview] = useState(initialReview)
  const [imageRefs, setImageRefs] = useState<Partial<Record<MediaMetadataReviewImageKind, string>>>({})
  const [preview, setPreview] = useState<MediaMetadataReviewPreviewPayload | null>(null)
  const [refetchSources, setRefetchSources] = useState<RemoteSource[]>([])
  const [refetchFields, setRefetchFields] = useState<MediaMetadataReviewFieldName[]>([])
  const [refetchImages, setRefetchImages] = useState<MediaMetadataReviewImageKind[]>([])
  const [includeNfo, setIncludeNfo] = useState(true)
  const [includePortrait, setIncludePortrait] = useState(false)
  const [includeLandscape, setIncludeLandscape] = useState(false)
  const [refetchNotice, setRefetchNotice] = useState<string | null>(null)
  const [abandonConfirming, setAbandonConfirming] = useState(false)

  useEffect(() => {
    setReview(initialReview)
    setImageRefs({})
    setPreview(null)
    setAbandonConfirming(false)
  }, [initialReview])

  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  async function reloadReview() {
    try {
      const payload = await api.mediaMetadataReview(review.review_id)
      setReview(payload.review)
    } catch {
      // Keep the last safe snapshot visible; the next explicit action can retry.
    }
  }

  const saveDraft = useMutation({
    mutationFn: ({ request }: {
      fieldName: MediaMetadataReviewFieldName
      request: Omit<MediaMetadataReviewDraftRequest, 'review_id' | 'expected_revision'>
    }) => api.updateMediaMetadataReviewDraft({
      review_id: review.review_id,
      expected_revision: review.revision,
      ...request,
    }),
    onSuccess: (payload) => {
      setReview(payload.review)
      setPreview(null)
      toast.push('字段草稿已保存', 'success')
    },
    onError: (error) => {
      toast.push(reviewErrorMessage(error, 'save'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const refetch = useMutation({
    mutationFn: () => api.refetchMediaMetadataReview({
      review_id: review.review_id,
      expected_revision: review.revision,
      sources: refetchSources,
      fields: refetchFields,
      images: refetchImages,
    }),
    onSuccess: (payload) => {
      setReview(payload.review)
      setImageRefs((current) => ({ ...current, ...payload.image_refs }))
      setPreview(null)
      if (payload.intent.status === 'completed') {
        setRefetchNotice('所选内容已重抓并加入审校快照。')
        toast.push('所选元数据已重抓', 'success')
      } else {
        setRefetchNotice('部分内容暂未获取，原有字段与图片快照均已保留。')
        toast.push('重抓完成，未获取内容已保留原值', 'info')
      }
    },
    onError: (error) => {
      toast.push(reviewErrorMessage(error, 'refetch'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const abandon = useMutation({
    mutationFn: () => api.abandonMediaMetadataReview(review.review_id, review.revision),
    onSuccess: (payload) => {
      setReview(payload.review)
      setPreview(null)
      setAbandonConfirming(false)
      toast.push('已放弃当前版本审校', 'success')
    },
    onError: (error) => {
      toast.push(reviewErrorMessage(error, 'abandon'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const uploadImage = useMutation({
    mutationFn: ({ kind, bodyBase64 }: { kind: MediaMetadataReviewImageKind; bodyBase64: string }) =>
      api.uploadMediaMetadataReviewImage(
        review.review_id,
        kind,
        bodyBase64,
        review.revision,
      ),
    onSuccess: (payload, variables) => {
      setReview(payload.review)
      setImageRefs((current) => ({ ...current, [variables.kind]: payload.image_ref }))
      setPreview(null)
      toast.push(`${variables.kind === 'portrait' ? '竖版' : '横版'}海报已暂存`, 'success')
    },
    onError: (error) => {
      toast.push(reviewErrorMessage(error, 'upload'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const createPreview = useMutation({
    mutationFn: () => api.previewMediaMetadataReview({
      review_id: review.review_id,
      include_nfo: includeNfo,
      ...(includePortrait && imageRefs.portrait ? { portrait_ref: imageRefs.portrait } : {}),
      ...(includeLandscape && imageRefs.landscape ? { landscape_ref: imageRefs.landscape } : {}),
      expected_revision: review.revision,
    }),
    onSuccess: (payload) => setPreview(payload),
    onError: (error) => {
      setPreview(null)
      toast.push(reviewErrorMessage(error, 'preview'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const publish = useMutation({
    mutationFn: (previewToken: string) => api.publishMediaMetadataReview(previewToken),
    onSuccess: () => {
      setPreview(null)
      toast.push('元数据与所选图片已发布', 'success')
      void reloadReview()
      onPublished()
    },
    onError: (error) => {
      setPreview(null)
      toast.push(reviewErrorMessage(error, 'publish'), 'error')
      if (error instanceof ApiError && error.status === 409) void reloadReview()
    },
  })

  const refetchError = useMemo(() => {
    if (!refetchFields.length && !refetchImages.length) return '至少选择一个字段或一种图片'
    if (!refetchSources.length) return null
    if (refetchFields.some((field) => !refetchSources.some((source) => sourceSupportsField(source, field)))) {
      return '请为每个所选字段选择支持重抓的来源'
    }
    if (refetchImages.length && !refetchSources.some((source) => source !== 'missav')) {
      return '图片需要选择支持详情资料的来源'
    }
    return null
  }, [refetchFields, refetchImages, refetchSources])

  const anyPending = saveDraft.isPending || abandon.isPending || refetch.isPending || uploadImage.isPending || createPreview.isPending || publish.isPending
  const hasPreviewSelection = includeNfo
    || (includePortrait && Boolean(imageRefs.portrait))
    || (includeLandscape && Boolean(imageRefs.landscape))

  async function handleUpload(kind: MediaMetadataReviewImageKind, event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0]
    event.currentTarget.value = ''
    if (!file) return
    if (file.size > 12 * 1024 * 1024) {
      toast.push('图片不能超过 12 MB', 'error')
      return
    }
    try {
      const bodyBase64 = await readFileBase64(file)
      uploadImage.mutate({ kind, bodyBase64 })
    } catch {
      toast.push('无法读取所选图片，请更换文件后重试。', 'error')
    }
  }

  function invalidatePreview(update: () => void) {
    setPreview(null)
    update()
  }

  return (
    <section className="metadata-review-workspace" aria-labelledby="metadata-review-heading">
      <header className="metadata-review-header">
        <div>
          <StatusBadge tone={review.abandoned ? 'warning' : 'info'}>
            {review.abandoned ? '已放弃当前版本' : '人工审校'}
          </StatusBadge>
          <h2 id="metadata-review-heading" tabIndex={-1} ref={headingRef}>{review.code} 元数据审校</h2>
          <code title={review.relative_media_path}>{review.relative_media_path}</code>
        </div>
        <IconButton label="关闭元数据审校" onClick={onClose} disabled={publish.isPending}>
          <X aria-hidden="true" />
        </IconButton>
      </header>

      <InlineNotice tone="info">
        <span>来源值、人工值与锁定状态仅保存为草稿；只有完成发布预览并再次确认，才会写入 NFO 或图片文件。</span>
      </InlineNotice>

      <div className="metadata-review-layout">
        <div className="metadata-review-fields" aria-label="字段来源对比与编辑">
          <div className="metadata-review-section-heading">
            <div>
              <h2>字段与来源</h2>
              <span>字段来源按站点优先级合并；FC2 作品优先使用官方详情，MissAV 仅提供可空简介。</span>
            </div>
            <StatusBadge>{FIELD_DEFINITIONS.filter((item) => review.fields[item.name].differs).length} 个差异</StatusBadge>
          </div>
          {FIELD_DEFINITIONS.map((definition) => (
            <MetadataReviewFieldEditor
              definition={definition}
              field={review.fields[definition.name]}
              saving={saveDraft.isPending && saveDraft.variables?.fieldName === definition.name}
              disabled={anyPending}
              onSave={(request) => saveDraft.mutate({ fieldName: definition.name, request })}
              key={definition.name}
            />
          ))}
        </div>

        <aside className="metadata-review-actions" aria-label="重抓与发布控制">
          <details className="metadata-review-refetch" open>
            <summary><RefreshCw aria-hidden="true" />按范围重抓</summary>
            <form onSubmit={(event) => {
              event.preventDefault()
              if (!refetchError) refetch.mutate()
            }}>
              <fieldset>
                <legend>来源</legend>
                <div className="metadata-review-option-grid source-options">
                  {REMOTE_SOURCES.map((source) => (
                    <label className="metadata-review-check-option" key={source.id}>
                      <input
                        type="checkbox"
                        checked={refetchSources.includes(source.id)}
                        onChange={(event) => setRefetchSources((items) => toggleItem(items, source.id, event.target.checked))}
                        disabled={anyPending}
                      />
                      <span>{source.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset>
                <legend>字段</legend>
                <div className="metadata-review-option-grid field-options">
                  {FIELD_DEFINITIONS.map((field) => (
                    <label className="metadata-review-check-option" key={field.name}>
                      <input
                        type="checkbox"
                        checked={refetchFields.includes(field.name)}
                        onChange={(event) => setRefetchFields((items) => toggleItem(items, field.name, event.target.checked))}
                        disabled={anyPending}
                      />
                      <span>{field.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset>
                <legend>图片</legend>
                <div className="metadata-review-option-grid source-options">
                  {IMAGE_DEFINITIONS.map((image) => (
                    <label className="metadata-review-check-option" key={image.id}>
                      <input
                        type="checkbox"
                        checked={refetchImages.includes(image.id)}
                        onChange={(event) => setRefetchImages((items) => toggleItem(items, image.id, event.target.checked))}
                        disabled={anyPending}
                      />
                      <span>{image.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <span className={refetchError ? 'field-error' : 'field-hint'}>
                {refetchError || '未选择来源时，服务会按字段匹配可用站点；点击前不会发起网络请求。'}
              </span>
              <Button type="submit" size="small" disabled={anyPending || Boolean(refetchError)}>
                <RefreshCw className={refetch.isPending ? 'spin' : ''} aria-hidden="true" />
                {refetch.isPending ? '重抓中' : '重抓所选内容'}
              </Button>
              {refetchNotice ? <span className="metadata-review-refetch-result" role="status">{refetchNotice}</span> : null}
            </form>
          </details>

          <section className="metadata-review-images" aria-labelledby="metadata-review-images-heading">
            <div className="metadata-review-section-heading">
              <div>
                <h2 id="metadata-review-images-heading">图片快照</h2>
                <span>重抓或上传后的图片只进入暂存区。</span>
              </div>
            </div>
            {IMAGE_DEFINITIONS.map((image) => {
              const snapshots = review.images[image.id] || {}
              return (
                <div className="metadata-review-image-row" key={image.id}>
                  <div>
                    <Image aria-hidden="true" />
                    <strong>{image.label}</strong>
                    {imageRefs[image.id] ? <StatusBadge tone="success"><Check aria-hidden="true" />已暂存</StatusBadge> : null}
                  </div>
                  {Object.keys(snapshots).length ? (
                    <ul aria-label={`${image.label}来源`}>
                      {Object.entries(snapshots).map(([source, snapshot]) => snapshot ? (
                        <li key={source}>
                          <span>{sourceLabel(source)}</span>
                          <span>{snapshot.width} × {snapshot.height}</span>
                        </li>
                      ) : null)}
                    </ul>
                  ) : <span className="field-hint">尚无图片快照</span>}
                  <label className="metadata-review-file-input">
                    <Upload aria-hidden="true" />
                    <span>上传{image.label}</span>
                    <input
                      type="file"
                      accept="image/jpeg,image/png,image/webp"
                      aria-label={`上传${image.label}`}
                      onChange={(event) => void handleUpload(image.id, event)}
                      disabled={anyPending}
                    />
                  </label>
                </div>
              )
            })}
          </section>

          <section className="metadata-review-publish" aria-labelledby="metadata-review-publish-heading">
            <div className="metadata-review-section-heading">
              <div>
                <h2 id="metadata-review-publish-heading">发布确认</h2>
                <span>NFO、竖图和横图分别确认。</span>
              </div>
            </div>
            <div className="metadata-review-publish-options">
              <label className="metadata-review-check-option">
                <input
                  type="checkbox"
                  checked={includeNfo}
                  onChange={(event) => invalidatePreview(() => setIncludeNfo(event.target.checked))}
                  disabled={anyPending}
                />
                <span>写入 NFO</span>
              </label>
              <label className="metadata-review-check-option">
                <input
                  type="checkbox"
                  checked={includePortrait}
                  onChange={(event) => invalidatePreview(() => setIncludePortrait(event.target.checked))}
                  disabled={anyPending || !imageRefs.portrait}
                />
                <span>写入竖版海报</span>
              </label>
              <label className="metadata-review-check-option">
                <input
                  type="checkbox"
                  checked={includeLandscape}
                  onChange={(event) => invalidatePreview(() => setIncludeLandscape(event.target.checked))}
                  disabled={anyPending || !imageRefs.landscape}
                />
                <span>写入横版海报</span>
              </label>
            </div>
            <Button
              type="button"
              size="small"
              onClick={() => createPreview.mutate()}
              disabled={anyPending || !hasPreviewSelection}
            >
              <FileCheck2 aria-hidden="true" />
              {createPreview.isPending ? '生成中' : '生成发布预览'}
            </Button>

            {preview ? (
              <div className="metadata-review-preview" role="status">
                <strong>发布预览</strong>
                <span>有效至 {formatDateTime(preview.expires_at)}</span>
                <ul aria-label="待发布文件">
                  {preview.artifacts.map((artifact) => (
                    <li key={`${artifact.kind}:${artifact.relative_path}`}>
                      <span>{artifact.kind === 'nfo' ? 'NFO' : artifact.kind === 'portrait' ? '竖版海报' : '横版海报'}</span>
                      <strong>{artifact.action === 'create' ? '新建' : artifact.action === 'replace' ? '替换并备份' : '内容不变'}</strong>
                      <code>{artifact.relative_path}</code>
                      <span>{formatBytes(artifact.proposed_bytes)}</span>
                    </li>
                  ))}
                </ul>
                <div>
                  <Button
                    type="button"
                    size="small"
                    variant="primary"
                    onClick={() => publish.mutate(preview.preview_token)}
                    disabled={publish.isPending}
                  >
                    <Check aria-hidden="true" />
                    {publish.isPending ? '发布中' : '确认发布'}
                  </Button>
                  <Button type="button" size="small" variant="ghost" onClick={() => setPreview(null)} disabled={publish.isPending}>
                    取消预览
                  </Button>
                </div>
              </div>
            ) : null}
          </section>

          <section className="metadata-review-abandon" aria-labelledby="metadata-review-abandon-heading">
            <div className="metadata-review-section-heading">
              <div>
                <h2 id="metadata-review-abandon-heading">审校状态</h2>
                <span>放弃标记仅针对当前内容版本。</span>
              </div>
            </div>
            <div className="metadata-review-abandon-content">
              {review.abandoned ? (
                <span role="status">当前版本已放弃，不再阻塞元数据历史清理；编辑字段或更新来源后会自动恢复待审校。</span>
              ) : (
                <>
                  <span>不准备发布当前草稿时，可将本版本标记为放弃。草稿与来源快照仍会保留。</span>
                  {abandonConfirming ? (
                    <div className="metadata-review-abandon-actions">
                      <Button type="button" size="small" variant="danger" onClick={() => abandon.mutate()} disabled={anyPending}>
                        <Ban aria-hidden="true" />
                        {abandon.isPending ? '处理中' : '确认放弃'}
                      </Button>
                      <Button type="button" size="small" variant="ghost" onClick={() => setAbandonConfirming(false)} disabled={abandon.isPending}>
                        取消
                      </Button>
                    </div>
                  ) : (
                    <Button type="button" size="small" variant="ghost" onClick={() => setAbandonConfirming(true)} disabled={anyPending}>
                      <Ban aria-hidden="true" />
                      放弃本次审校
                    </Button>
                  )}
                </>
              )}
            </div>
          </section>
        </aside>
      </div>
    </section>
  )
}
