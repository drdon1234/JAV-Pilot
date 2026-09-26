import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Languages, RefreshCw, Search, Star } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, Toggle } from '../components/ui'
import { api, ApiError, coverImageUrl } from '../lib/api'
import { loadSearchPreferences } from '../lib/searchPreferences'
import { SEARCH_CAPABILITIES, SEARCH_PARSER_PROFILES } from '../lib/sources'
import { useTranslationPreferences, useTranslations } from '../lib/translation'
import { useAiTranslations } from '../lib/aiTranslation'
import { AiTranslateButton, AiTranslationLine } from '../components/AiTranslation'
import type { RankingPeriod, RankingType } from '../types'
import { QuickWebDownload } from './QuickWebDownload'
import { StableImage } from './WorkUi'

import '../styles/rankings.css'

const PERIODS: Array<{ value: RankingPeriod; label: string }> = [
  { value: 'daily', label: '日榜' },
  { value: 'weekly', label: '周榜' },
  { value: 'monthly', label: '月榜' },
]
const TYPES: Array<{ value: RankingType; label: string }> = [
  { value: 'censored', label: '有码' },
  { value: 'uncensored', label: '无码' },
  { value: 'western', label: '欧美' },
  { value: 'fc2', label: 'FC2' },
]

/** 排行榜: JavDB's daily, weekly and monthly lists with search and download shortcuts. */
export function RankingsPage() {
  const [period, setPeriod] = useState<RankingPeriod>('daily')
  const [type, setType] = useState<RankingType>('censored')
  const [refreshToken, setRefreshToken] = useState(0)
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const translation = useTranslationPreferences(settings.data?.settings.workflow_defaults?.translation)
  const ranking = useQuery({
    queryKey: ['rankings', period, type, refreshToken],
    queryFn: () => api.rankings(period, type, refreshToken > 0),
    staleTime: 10 * 60_000,
    retry: false,
  })
  const webService = useQuery({
    queryKey: ['web-download-service'],
    queryFn: () => api.webDownloads({ limit: 1 }),
    retry: false,
    staleTime: 15_000,
  })
  const webConfigured = webService.data?.configured === true && webService.data.enabled !== false
  const items = ranking.data?.items ?? []
  const titles = useTranslations(items.map((item) => item.title), translation.preferences.enabled)
  const aiTitles = useAiTranslations(items.map((item) => item.title))
  const sites = settings.data?.settings.sites ?? []
  const searchSources = sites
    .filter((site) => site.enabled && SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability)) && SEARCH_PARSER_PROFILES.has(site.parser_profile))
    .map((site) => site.id)
  const loginRequired = ranking.error instanceof ApiError && ranking.error.code === 'login_required'
  const sourceDisabled = ranking.error instanceof ApiError && ranking.error.code === 'source_disabled'

  function searchHref(code: string): string {
    const params = new URLSearchParams({
      q: code,
      source: searchSources.join(','),
      result_limit: '20',
      magnets: loadSearchPreferences().fetchMagnets ? '1' : '0',
      sort: 'relevance',
      match: 'exact',
      kind: 'code',
      site_mode: 'all',
      page: '1',
      page_size: '20',
    })
    return `/results?${params.toString()}`
  }

  return (
    <div className="page rankings-page">
      <PageHeader
        title="排行榜"
        description="来自 JavDB 的日榜、周榜与月榜"
        actions={(
          <IconButton label="刷新榜单" onClick={() => setRefreshToken((value) => value + 1)} disabled={ranking.isFetching}>
            <RefreshCw className={ranking.isFetching ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        )}
      />
      <div className="rankings-toolbar">
        <div className="segmented-control" aria-label="时间范围">
          {PERIODS.map((option) => (
            <button type="button" className={period === option.value ? 'active' : ''} aria-pressed={period === option.value} onClick={() => setPeriod(option.value)} key={option.value}>{option.label}</button>
          ))}
        </div>
        <div className="segmented-control" aria-label="榜单类别">
          {TYPES.map((option) => (
            <button type="button" className={type === option.value ? 'active' : ''} aria-pressed={type === option.value} onClick={() => setType(option.value)} key={option.value}>{option.label}</button>
          ))}
        </div>
        <Toggle label="翻译标题" checked={translation.preferences.enabled} onChange={(event) => translation.update({ enabled: event.target.checked })} />
        {titles.loading ? <span className="rankings-translating"><Languages className="spin" aria-hidden="true" />正在翻译</span> : null}
        <AiTranslateButton state={aiTitles} />
      </div>
      {loginRequired ? (
        <InlineNotice tone="info" role="status">
          JavDB 只公开有码榜单；无码、欧美和 FC2 榜单需要登录后的 JavDB 会话（可在部署环境中配置 JavDB Cookie）。
        </InlineNotice>
      ) : null}
      {sourceDisabled ? (
        <InlineNotice tone="warning" role="status">
          排行榜来自 JavDB，请先在 <Link to="/sites">站点</Link> 页面启用 JavDB。
        </InlineNotice>
      ) : null}
      {ranking.isError && !loginRequired && !sourceDisabled ? (
        <EmptyState
          title="暂时无法获取榜单"
          description="JavDB 可能正在进行访问验证，请稍后刷新。"
          action={<Button onClick={() => setRefreshToken((value) => value + 1)}>重新获取</Button>}
        />
      ) : null}
      {ranking.isLoading ? <SkeletonRows count={6} /> : null}
      {items.length ? (
        <ol className="rankings-list" aria-label="榜单作品">
          {items.map((item) => {
            const translated = translation.preferences.enabled ? titles.translate(item.title) : null
            const cover = item.cover ? coverImageUrl(item.source_id, item.cover, sites) : ''
            return (
              <li className="ranking-row" key={`${item.rank}-${item.code}`}>
                <span className="ranking-rank" aria-label={`第 ${item.rank} 名`}>{item.rank}</span>
                <div className="ranking-cover">
                  <StableImage src={cover} alt={`${item.code ?? item.title} 封面`} referrerPolicy="no-referrer" />
                </div>
                <div className="ranking-main">
                  <strong>{item.code ?? '未知番号'}</strong>
                  <p>{translated || item.title}</p>
                  {translated && translation.preferences.showOriginal ? <small>{item.title}</small> : null}
                  <AiTranslationLine text={aiTitles.translate(item.title)} />
                  <span className="ranking-meta">
                    {item.release_date || '日期未知'}
                    {item.rating !== null ? <><Star aria-hidden="true" /> {item.rating}{item.votes !== null ? `（${item.votes} 人）` : ''}</> : null}
                  </span>
                </div>
                <div className="ranking-actions">
                  {item.code ? (
                    <Link className="button button-secondary button-small" to={searchHref(item.code)}>
                      <Search aria-hidden="true" />
                      搜索
                    </Link>
                  ) : null}
                  {item.code && webConfigured ? <QuickWebDownload code={item.code} /> : null}
                  {item.detail_url ? (
                    <a className="button button-ghost button-small" href={item.detail_url} target="_blank" rel="noopener noreferrer" aria-label={`在 JavDB 打开 ${item.code ?? ''}`}>
                      <ExternalLink aria-hidden="true" />
                    </a>
                  ) : null}
                </div>
              </li>
            )
          })}
        </ol>
      ) : null}
    </div>
  )
}
