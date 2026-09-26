import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type SetStateAction } from 'react'

import { api } from './api'
import type { AppSettings, SettingsSnapshot } from '../types'

export function useSettingsDraft(onInitialize?: (settings: AppSettings) => void) {
  const queryClient = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const [draft, setDraftState] = useState<AppSettings | null>(null)
  const [loadedRevision, setLoadedRevision] = useState('')
  const editRevision = useRef(0)

  useEffect(() => {
    if (settings.data && !draft) {
      setDraftState(structuredClone(settings.data.settings))
      setLoadedRevision(settings.data.revision)
      onInitialize?.(settings.data.settings)
    }
  }, [draft, settings.data, onInitialize])

  function setDraft(value: SetStateAction<AppSettings | null>) {
    editRevision.current += 1
    setDraftState(value)
  }

  function acceptSaved(snapshot: SettingsSnapshot, submittedRevision: number): boolean {
    queryClient.setQueryData(['settings'], snapshot)
    setLoadedRevision(snapshot.revision)
    if (editRevision.current !== submittedRevision) return false
    setDraftState(structuredClone(snapshot.settings))
    return true
  }

  async function reloadSaved(): Promise<AppSettings> {
    const revision = editRevision.current
    const snapshot = await api.settings()
    queryClient.setQueryData(['settings'], snapshot)
    if (editRevision.current !== revision) throw new Error('载入期间又有新的修改，已保留当前草稿，请重新载入')
    setLoadedRevision(snapshot.revision)
    setDraft(structuredClone(snapshot.settings))
    return snapshot.settings
  }

  return { settings, draft, setDraft, loadedRevision, editRevision, acceptSaved, reloadSaved }
}
