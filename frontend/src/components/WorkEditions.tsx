import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { codeKey as keyOf } from '../lib/localPlayback'
import { t } from '../lib/i18n'
import { webDownloadVariantLabel } from '../lib/webDownloads'

export function WorkEditions({ code }: { code: string }) {
  const query = useQuery({
    queryKey: ['work-editions', code],
    queryFn: () => api.mediaLibrary({ query: code, limit: 500 }),
    enabled: Boolean(code),
    staleTime: 30000,
  })
  const editions =
    query.data?.items.filter((entry) => keyOf(entry.code || entry.code_key || '') === keyOf(code)) || []
  if (!editions.length) return null
  return (
    <section className="library-detail-files">
      <h2>{t('本地版本')}</h2>
      {editions.map((entry) => (
        <div className="library-detail-file" key={entry.entry_id}>
          <div>
            <Link to={'/library?entry=' + entry.entry_id}>
              {entry.variant ? webDownloadVariantLabel(entry.variant) : t('本地媒体')}
            </Link>
            <span> {entry.quality_height ? entry.quality_height + 'p' : t('画质未知')}</span>
            {entry.media_files.map((file, i) => (
              <div key={file.identity}>
                {entry.presence === 'present' ? (
                  <Link to={'/player?entry=' + entry.entry_id + '&file=' + i + '&identity=' + file.identity}>
                    {file.relative_path}
                  </Link>
                ) : (
                  <span>{file.relative_path}</span>
                )}{' '}
                {[file.part_key, file.quality_height && file.quality_height + 'p']
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  )
}
