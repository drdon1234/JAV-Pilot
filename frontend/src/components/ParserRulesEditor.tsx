import { ArrowDown, ArrowUp, Braces, ChevronRight, Plus, RotateCcw, Trash2 } from 'lucide-react'
import { useId, useMemo } from 'react'

import type { ParserRuleAttribute, ParserValueRule, SiteParserRules, SiteSettings } from '../types'
import { Button, Field, IconButton, InlineNotice, StatusBadge } from './ui'

const ATTRIBUTE_OPTIONS: ReadonlyArray<{ value: ParserRuleAttribute; label: string }> = [
  { value: 'text', label: '文本内容' },
  { value: 'href', label: '链接 href' },
  { value: 'src', label: '资源 src' },
  { value: 'title', label: '标题 title' },
  { value: 'alt', label: '替代文本 alt' },
  { value: 'datetime', label: '日期时间 datetime' },
  { value: 'content', label: '内容 content' },
  { value: 'poster', label: '封面 poster' },
  { value: 'data-src', label: '延迟资源 data-src' },
  { value: 'data-href', label: '延迟链接 data-href' },
  { value: 'data-url', label: '数据链接 data-url' },
  { value: 'data-original', label: '原图 data-original' },
  { value: 'data-lazy-src', label: '延迟原图 data-lazy-src' },
]

const ATTRIBUTE_LABELS = Object.fromEntries(ATTRIBUTE_OPTIONS.map((option) => [option.value, option.label])) as Record<ParserRuleAttribute, string>

const DETAIL_SCALAR_FIELDS: ReadonlyArray<{ key: keyof SiteParserRules['detail']['fields']; label: string }> = [
  { key: 'title', label: '标题' },
  { key: 'original_title', label: '原始标题' },
  { key: 'release_date', label: '发行日期' },
  { key: 'duration', label: '时长' },
  { key: 'rating', label: '评分' },
]

const DETAIL_RELATION_FIELDS: ReadonlyArray<{ key: keyof SiteParserRules['detail']['fields']; label: string }> = [
  { key: 'maker', label: '片商' },
  { key: 'publisher', label: '发行商' },
  { key: 'series', label: '系列' },
  { key: 'director', label: '导演' },
  { key: 'actor', label: '演员' },
  { key: 'tag', label: '标签' },
]

function valueRule(attributes: ParserRuleAttribute[]): ParserValueRule {
  return { selector: '', attributes }
}

export function createEmptyParserRules(): SiteParserRules {
  return {
    schema_version: 1,
    search: {
      item_selector: ':scope',
      empty_selector: '',
      ready_selector: '',
      detail_url: valueRule(['href']),
      title: valueRule(['text', 'title', 'alt']),
      code: valueRule(['text', 'title']),
      date: valueRule(['text']),
      rating: valueRule(['text']),
      cover: valueRule(['src', 'data-src', 'data-original', 'data-lazy-src']),
      magnet_available_selector: '',
      default_magnet_hint: 'unknown',
    },
    detail: {
      fields: {
        title: valueRule(['text']),
        original_title: valueRule(['text']),
        release_date: valueRule(['text']),
        duration: valueRule(['text']),
        rating: valueRule(['text']),
        maker: valueRule(['text']),
        publisher: valueRule(['text']),
        series: valueRule(['text']),
        director: valueRule(['text']),
        actor: valueRule(['text']),
        tag: valueRule(['text']),
      },
      images: {
        cover: valueRule(['href', 'src', 'data-src']),
        backdrop: valueRule(['href', 'src', 'data-src']),
        sample: valueRule(['href', 'src', 'data-src']),
      },
      magnets: {
        item_selector: '',
        uri: valueRule(['href', 'data-href', 'data-url']),
        name: valueRule(['text', 'title']),
        size: valueRule(['text']),
        badges: valueRule(['text', 'title']),
      },
    },
  }
}

export type ParserRuleErrors = Record<string, string>

