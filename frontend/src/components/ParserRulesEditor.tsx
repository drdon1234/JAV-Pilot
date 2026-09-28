import { ArrowDown, ArrowUp, Braces, ChevronRight, Plus, RotateCcw, Trash2 } from 'lucide-react'
import { useId, useMemo } from 'react'

import type { ParserRuleAttribute, ParserValueRule, SiteParserRules, SiteSettings } from '../types'
import { Button, Field, IconButton, InlineNotice, StatusBadge } from './ui'
import { t } from '../lib/i18n'

const ATTRIBUTE_OPTIONS: ReadonlyArray<{ value: ParserRuleAttribute; label: string }> = [
  { value: 'text', label: t('文本内容') },
  { value: 'href', label: t('链接 href') },
  { value: 'src', label: t('资源 src') },
  { value: 'title', label: t('标题 title') },
  { value: 'alt', label: t('替代文本 alt') },
  { value: 'datetime', label: t('日期时间 datetime') },
  { value: 'content', label: t('内容 content') },
  { value: 'poster', label: t('封面 poster') },
  { value: 'data-src', label: t('延迟资源 data-src') },
  { value: 'data-href', label: t('延迟链接 data-href') },
  { value: 'data-url', label: t('数据链接 data-url') },
  { value: 'data-original', label: t('原图 data-original') },
  { value: 'data-lazy-src', label: t('延迟原图 data-lazy-src') },
]

const ATTRIBUTE_LABELS = Object.fromEntries(ATTRIBUTE_OPTIONS.map((option) => [option.value, option.label])) as Record<ParserRuleAttribute, string>

const DETAIL_SCALAR_FIELDS: ReadonlyArray<{ key: keyof SiteParserRules['detail']['fields']; label: string }> = [
  { key: 'title', label: t('标题') },
  { key: 'original_title', label: t('原始标题') },
  { key: 'release_date', label: t('发行日期') },
  { key: 'duration', label: t('时长') },
  { key: 'rating', label: t('评分') },
]

const DETAIL_RELATION_FIELDS: ReadonlyArray<{ key: keyof SiteParserRules['detail']['fields']; label: string }> = [
  { key: 'maker', label: t('片商') },
  { key: 'publisher', label: t('发行商') },
  { key: 'series', label: t('系列') },
  { key: 'director', label: t('导演') },
  { key: 'actor', label: t('演员') },
  { key: 'tag', label: t('标签') },
]

export type ParserRuleErrors = Record<string, string>

function validateValueRule(path: string, rule: ParserValueRule, errors: ParserRuleErrors) {
  validateSelector(`${path}.selector`, rule.selector, errors)
  if (!rule.attributes.length || rule.attributes.length > 6) errors[`${path}.attributes`] = t('读取来源必须保留 1 到 6 项')
  if (rule.index !== undefined && (!Number.isInteger(rule.index) || rule.index < 0 || rule.index > 49)) errors[`${path}.index`] = t('匹配位置必须是 0 到 49 的整数')
}

