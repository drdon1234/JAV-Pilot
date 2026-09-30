import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, ListChecks, RefreshCw, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { SubtitleCandidatesDialog, type SubtitleDialogTarget } from '../components/SubtitleCandidatesDialog'
import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import { t } from '../lib/i18n'
import { serviceErrorMessage } from '../lib/presentation'
import {
  PROVIDER_LABELS,
  SUBTITLE_ACTIVE_STATUSES,
  SUBTITLE_STATUS_FILTERS,
  subtitleReasonLabel,
  subtitleScriptLabel,
  subtitleStatusLabel,
  subtitleStatusTone,
} from '../lib/subtitles'
import type { SubtitleActionRequest, SubtitleJob, SubtitleSummary } from '../types'

import '../styles/subtitles.css'

const PAGE_SIZE = 50
const SUMMARY_FIELDS: Array<[keyof SubtitleSummary, string]> = [
  ['waiting', t('等待中')],
  ['running', t('获取中')],
  ['completed', t('已完成')],
  ['not_found', t('未找到')],
  ['skipped', t('已跳过')],
  ['failed', t('失败')],
]

export function SubtitlesPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('all')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [dialog, setDialog] = useState<SubtitleDialogTarget | null>(null)

  const jobs = useQuery({
    queryKey: ['subtitles', filter, query, page],
    queryFn: () => api.subtitles({ filter, query: query || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    retry: false,
    refetchInterval: (state) => {
      const summary = state.state.data?.summary
      return summary && (summary.running > 0 || summary.waiting > 0) ? 3_000 : 15_000
    },
    refetchIntervalInBackground: false,
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['subtitles'] })
    void queryClient.invalidateQueries({ queryKey: ['media-library'] })
  }

  const batch = useMutation({
    mutationFn: () => api.subtitleAction({ action: 'batch_missing' }),
    onSuccess: (payload) => {
      const queued = payload.queued ?? 0
      toast.push(
        queued ? t('已为 {queued} 部作品加入字幕获取队列', { queued }) : t('没有需要获取字幕的作品'),
        queued ? 'success' : 'info',
      )
      refresh()
    },
    onError: (error) => toast.push(serviceErrorMessage(error, t('字幕操作未完成，请稍后重试')), 'error'),
  })

  const jobAction = useMutation({
    mutationFn: (request: SubtitleActionRequest) => api.subtitleAction(request),
    onSuccess: (_payload, request) => {
      toast.push(request.action === 'forget' ? t('已清除记录') : t('已加入字幕获取队列'), 'success')
      refresh()
    },
    onError: (error) => toast.push(serviceErrorMessage(error, t('字幕操作未完成，请稍后重试')), 'error'),
  })

  const payload = jobs.data
  const count = payload?.count ?? 0
  const pageCount = Math.max(1, Math.ceil(count / PAGE_SIZE))
  const disabled = payload?.enabled === false

  const submitFilter = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setQuery(queryInput.trim())
    setPage(0)
  }

  return (
    <div className="page subtitles-page">
      <PageHeader
        title={t('字幕')}
        description={t('为媒体库影片获取外挂中文字幕')}
        actions={(
          <IconButton label={t('刷新字幕任务')} onClick={() => void jobs.refetch()} disabled={jobs.isFetching}>
            <RefreshCw className={jobs.isFetching ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        )}
      />

      {disabled ? <InlineNotice tone="warning">{t('字幕功能已通过环境变量关闭。')}</InlineNotice> : null}

      <section className="subtitles-controls" aria-label={t('批量获取字幕')}>
        <Button type="button" variant="primary" onClick={() => batch.mutate()} disabled={batch.isPending || disabled}>
          <ListChecks aria-hidden="true" />
          {batch.isPending ? t('正在加入队列') : t('为缺字幕的作品获取')}
        </Button>
        <span>
          {t('检查媒体库中还没有字幕的影片（每次最多 500 部），逐部查询已启用的字幕来源。分段影片和中文字幕版本会被跳过。')}
        </span>
        <Link className="subtitles-settings-link" to="/workflow-defaults#subtitles">{t('字幕设置')}</Link>
      </section>

      {payload?.summary ? (
        <dl className="subtitles-summary">
          {SUMMARY_FIELDS.map(([key, label]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{payload.summary[key]}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <section className="subtitles-workspace" aria-label={t('字幕任务')}>
        <form className="subtitles-filters" onSubmit={submitFilter}>
          <label>
            <span>{t('状态')}</span>
            <select value={filter} onChange={(event) => { setFilter(event.target.value); setPage(0) }}>
              {SUBTITLE_STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('番号')}</span>
            <input
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
              placeholder={t('搜索番号')}
              maxLength={40}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <Button type="submit">{t('筛选')}</Button>
        </form>

        {jobs.isLoading ? <SkeletonRows /> : null}
        {jobs.isError ? (
          <EmptyState
            title={t('无法读取字幕任务')}
            description={serviceErrorMessage(jobs.error, t('请稍后重试'))}
            action={<Button type="button" onClick={() => void jobs.refetch()}>{t('重新检测')}</Button>}
          />
        ) : null}
        {payload && payload.jobs.length === 0 ? (
          <EmptyState
            title={t('暂无字幕任务')}
            description={t('影片完成元数据补全后会自动获取字幕，也可以点击上方按钮批量获取。')}
          />
        ) : null}
        {payload?.jobs.length ? (
          <ul className="subtitles-list" aria-label={t('字幕任务')}>
            {payload.jobs.map((job) => (
              <SubtitleJobRow
                key={job.job_id}
                job={job}
                busy={jobAction.isPending}
                onOpen={() => setDialog({ kind: 'job', jobId: job.job_id, code: job.code })}
                onRetry={() => jobAction.mutate({ action: 'retry', job_id: job.job_id })}
                onForget={() => jobAction.mutate({ action: 'forget', job_id: job.job_id })}
              />
            ))}
          </ul>
        ) : null}

        {count > PAGE_SIZE ? (
          <nav className="history-pager" aria-label={t('字幕任务分页')}>
            <IconButton label={t('上一页')} size="small" onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0 || jobs.isFetching}>
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>{t('第 {page} / {pageCount} 页，共 {count} 部', { page: page + 1, pageCount, count })}</span>
            <IconButton label={t('下一页')} size="small" onClick={() => setPage(page + 1)} disabled={!payload?.has_more || jobs.isFetching}>
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </nav>
        ) : null}
      </section>

      <SubtitleCandidatesDialog target={dialog} onClose={() => setDialog(null)} />
    </div>
  )
}

function SubtitleJobRow({
  job,
  busy,
  onOpen,
  onRetry,
  onForget,
}: {
  job: SubtitleJob
  busy: boolean
  onOpen: () => void
  onRetry: () => void
  onForget: () => void
}) {
  const reason = subtitleReasonLabel(job.reason)
  const active = SUBTITLE_ACTIVE_STATUSES.has(job.status)
  const path = job.subtitle_path || job.relative_media_path
  return (
    <li className={`subtitles-row subtitles-status-${job.status}`}>
      <div className="subtitles-row-main">
        <div className="subtitles-row-heading">
          <strong>{job.code}</strong>
          <StatusBadge tone={subtitleStatusTone(job.status)}>{subtitleStatusLabel(job.status)}</StatusBadge>
          {job.selected_provider ? <StatusBadge>{PROVIDER_LABELS[job.selected_provider]}</StatusBadge> : null}
          {job.subtitle_script ? <StatusBadge>{subtitleScriptLabel(job.subtitle_script)}</StatusBadge> : null}
        </div>
        {reason ? <span className="subtitles-row-reason">{reason}</span> : null}
        <code title={path}>{path}</code>
      </div>
      <div className="subtitles-row-actions">
        <Button type="button" size="small" onClick={onOpen}>{t('候选')}</Button>
        <IconButton
          label={t('重新获取 {code} 的字幕', { code: job.code })}
          size="small"
          onClick={onRetry}
          disabled={busy || active}
        >
          <RotateCcw aria-hidden="true" />
        </IconButton>
        {job.reason === 'media_missing' ? (
          <IconButton label={t('清除 {code} 的记录', { code: job.code })} size="small" onClick={onForget} disabled={busy}>
            <Trash2 aria-hidden="true" />
          </IconButton>
        ) : null}
      </div>
    </li>
  )
}
