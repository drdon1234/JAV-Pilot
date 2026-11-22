import type { MediaMetadataStatus } from '../types'
import { t } from './i18n'

export const metadataJobLabels: Record<MediaMetadataStatus, string> = {
  waiting_media: t('等待媒体'),
  queued: t('排队中'),
  running: t('补全中'),
  retry: t('等待重试'),
  completed: t('已完成'),
  failed: t('失败'),
}