function validateSelector(path: string, selector: string, errors: ParserRuleErrors, required = false) {
  const value = selector.trim()
  if (!value) {
    if (required) errors[path] = t('结果卡片选择器不能为空')
    return
  }
  if (value.length > 240) {
    errors[path] = t('选择器最多 240 个字符')
    return
  }
  if (value.split(',').length > 8) errors[path] = t('选择器最多包含 8 组')
  const componentCount = Array.from(value).filter((character) => [' ', '>', '+', '~'].includes(character)).length + 1
  if (componentCount > 24) errors[path] = t('选择器最多包含 24 个组成部分')
  if (/:(?:has|contains|-soup-contains)\s*\(|javascript:/i.test(value)) errors[path] = t('选择器包含不支持的特性')
}

export function validateParserRules(rules: SiteParserRules | undefined): ParserRuleErrors {
  if (!rules) return { parser_rules: t('缺少解析规则，请切回跟随内置或创建自定义规则') }
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
          {t('解析规则')}
        </span>
        <span className="parser-rules-summary">{mode === 'inherit' ? t('{value} {parser_profile} 内置规则', { value: rules ? t('跟随') : t('等待载入'), parser_profile: site.parser_profile }) : t('使用站点自定义规则')}</span>
        <StatusBadge tone={errorCount ? 'danger' : mode === 'custom' ? 'info' : 'neutral'}>
          {errorCount ? t('{errorCount} 项错误', { errorCount }) : mode === 'custom' ? t('自定义') : rules ? t('跟随内置') : t('待保存')}
        </StatusBadge>
        <ChevronRight className="parser-rules-chevron" aria-hidden="true" />
      </summary>

      <div className="parser-rules-content">
        <fieldset className="parser-mode-control">
          <legend>{t('{value} 规则来源', { value: site.name || site.id })}</legend>
          <label>
            <input type="radio" name={modeGroupName} checked={mode === 'inherit'} onChange={() => setMode('inherit')} />
            <span>{t('跟随内置')}</span>
          </label>
          <label>
            <input
              type="radio"
              name={modeGroupName}
              checked={mode === 'custom'}
              disabled={!rules}
              title={rules ? undefined : t('保存站点后才能基于当前解析器创建自定义规则')}
              onChange={() => setMode('custom')}
            />
            <span>{t('站点自定义')}</span>
          </label>
        </fieldset>

        {mode === 'custom' ? (
          <Button
            type="button"
            size="small"
            variant="ghost"
            className="parser-restore-button"
            aria-label={t('恢复 {value} 的内置规则', { value: site.name || site.id })}
            onClick={() => setMode('inherit')}
          >
            <RotateCcw aria-hidden="true" />
            {t('恢复内置规则')}
          </Button>
        ) : null}

        {mode === 'inherit' ? (
          rules ? (
            <InlineNotice tone="warning" role="status">
              {t('保存后将使用 {parser_profile} 内置规则替换当前规则；保存前可切回站点自定义撤销。', { parser_profile: site.parser_profile })}
            </InlineNotice>
          ) : (
            <InlineNotice tone="warning" role="status">
              {t('解析器或规则来源已变更。请先保存站点，由服务端载入 {parser_profile} 的内置规则后再自定义。', { parser_profile: site.parser_profile })}
            </InlineNotice>
          )
        ) : !rules ? (
          <InlineNotice tone="danger" role="alert">{t('缺少解析规则，请切回跟随内置后重试。')}</InlineNotice>
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
          <h3 id={searchHeadingId}>{t('搜索结果')}</h3>
          <span>{t('卡片边界、页面状态与摘要字段')}</span>
        </header>
        <div className="parser-selector-grid">
          <SelectorField label={t('结果卡片')} contextLabel={t('{siteName} 搜索结果卡片', { siteName })} value={rules.search.item_selector} required error={errors['search.item_selector']} onChange={(value) => setSearch('item_selector', value)} />
          <SelectorField label={t('页面就绪')} contextLabel={t('{siteName} 搜索页面就绪', { siteName })} value={rules.search.ready_selector} error={errors['search.ready_selector']} onChange={(value) => setSearch('ready_selector', value)} />
          <SelectorField label={t('空结果')} contextLabel={t('{siteName} 搜索空结果', { siteName })} value={rules.search.empty_selector} error={errors['search.empty_selector']} onChange={(value) => setSearch('empty_selector', value)} />
          <SelectorField label={t('存在磁链')} contextLabel={t('{siteName} 搜索存在磁链', { siteName })} value={rules.search.magnet_available_selector} error={errors['search.magnet_available_selector']} onChange={(value) => setSearch('magnet_available_selector', value)} />
          <Field label={t('默认磁链状态')}>
            <select aria-label={t('{siteName} 搜索默认磁链状态', { siteName })} value={rules.search.default_magnet_hint} onChange={(event) => setSearch('default_magnet_hint', event.target.value as SiteParserRules['search']['default_magnet_hint'])}>
              <option value="unknown">{t('未知')}</option>
              <option value="available">{t('可用')}</option>
              <option value="unavailable">{t('不可用')}</option>
            </select>
          </Field>
        </div>
        <div className="parser-value-list" role="group" aria-label={t('搜索字段规则')}>
          <ValueRuleEditor label={t('详情链接')} contextLabel={t('{siteName} 搜索详情链接', { siteName })} path="search.detail_url" rule={rules.search.detail_url} errors={errors} onChange={(value) => setSearch('detail_url', value)} />
          <ValueRuleEditor label={t('标题')} contextLabel={t('{siteName} 搜索标题', { siteName })} path="search.title" rule={rules.search.title} errors={errors} onChange={(value) => setSearch('title', value)} />
          <ValueRuleEditor label={t('番号')} contextLabel={t('{siteName} 搜索番号', { siteName })} path="search.code" rule={rules.search.code} errors={errors} onChange={(value) => setSearch('code', value)} />
          <ValueRuleEditor label={t('发行日期')} contextLabel={t('{siteName} 搜索发行日期', { siteName })} path="search.date" rule={rules.search.date} errors={errors} onChange={(value) => setSearch('date', value)} />
          <ValueRuleEditor label={t('评分')} contextLabel={t('{siteName} 搜索评分', { siteName })} path="search.rating" rule={rules.search.rating} errors={errors} onChange={(value) => setSearch('rating', value)} />
          <ValueRuleEditor label={t('封面')} contextLabel={t('{siteName} 搜索封面', { siteName })} path="search.cover" rule={rules.search.cover} errors={errors} onChange={(value) => setSearch('cover', value)} />
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={detailHeadingId}>
        <header>
          <h3 id={detailHeadingId}>{t('详情字段')}</h3>
          <span>{t('基础字段取一个匹配，关联字段合并全部匹配')}</span>
        </header>
        <div className="parser-value-list" role="group" aria-label={t('详情字段规则')}>
          {DETAIL_SCALAR_FIELDS.map(({ key, label }) => (
            <ValueRuleEditor key={key} label={label} contextLabel={t('{siteName} 详情{label}', { siteName, label })} path={`detail.fields.${key}`} rule={rules.detail.fields[key]} errors={errors} onChange={(value) => setDetailField(key, value)} />
          ))}
          {DETAIL_RELATION_FIELDS.map(({ key, label }) => (
            <ValueRuleEditor key={key} label={label} contextLabel={t('{siteName} 详情{label}', { siteName, label })} path={`detail.fields.${key}`} rule={rules.detail.fields[key]} errors={errors} collectAll onChange={(value) => setDetailField(key, value)} />
          ))}
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={imagesHeadingId}>
        <header>
          <h3 id={imagesHeadingId}>{t('图片')}</h3>
          <span>{t('合并全部匹配并由媒体校验筛选')}</span>
        </header>
        <div className="parser-value-list" role="group" aria-label={t('图片规则')}>
          <ValueRuleEditor label={t('封面')} contextLabel={t('{siteName} 图片封面', { siteName })} path="detail.images.cover" rule={rules.detail.images.cover} errors={errors} collectAll onChange={(value) => setImage('cover', value)} />
          <ValueRuleEditor label={t('背景图')} contextLabel={t('{siteName} 图片背景图', { siteName })} path="detail.images.backdrop" rule={rules.detail.images.backdrop} errors={errors} collectAll onChange={(value) => setImage('backdrop', value)} />
          <ValueRuleEditor label={t('样品图')} contextLabel={t('{siteName} 图片样品图', { siteName })} path="detail.images.sample" rule={rules.detail.images.sample} errors={errors} collectAll onChange={(value) => setImage('sample', value)} />
        </div>
      </section>

      <section className="parser-rule-section" aria-labelledby={magnetsHeadingId}>
        <header>
          <h3 id={magnetsHeadingId}>{t('磁链')}</h3>
          <span>{t('每个磁链条目独立提取并按 info hash 合并')}</span>
        </header>
        <div className="parser-selector-grid parser-magnet-selector">
          <SelectorField label={t('磁链条目')} contextLabel={t('{siteName} 磁链条目', { siteName })} value={rules.detail.magnets.item_selector} error={errors['detail.magnets.item_selector']} onChange={(value) => setMagnet('item_selector', value)} />
        </div>
        <div className="parser-value-list" role="group" aria-label={t('磁链字段规则')}>
          <ValueRuleEditor label={t('磁链地址')} contextLabel={t('{siteName} 磁链地址', { siteName })} path="detail.magnets.uri" rule={rules.detail.magnets.uri} errors={errors} onChange={(value) => setMagnet('uri', value)} />
          <ValueRuleEditor label={t('名称')} contextLabel={t('{siteName} 磁链名称', { siteName })} path="detail.magnets.name" rule={rules.detail.magnets.name} errors={errors} onChange={(value) => setMagnet('name', value)} />
          <ValueRuleEditor label={t('大小')} contextLabel={t('{siteName} 磁链大小', { siteName })} path="detail.magnets.size" rule={rules.detail.magnets.size} errors={errors} onChange={(value) => setMagnet('size', value)} />
          <ValueRuleEditor label={t('标记')} contextLabel={t('{siteName} 磁链标记', { siteName })} path="detail.magnets.badges" rule={rules.detail.magnets.badges} errors={errors} collectAll onChange={(value) => setMagnet('badges', value)} />
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
    <Field label={label} hint={required ? t('用于确定每条搜索结果的边界') : t('留空时使用解析器回退')} error={error} errorId={error ? errorId : undefined}>
      <input
        aria-label={t('{contextLabel}选择器', { contextLabel })}
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
          aria-label={t('{contextLabel}选择器', { contextLabel })}
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
        <span className="parser-match-mode">{t('全部匹配')}</span>
      ) : (
        <label className="parser-index-field">
          <span>{t('匹配位置')}</span>
          <input
            type="number"
            min={0}
            max={49}
            step={1}
            value={rule.index ?? ''}
            placeholder="0"
            aria-label={t('{contextLabel}匹配位置', { contextLabel })}
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
          aria-label={t('{label}读取来源：{value}', { label, value: attributes.length ? attributes.join(t('、')) : t('未设置') })}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? errorId : undefined}
        >
          <span>{t('读取来源')}</span>
          <strong>{attributes.length ? attributes.join(' → ') : t('未设置')}</strong>
          <ChevronRight aria-hidden="true" />
        </summary>
        <div className="parser-attribute-content">
          {attributes.length ? (
            <ol aria-label={t('{label}读取来源顺序', { label })}>
              {attributes.map((attribute, index) => (
                <li key={attribute}>
                  <select
                    aria-label={t('{label}读取来源 {value}', { label, value: index + 1 })}
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
                  <IconButton label={t('上移{label}的{value}', { label, value: ATTRIBUTE_LABELS[attribute] })} size="small" disabled={index === 0} onClick={() => move(index, -1)}>
                    <ArrowUp aria-hidden="true" />
                  </IconButton>
                  <IconButton label={t('下移{label}的{value}', { label, value: ATTRIBUTE_LABELS[attribute] })} size="small" disabled={index === attributes.length - 1} onClick={() => move(index, 1)}>
                    <ArrowDown aria-hidden="true" />
                  </IconButton>
                  <IconButton label={t('删除{label}的{value}', { label, value: ATTRIBUTE_LABELS[attribute] })} size="small" className="danger-icon" onClick={() => onChange(attributes.filter((_item, itemIndex) => itemIndex !== index))}>
                    <Trash2 aria-hidden="true" />
                  </IconButton>
                </li>
              ))}
            </ol>
          ) : <span className="parser-attribute-empty">{t('没有读取来源')}</span>}
          <Button
            type="button"
            aria-label={t('为{label}添加读取来源', { label })}
            size="small"
            variant="ghost"
            disabled={atLimit || !available.length}
            onClick={() => available[0] && onChange([...attributes, available[0].value])}
          >
            <Plus aria-hidden="true" />
            {t('添加来源')}
          </Button>
        </div>
      </details>
      {error ? <span className="parser-control-error" id={errorId} role="alert">{error}</span> : null}
    </div>
  )
}
