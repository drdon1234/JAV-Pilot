import { useState } from 'react'

import { ApiError } from '../lib/api'
import { Button, InlineNotice } from './ui'
import { t } from '../lib/i18n'

export function SettingsSaveError({ error, onReload }: { error: Error | null; onReload: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [reloadError, setReloadError] = useState('')
  if (!error) return null

  async function reload() {
    setReloading(true)
    setReloadError('')
    try {
      await onReload()
      setConfirming(false)
    } catch (failure) {
      setReloadError((failure as Error).message)
    } finally {
      setReloading(false)
    }
  }

  return (
    <InlineNotice tone="danger" role="alert">
      <span>{reloadError || error.message}</span>
      {error instanceof ApiError && error.status === 409 ? (
        confirming ? (
          <>
            <span>{t('重新载入会放弃本页未保存的修改。')}</span>
            <Button type="button" size="small" disabled={reloading} onClick={() => void reload()}>
              {reloading ? t('载入中') : t('放弃修改并载入')}
            </Button>
            <Button type="button" size="small" variant="ghost" disabled={reloading} onClick={() => setConfirming(false)}>{t('取消')}</Button>
          </>
        ) : <Button type="button" size="small" onClick={() => setConfirming(true)}>{t('载入最新设置')}</Button>
      ) : null}
    </InlineNotice>
  )
}
