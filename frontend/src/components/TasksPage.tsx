import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { taskSources, taskPage, type TaskSource } from '../lib/taskSummary'
import { api } from '../lib/api'
import { t } from '../lib/i18n'
import { PageHeader, Button, SkeletonRows } from './ui'

function Queue({source, code}: {source:TaskSource; code:string}) {
  const [page, setPage] = useState(0)
  const task = useQuery({queryKey:['task-summary',source,page,code],queryFn:()=>taskPage(source,page,code),refetchInterval:15000,retry:false})
  return <section className="library-detail-files"><h2>{taskSources[source]}</h2>
    {task.isError ? <p role="alert">{task.error.message} <Button onClick={()=>void task.refetch()}>{t('重试')}</Button></p> : task.isPending ? <SkeletonRows count={1}/> : <>
      {!task.data.rows.length && <p>{t('暂无任务')}</p>}
      <ul>{task.data.rows.map(row=><li key={row.id}><Link to={row.href}>{row.title}</Link> · {row.status}</li>)}</ul>
      {page>0 && <Button onClick={()=>setPage(page-1)}>{t('上一页')}</Button>}
      {task.data.more && <Button onClick={()=>setPage(page+1)}>{t('下一页')}</Button>}
    </>}
  </section>
}
export function TasksPage() {
  const [params,set] = useSearchParams()
  const selected=params.get('source') || '', code=params.get('q') || '', probe=params.get('probe') || ''
  const result=useQuery({queryKey:['task-probe',probe],queryFn:()=>api.magnetProbe(probe),enabled:Boolean(probe),refetchInterval:15000,retry:false})
  return <div className="page"><PageHeader title={t('任务总览')}/>
    {probe ? <section><Link to="/tasks">{t('返回任务总览')}</Link><h2>{t('磁链探测')}</h2>{result.isError ? <p role="alert">{result.error.message}</p> : result.data ? <ul>{result.data.items.map((item,i)=><li key={i}>{item.catalog_code || item.info_hash} · {item.state}</li>)}</ul> : <SkeletonRows/>}</section> : <>
      <form className="watch-inline-actions" onSubmit={event=>{event.preventDefault();const form=new FormData(event.currentTarget);set({q:String(form.get('q')||'').trim(),source:String(form.get('source')||'')})}}>
        <input key={code} name="q" defaultValue={code} aria-label={t('番号')} placeholder={t('番号')} maxLength={64}/>
        <select key={selected} name="source" defaultValue={selected} aria-label={t('来源')}><option value="">{t('全部来源')}</option>
          {Object.entries(taskSources).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select><Button type="submit">{t('筛选')}</Button>
      </form>
      {(Object.keys(taskSources) as TaskSource[]).filter(source=>(!selected || selected===source) && (!code || source!=='probe')).map(source=><Queue key={source+':'+code} source={source} code={code}/>)}
    </>}
  </div>
}
