import { api, requestJson } from './api'
import { t } from './i18n'
import { subtitleStatusLabel } from './subtitles'
import { webDownloadStatusLabel } from './webDownloads'
import { metadataJobLabels } from './metadataStatus'
import type { TorrentStage } from '../types'

export const taskSources = {bt: t('BT 下载'), web: t('Web 下载'), metadata: t('元数据'), subtitles: t('字幕'), probe: t('磁链探测')}
export type TaskSource = keyof typeof taskSources
export const torrentStages: Record<TorrentStage, string> = {
  queued: t('排队中'), downloading: t('下载中'), checking: t('校验中'), paused: t('已暂停'), completed: t('已完成'), error: t('异常'),
}
export interface TaskSummary {id: string; title: string; status: string; href: string}
interface TaskPage { rows: TaskSummary[]; more: boolean }
export function taskHref(source: TaskSource, code: string) {
  const query = encodeURIComponent(code)
  if (source === 'metadata') return '/metadata?code='+query
  if (source === 'subtitles') return '/subtitles?q='+query
  return '/downloads?view='+source+'&q='+query
}
export async function taskPage(source: TaskSource, page: number, code: string): Promise<TaskPage> {
  const limit = 20, offset = page * limit
  const filter = {limit, offset, query: code}
  if (source === 'bt') {
    const data = await api.torrents('all', limit, offset, code)
    if (data.configured && !data.ok) throw new Error(data.error || t('读取失败'))
    return {more: !!data.has_more, rows: data.tasks.map(job => ({id:job.hash, title:job.name, status:torrentStages[job.stage], href:taskHref(source, job.catalog_codes[0] || '')}))}
  }
  if (source === 'web') {
    const data = await api.webDownloads(filter)
    return {more: !!data.has_more, rows:data.tasks.map(job => ({id:job.job_id,title:job.code,status:webDownloadStatusLabel(job.status),href:taskHref(source,job.code)}))}
  }
  if (source === 'metadata') {
    const data = await api.mediaMetadata(filter)
    return {more: !!data.has_more, rows:data.jobs.map(job => ({id:job.job_id,title:job.code,status:metadataJobLabels[job.status],href:taskHref(source,job.code)}))}
  }
  if (source === 'subtitles') {
    const data = await api.subtitles(filter)
    return {more: !!data.has_more, rows:data.jobs.map(job => ({id:job.job_id,title:job.code,status:subtitleStatusLabel(job.status),href:taskHref(source,job.code)}))}
  }
  const data = await requestJson<{items: {probe_id:string;status:string;progress:{resolved:number;total:number}}[]}>('/api/magnets/probe?view=summary')
  return {more:false, rows:data.items.map(job=>({id:job.probe_id,title:t('磁链探测')+' · '+job.progress.resolved+'/'+job.progress.total,status:webDownloadStatusLabel(job.status),href:'/tasks?probe='+encodeURIComponent(job.probe_id)}))}
}
