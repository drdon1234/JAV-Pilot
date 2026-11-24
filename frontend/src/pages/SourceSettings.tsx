import { useMutation, useQuery } from '@tanstack/react-query'
import { Database, Puzzle } from 'lucide-react'
import { requestJson } from '../lib/api'
import { browserImportScript } from '../lib/browserImport'
import { saveBlob } from '../lib/saveBlob'
import { t } from '../lib/i18n'
import { useToast } from '../components/ToastProvider'
import { Button, Field, InlineNotice, SkeletonRows } from '../components/ui'

export function SourceSettings() {
  const q = useQuery({
    queryKey: ['custom-source'],
    queryFn: () =>
      requestJson<{ url: string; fields: Record<string, string> }>('/api/metadata/custom-source'),
  })
  const toast = useToast()
  const save = useMutation({
    mutationFn: (value: { url: string; fields: string }) =>
      requestJson('/api/metadata/custom-source', {
        method: 'POST',
        body: JSON.stringify({ url: value.url, fields: JSON.parse(value.fields) }),
      }),
    onSuccess: () => {
      void q.refetch()
      toast.push(t('已保存'), 'success')
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  })
  function install() {
    const contents = browserImportScript(location.origin)
    saveBlob(new Blob([contents], { type: 'text/javascript' }), 'jav-pilot.user.js')
  }
  return (
    <>
      <div className="settings-section-header">
        <div><Database aria-hidden="true" /><h2>{t('自定义资料源')}</h2></div>
      </div>
      {q.isPending ? <SkeletonRows /> : q.isError ? (
        <InlineNotice tone="danger">{q.error.message}</InlineNotice>
      ) : (
        <form
          className="settings-form-grid"
          onSubmit={(event) => {
            event.preventDefault()
            const form = new FormData(event.currentTarget)
            save.mutate({ url: String(form.get('url')), fields: String(form.get('fields')) })
          }}
        >
          <Field className="field-span-2" label={t('公开 HTTPS 接口，番号用 {code} 表示')}>
            <input name="url" type="url" spellCheck={false} defaultValue={q.data.url} />
          </Field>
          <Field className="field-span-2" label={t('字段与 JSON 路径')}>
            <textarea name="fields" spellCheck={false} rows={5} defaultValue={JSON.stringify(q.data.fields, null, 2)} />
          </Field>
          <div className="form-footer field-span-full">
            <Button variant="primary" type="submit" disabled={save.isPending}>{t('保存')}</Button>
          </div>
        </form>
      )}
      <div className="settings-section-header source-browser-install">
        <div><Puzzle aria-hidden="true" /><h2>{t('浏览器入库')}</h2></div>
        <Button size="small" onClick={install}>{t('下载用户脚本')}</Button>
      </div>
    </>
  )
}
