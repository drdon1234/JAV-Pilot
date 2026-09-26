import { useQueryClient } from '@tanstack/react-query'
import { Check, Download, LoaderCircle } from 'lucide-react'
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button } from '../components/ui'
import { api } from '../lib/api'
import { downloadHistorySummary } from '../lib/downloadHistory'
import { createWebDownloadIntentKey, WEB_DOWNLOAD_HISTORY_QUERY_ROOT } from '../lib/webDownloads'

type QuickState =
  | { phase: 'idle' | 'checking' | 'adding' | 'added' }
  | { phase: 'confirm'; summary: string }

/**
 * 从 Web 下载 for one work, directly from a search result. The download
 * history is checked first so a work that already exists is not queued again
 * without an explicit confirmation.
 */
export function QuickWebDownload({ code, disabled = false }: { code: string; disabled?: boolean }) {
  const toast = useToast()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [state, setState] = useState<QuickState>({ phase: 'idle' })
  const idempotencyKey = useRef('')

  async function add() {
    setState({ phase: 'adding' })
    idempotencyKey.current ||= createWebDownloadIntentKey()
    try {
      await api.addWebDownload(code, idempotencyKey.current)
      setState({ phase: 'added' })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      void queryClient.invalidateQueries({ queryKey: WEB_DOWNLOAD_HISTORY_QUERY_ROOT })
      toast.push(`${code} Web 下载已加入后台`, 'success')
    } catch (error) {
      setState({ phase: 'idle' })
      toast.push((error as Error).message, 'error')
    }
  }

  async function start() {
    setState({ phase: 'checking' })
    try {
      const lookup = await api.downloadHistoryLookup([code])
      const summary = downloadHistorySummary(lookup.items[0])
      if (summary) {
        setState({ phase: 'confirm', summary })
        return
      }
    } catch {
      // The history check is advisory; the Web queue still deduplicates jobs.
    }
    await add()
  }

  if (state.phase === 'added') {
    return (
      <Button type="button" size="small" variant="secondary" onClick={() => navigate('/downloads?view=web')}>
        <Check aria-hidden="true" />
        查看 Web 任务
      </Button>
    )
  }
  if (state.phase === 'confirm') {
    return (
      <div className="quick-web-confirm" role="group" aria-label={`${code} 下载确认`}>
        <span>{state.summary}，仍要下载？</span>
        <Button type="button" size="small" variant="primary" onClick={() => void add()}>仍要下载</Button>
        <Button type="button" size="small" variant="ghost" onClick={() => setState({ phase: 'idle' })}>取消</Button>
      </div>
    )
  }
  const busy = state.phase === 'checking' || state.phase === 'adding'
  return (
    <Button type="button" size="small" variant="secondary" disabled={disabled || busy} onClick={() => void start()} aria-busy={busy}>
      {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
      {state.phase === 'checking' ? '检查记录' : state.phase === 'adding' ? '正在添加' : '从 Web 下载'}
    </Button>
  )
}