function validateValueRule(path: string, rule: ParserValueRule, errors: ParserRuleErrors) {
  validateSelector(`${path}.selector`, rule.selector, errors)
  if (!rule.attributes.length || rule.attributes.length > 6) errors[`${path}.attributes`] = '读取来源必须保留 1 到 6 项'
  if (rule.index !== undefined && (!Number.isInteger(rule.index) || rule.index < 0 || rule.index > 49)) errors[`${path}.index`] = '匹配位置必须是 0 到 49 的整数'
}

function validateSelector(path: string, selector: string, errors: ParserRuleErrors, required = false) {
  const value = selector.trim()
  if (!value) {
    if (required) errors[path] = '结果卡片选择器不能为空'
    return
  }
  if (value.length > 240) {
    errors[path] = '选择器最多 240 个字符'
    return
  }
  if (value.split(',').length > 8) errors[path] = '选择器最多包含 8 组'
  const componentCount = Array.from(value).filter((character) => [' ', '>', '+', '~'].includes(character)).length + 1
  if (componentCount > 24) errors[path] = '选择器最多包含 24 个组成部分'
  if (/:(?:has|contains|-soup-contains)\s*\(|javascript:/i.test(value)) errors[path] = '选择器包含不支持的特性'
}

export function validateParserRules(rules: SiteParserRules | undefined): ParserRuleErrors {
  if (!rules) return { parser_rules: '缺少解析规则，请切回跟随内置或创建自定义规则' }
  const errors: ParserRuleErrors = {}
  validateSelector('search.item_selector', rules.search.item_selector, errors, true)
  validateSelector('search.empty_selector', rules.search.empty_selector, errors)
  validateSelector('search.ready_selector', rules.search.ready_selector, errors)
  validateSelector('search.magnet_available_selector', rules.search.magnet_available_selector, errors)
  validateSelector('detail.magnets.item_selector', rules.detail.magnets.item_selector, errors)
  const searchValueKeys: Array<keyof Pick<SiteParserRules['search'], 'detail_url' | 'title' | 'code' | 'date' | 'rating' | 'cover'>> = [
    'detail_url',
    'title',
    'code',
    'date',
    'rating',
    'cover',
  ]
  searchValueKeys.forEach((key) => validateValueRule(`search.${key}`, rules.search[key], errors))
  ;([...DETAIL_SCALAR_FIELDS, ...DETAIL_RELATION_FIELDS]).forEach(({ key }) =>
    validateValueRule(`detail.fields.${key}`, rules.detail.fields[key], errors),
  )
  ;(['cover', 'backdrop', 'sample'] as const).forEach((key) =>
    validateValueRule(`detail.images.${key}`, rules.detail.images[key], errors),
  )
  ;(['uri', 'name', 'size', 'badges'] as const).forEach((key) =>
    validateValueRule(`detail.magnets.${key}`, rules.detail.magnets[key], errors),
  )
  return errors
}

export function siteParserErrorCount(site: SiteSettings): number {
  return (site.parser_rules_mode ?? 'inherit') === 'custom' ? Object.keys(validateParserRules(site.parser_rules)).length : 0
}

export function ParserRulesEditor({ site, onChange }: { site: SiteSettings; onChange: (site: SiteSettings) => void }) {
  const modeGroupName = useId()
  const mode = site.parser_rules_mode ?? 'inherit'
  const rules = site.parser_rules
  const errors = useMemo(() => (mode === 'custom' ? validateParserRules(rules) : {}), [mode, rules])
  const errorCount = Object.keys(errors).length

  function setMode(nextMode: 'inherit' | 'custom') {
    if (nextMode === 'inherit') {
      onChange({ ...site, parser_rules_mode: 'inherit' })
      return
    }
    if (!site.parser_rules) return
    onChange({
      ...site,
      parser_rules_mode: 'custom',
      parser_rules: site.parser_rules,
    })
  }

  function setRules(nextRules: SiteParserRules) {
    onChange({ ...site, parser_rules_mode: 'custom', parser_rules: nextRules })
  }

  return (
    <details className="parser-rules-editor">
      <summary>
        <span className="parser-rules-title">
          <Braces aria-hidden="true" />
          解析规则
        </span>
        <span className="parser-rules-summary">{mode === 'inherit' ? `${rules ? '跟随' : '等待载入'} ${site.parser_profile} 内置规则` : '使用站点自定义规则'}</span>
        <StatusBadge tone={errorCount ? 'danger' : mode === 'custom' ? 'info' : 'neutral'}>
          {errorCount ? `${errorCount} 项错误` : mode === 'custom' ? '自定义' : rules ? '跟随内置' : '待保存'}
        </StatusBadge>
        <ChevronRight className="parser-rules-chevron" aria-hidden="true" />
      </summary>

      <div className="parser-rules-content">
        <fieldset className="parser-mode-control">
          <legend>{site.name || site.id} 规则来源</legend>
          <label>
            <input type="radio" name={modeGroupName} checked={mode === 'inherit'} onChange={() => setMode('inherit')} />
            <span>跟随内置</span>
          </label>
          <label>
            <input
              type="radio"
              name={modeGroupName}
              checked={mode === 'custom'}
              disabled={!rules}
              title={rules ? undefined : '保存站点后才能基于当前解析器创建自定义规则'}
              onChange={() => setMode('custom')}
            />
            <span>站点自定义</span>
          </label>
        </fieldset>

        {mode === 'custom' ? (
          <Button
            type="button"
            size="small"
            variant="ghost"
            className="parser-restore-button"
            aria-label={`恢复 ${site.name || site.id} 的内置规则`}
            onClick={() => setMode('inherit')}
          >
            <RotateCcw aria-hidden="true" />
            恢复内置规则
          </Button>
        ) : null}

        {mode === 'inherit' ? (
          rules ? (
            <InlineNotice tone="warning" role="status">
              保存后将使用 {site.parser_profile} 内置规则替换当前规则；保存前可切回站点自定义撤销。
            </InlineNotice>
          ) : (
            <InlineNotice tone="warning" role="status">
              解析器或规则来源已变更。请先保存站点，由服务端载入 {site.parser_profile} 的内置规则后再自定义。
            </InlineNotice>
          )
        ) : !rules ? (
          <InlineNotice tone="danger" role="alert">缺少解析规则，请切回跟随内置后重试。</InlineNotice>
        ) : (
          <CustomRulesForm siteName={site.name || site.id} rules={rules} errors={errors} onChange={setRules} />
        )}
      </div>
    </details>
  )
}

function CustomRulesForm({
  siteName,
  rules,
  errors,
  onChange,
}: {
  siteName: string
  rules: SiteParserRules
  errors: ParserRuleErrors
  onChange: (rules: SiteParserRules) => void
}) {
  const searchHeadingId = useId()
  const detailHeadingId = useId()
  const imagesHeadingId = useId()
  const magnetsHeadingId = useId()
  const setSearch = <K extends keyof SiteParserRules['search']>(key: K, value: SiteParserRules['search'][K]) =>
    onChange({ ...rules, search: { ...rules.search, [key]: value } })
  const setDetailField = (key: keyof SiteParserRules['detail']['fields'], value: ParserValueRule) =>
    onChange({ ...rules, detail: { ...rules.detail, fields: { ...rules.detail.fields, [key]: value } } })
  const setImage = (key: keyof SiteParserRules['detail']['images'], value: ParserValueRule) =>
    onChange({ ...rules, detail: { ...rules.detail, images: { ...rules.detail.images, [key]: value } } })
  const setMagnet = <K extends keyof SiteParserRules['detail']['magnets']>(key: K, value: SiteParserRules['detail']['magnets'][K]) =>
    onChange({ ...rules, detail: { ...rules.detail, magnets: { ...rules.detail.magnets, [key]: value } } })

  return (
    <div className="parser-rule-groups">
      <section className="parser-rule-section" aria-labelledby={searchHeadingId}>
        <header>
          <h3 id={searchHeadingId}>搜索结果</h3>
          <span>卡片边界、页面状态与摘要字段</span>
        </header>
        <div className="parser-selector-grid">
          <SelectorField label="结果卡片" contextLabel={`${siteName} 搜索结果卡片`} value={rules.search.item_selector} required error={errors['search.item_selector']} onChange={(value) => setSearch('item_selector', value)} />
          <SelectorField label="页面就绪" contextLabel={`${siteName} 搜索页面就绪`} value={rules.search.ready_selector} error={errors['search.ready_selector']} onChange={(value) => setSearch('ready_selector', value)} />
          <SelectorField label="空结果" contextLabel={`${siteName} 搜索空结果`} value={rules.search.empty_selector} error={errors['search.empty_selector']} onChange={(value) => setSearch('empty_selector', value)} />
          <SelectorField label="存在磁链" contextLabel={`${siteName} 搜索存在磁链`} value={rules.search.magnet_available_selector} error={errors['search.magnet_available_selector']} onChange={(value) => setSearch('magnet_available_selector', value)} />
          <Field label="默认磁链状态">
            <select aria-label={`${siteName} 搜索默认磁链状态`} value={rules.search.default_magnet_hint} onChange={(event) => setSearch('default_magnet_hint', event.target.value as SiteParserRules['search']['default_magnet_hint'])}>
              <option value="unknown">未知</option>
              <option value="available">可用</option>
              <option value="unavailable">不可用</option>
            </select>
          </Field>
        </div>
        <div className="parser-value-list" aria-label="搜索字段规则">
          <ValueRuleEditor label="详情链接" contextLabel={`${siteName} 搜索详情链接`} path="search.detail_url" rule={rules.search.detail_url} errors={errors} onChange={(value) => setSearch('detail_url', value)} />
          <ValueRuleEditor label="标题" contextLabel={`${siteName} 搜索标题`} path="search.title" rule={rules.search.title} errors={errors} onChange={(value) => setSearch('title', value)} />
          <ValueRuleEditor label="番号" contextLabel={`${siteName} 搜索番号`} path="search.code" rule={rules.search.code} errors={errors} onChange={(value) => setSearch('code', value)} />
          <ValueRuleEditor label="发行日期" contextLabel={`${siteName} 搜索发行日期`} path="search.date" rule={rules.search.date} errors={errors} onChange={(value) => setSearch('date', value)} />
          <ValueRuleEditor label="评分" contextLabel={`${siteName} 搜索评分`} path="search.rating" rule={rules.search.rating} errors={errors} onChange={(value) => setSearch('rating', value)} />
          <ValueRuleEditor label="封面" contextLabel={`${siteName} 搜索封面`} path="search.cover" rule={rules.search.cover} errors={errors} onChange={(value) => setSearch('cover', value)} />
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={detailHeadingId}>
        <header>
          <h3 id={detailHeadingId}>详情字段</h3>
          <span>基础字段取一个匹配，关联字段合并全部匹配</span>
        </header>
        <div className="parser-value-list" aria-label="详情字段规则">
          {DETAIL_SCALAR_FIELDS.map(({ key, label }) => (
            <ValueRuleEditor key={key} label={label} contextLabel={`${siteName} 详情${label}`} path={`detail.fields.${key}`} rule={rules.detail.fields[key]} errors={errors} onChange={(value) => setDetailField(key, value)} />
          ))}
          {DETAIL_RELATION_FIELDS.map(({ key, label }) => (
            <ValueRuleEditor key={key} label={label} contextLabel={`${siteName} 详情${label}`} path={`detail.fields.${key}`} rule={rules.detail.fields[key]} errors={errors} collectAll onChange={(value) => setDetailField(key, value)} />
          ))}
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={imagesHeadingId}>
        <header>
          <h3 id={imagesHeadingId}>图片</h3>
          <span>合并全部匹配并由媒体校验筛选</span>
        </header>
        <div className="parser-value-list" aria-label="图片规则">
          <ValueRuleEditor label="封面" contextLabel={`${siteName} 图片封面`} path="detail.images.cover" rule={rules.detail.images.cover} errors={errors} collectAll onChange={(value) => setImage('cover', value)} />
          <ValueRuleEditor label="背景图" contextLabel={`${siteName} 图片背景图`} path="detail.images.backdrop" rule={rules.detail.images.backdrop} errors={errors} collectAll onChange={(value) => setImage('backdrop', value)} />
          <ValueRuleEditor label="样品图" contextLabel={`${siteName} 图片样品图`} path="detail.images.sample" rule={rules.detail.images.sample} errors={errors} collectAll onChange={(value) => setImage('sample', value)} />
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={magnetsHeadingId}>
        <header>
          <h3 id={magnetsHeadingId}>磁链</h3>
          <span>每个磁链条目独立提取并按 info hash 合并</span>
        </header>
        <div className="parser-selector-grid parser-magnet-selector">
          <SelectorField label="磁链条目" contextLabel={`${siteName} 磁链条目`} value={rules.detail.magnets.item_selector} error={errors['detail.magnets.item_selector']} onChange={(value) => setMagnet('item_selector', value)} />
        </div>
        <div className="parser-value-list" aria-label="磁链字段规则">
          <ValueRuleEditor label="磁链地址" contextLabel={`${siteName} 磁链地址`} path="detail.magnets.uri" rule={rules.detail.magnets.uri} errors={errors} onChange={(value) => setMagnet('uri', value)} />
          <ValueRuleEditor label="名称" contextLabel={`${siteName} 磁链名称`} path="detail.magnets.name" rule={rules.detail.magnets.name} errors={errors} onChange={(value) => setMagnet('name', value)} />
          <ValueRuleEditor label="大小" contextLabel={`${siteName} 磁链大小`} path="detail.magnets.size" rule={rules.detail.magnets.size} errors={errors} onChange={(value) => setMagnet('size', value)} />
          <ValueRuleEditor label="标记" contextLabel={`${siteName} 磁链标记`} path="detail.magnets.badges" rule={rules.detail.magnets.badges} errors={errors} collectAll onChange={(value) => setMagnet('badges', value)} />
        </div>
      </section>
    </div>
  )
}

function SelectorField({
  label,
  contextLabel,
  value,
  error,
  required = false,
  onChange,
}: {
  label: string
  contextLabel: string
  value: string
  error?: string
  required?: boolean
  onChange: (value: string) => void
}) {
  const errorId = useId()
  return (
    <Field label={label} hint={required ? '用于确定每条搜索结果的边界' : '留空时使用解析器回退'} error={error} errorId={error ? errorId : undefined}>
      <input
        aria-label={`${contextLabel}选择器`}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        required={required}
        value={value}
        maxLength={240}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
      />
    </Field>
  )
}

function ValueRuleEditor({
  label,
  contextLabel,
  path,
  rule,
  errors,
  collectAll = false,
  onChange,
}: {
  label: string
  contextLabel: string
  path: string
  rule: ParserValueRule
  errors: ParserRuleErrors
  collectAll?: boolean
  onChange: (rule: ParserValueRule) => void
}) {
  const errorBaseId = useId()
  const selectorError = errors[`${path}.selector`]
  const attributesError = errors[`${path}.attributes`]
  const indexError = errors[`${path}.index`]
  const selectorErrorId = `${errorBaseId}-selector`
  const attributesErrorId = `${errorBaseId}-attributes`
  const indexErrorId = `${errorBaseId}-index`
  const hasError = Boolean(selectorError || attributesError || indexError)
  return (
    <div className={`parser-value-row ${hasError ? 'has-error' : ''}`}>
      <label className="parser-value-selector">
        <span>{label}</span>
        <input
          aria-label={`${contextLabel}选择器`}
          value={rule.selector}
          onChange={(event) => onChange({ ...rule, selector: event.target.value })}
          aria-invalid={Boolean(selectorError)}
          aria-describedby={selectorError ? selectorErrorId : undefined}
          spellCheck={false}
          maxLength={240}
        />
        {selectorError ? <span className="parser-control-error" id={selectorErrorId} role="alert">{selectorError}</span> : null}
      </label>
      <AttributeOrderEditor
        label={contextLabel}
        attributes={rule.attributes}
        error={attributesError}
        errorId={attributesErrorId}
        onChange={(attributes) => onChange({ ...rule, attributes })}
      />
      {collectAll ? (
        <span className="parser-match-mode">全部匹配</span>
      ) : (
        <label className="parser-index-field">
          <span>匹配位置</span>
          <input
            type="number"
            min={0}
            max={49}
            step={1}
            value={rule.index ?? ''}
            placeholder="0"
            aria-label={`${contextLabel}匹配位置`}
            aria-invalid={Boolean(indexError)}
            aria-describedby={indexError ? indexErrorId : undefined}
            onChange={(event) => onChange({ ...rule, index: event.target.value === '' ? undefined : Number(event.target.value) })}
          />
          {indexError ? <span className="parser-control-error" id={indexErrorId} role="alert">{indexError}</span> : null}
        </label>
      )}
    </div>
  )
}

function AttributeOrderEditor({
  label,
  attributes,
  error,
  errorId,
  onChange,
}: {
  label: string
  attributes: ParserRuleAttribute[]
  error?: string
  errorId: string
  onChange: (attributes: ParserRuleAttribute[]) => void
}) {
  const available = ATTRIBUTE_OPTIONS.filter((option) => !attributes.includes(option.value))
  const atLimit = attributes.length >= 6

  function move(index: number, offset: -1 | 1) {
    const target = index + offset
    if (target < 0 || target >= attributes.length) return
    const next = [...attributes]
    ;[next[index], next[target]] = [next[target], next[index]]
    onChange(next)
  }

  return (
    <div className="parser-attribute-field">
      <details className="parser-attribute-editor">
        <summary
          aria-label={`${label}读取来源：${attributes.length ? attributes.join('、') : '未设置'}`}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? errorId : undefined}
        >
          <span>读取来源</span>
          <strong>{attributes.length ? attributes.join(' → ') : '未设置'}</strong>
          <ChevronRight aria-hidden="true" />
        </summary>
        <div className="parser-attribute-content">
          {attributes.length ? (
            <ol aria-label={`${label}读取来源顺序`}>
              {attributes.map((attribute, index) => (
                <li key={attribute}>
                  <select
                    aria-label={`${label}读取来源 ${index + 1}`}
                    value={attribute}
                    onChange={(event) => {
                      const next = [...attributes]
                      next[index] = event.target.value as ParserRuleAttribute
                      onChange(next)
                    }}
                  >
                    {ATTRIBUTE_OPTIONS.filter((option) => option.value === attribute || !attributes.includes(option.value)).map((option) => (
                      <option value={option.value} key={option.value}>{option.label}</option>
                    ))}
                  </select>
                  <IconButton label={`上移${label}的${ATTRIBUTE_LABELS[attribute]}`} size="small" disabled={index === 0} onClick={() => move(index, -1)}>
                    <ArrowUp aria-hidden="true" />
                  </IconButton>
                  <IconButton label={`下移${label}的${ATTRIBUTE_LABELS[attribute]}`} size="small" disabled={index === attributes.length - 1} onClick={() => move(index, 1)}>
                    <ArrowDown aria-hidden="true" />
                  </IconButton>
                  <IconButton label={`删除${label}的${ATTRIBUTE_LABELS[attribute]}`} size="small" className="danger-icon" onClick={() => onChange(attributes.filter((_item, itemIndex) => itemIndex !== index))}>
                    <Trash2 aria-hidden="true" />
                  </IconButton>
                </li>
              ))}
            </ol>
          ) : <span className="parser-attribute-empty">没有读取来源</span>}
          <Button
            type="button"
            aria-label={`为${label}添加读取来源`}
            size="small"
            variant="ghost"
            disabled={atLimit || !available.length}
            onClick={() => available[0] && onChange([...attributes, available[0].value])}
          >
            <Plus aria-hidden="true" />
            添加来源
          </Button>
        </div>
      </details>
      {error ? <span className="parser-control-error" id={errorId} role="alert">{error}</span> : null}
    </div>
  )
}
