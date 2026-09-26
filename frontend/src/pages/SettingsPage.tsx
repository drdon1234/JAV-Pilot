import '../styles/settings.css'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, KeyRound, LogOut, RefreshCw, Save, ServerCog } from 'lucide-react'
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, Field, InlineNotice, PageHeader, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { clearAuthenticatedDetailPrefetchBatch } from '../lib/detailPrefetchSession'
import { serviceErrorMessage } from '../lib/presentation'
import { useSearchSessions } from '../lib/searchSessions'
import { NotificationSettingsPanel } from './NotificationSettingsPanel'

interface QbForm {
  url: string
  username: string
  password: string
  category: string
  save_path: string
  library_path: string
  app_library_path: string
  tags: string
  clearPassword: boolean
}

const emptyQbForm: QbForm = {
  url: '',
  username: '',
  password: '',
  category: 'jav',
  save_path: '/downloads/jav',
  library_path: '/media/JAV',
  app_library_path: '/media/JAV',
  tags: 'jav-pilot',
  clearPassword: false,
}

const settingsSections = [
  { id: 'qbittorrent-settings', label: '下载器', icon: ServerCog },
  { id: 'access-settings', label: '访问控制', icon: KeyRound },
  { id: 'notification-settings', label: '通知', icon: BellRing },
  { id: 'runtime-status', label: '运行状态', icon: ServerCog },
] as const

type SettingsSectionId = (typeof settingsSections)[number]['id']

function sectionFromHash(hash: string): SettingsSectionId {
  const candidate = hash.replace(/^#/, '')
  return settingsSections.some((section) => section.id === candidate) ? (candidate as SettingsSectionId) : 'qbittorrent-settings'
}

export function SettingsPage() {
  const toast = useToast()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const searchSessions = useSearchSessions()
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: api.runtime })
  const auth = useQuery({ queryKey: ['auth'], queryFn: api.authStatus })
  const downloader = useQuery({
    queryKey: ['downloader-status'],
    queryFn: api.downloaderStatus,
  })
  const [qbForm, setQbForm] = useState<QbForm>(emptyQbForm)
  const qbEditRevision = useRef(0)
  const qbDirty = useRef(false)
  function updateQbForm(value: QbForm) {
    qbEditRevision.current += 1
    qbDirty.current = true
    setQbForm(value)
  }
  const [passwordForm, setPasswordForm] = useState({
    current: '',
    next: '',
    confirm: '',
    username: 'admin',
  })
  const [loggingOut, setLoggingOut] = useState(false)

  useEffect(() => {
    const qb = runtime.data?.config.qbittorrent
    if (!qb || qbDirty.current) return
    setQbForm((current) => ({
      ...current,
      url: qb.url || '',
      username: qb.username || '',
      category: qb.category || 'jav',
      save_path: qb.save_path || '/downloads/jav',
      library_path: qb.library_path || '',
      app_library_path: qb.app_library_path || '/media/JAV',
      tags: qb.tags || 'jav-pilot',
      password: '',
      clearPassword: false,
    }))
  }, [runtime.data])
  useEffect(() => {
    if (auth.data?.username)
      setPasswordForm((current) => ({
        ...current,
        username: auth.data!.username,
      }))
  }, [auth.data])

  const saveQb = useMutation({
    mutationFn: ({ form }: { form: QbForm; revision: number }) =>
      api.saveQbittorrent({
        url: form.url.trim(),
        username: form.username.trim(),
        password: form.password,
        password_action: form.clearPassword ? 'clear' : form.password ? 'set' : 'keep',
        category: form.category.trim(),
        save_path: form.save_path.trim(),
        library_path: form.library_path.trim(),
        app_library_path: form.app_library_path.trim(),
        tags: form.tags.trim(),
      }),
    onSuccess: (_value, submission) => {
      const current = qbEditRevision.current === submission.revision
      if (current) qbDirty.current = false
      if (current) setQbForm((current) => ({
        ...current,
        password: '',
        clearPassword: false,
      }))
      toast.push(current ? 'qBittorrent 设置已保存' : 'qBittorrent 设置已保存，当前仍有未保存修改', 'success')
      void queryClient.invalidateQueries({ queryKey: ['runtime'] })
      void queryClient.invalidateQueries({ queryKey: ['downloader-status'] })
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const changePassword = useMutation({
    mutationFn: () =>
      api.changePassword({
        username: passwordForm.username.trim() || 'admin',
        current_password: passwordForm.current,
        new_password: passwordForm.next,
      }),
    onSuccess: (value) => {
      setPasswordForm((current) => ({
        ...current,
        current: '',
        next: '',
        confirm: '',
      }))
      queryClient.setQueryData(['auth'], { ...value.auth, authenticated: true })
      toast.push('WebUI 账号密码已更新', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  function submitPassword(event: FormEvent) {
    event.preventDefault()
    if (passwordForm.next.length < 12) {
      toast.push('新密码至少需要 12 个字符', 'error')
      return
    }
    if (passwordForm.next !== passwordForm.confirm) {
      toast.push('两次输入的新密码不一致', 'error')
      return
    }
    changePassword.mutate()
  }

  async function logout() {
    if (loggingOut) return
    setLoggingOut(true)
    try {
      const cleanupResults = await Promise.allSettled([
        searchSessions.clearSessions(),
        clearAuthenticatedDetailPrefetchBatch(),
      ])
      const cleanupFailure = cleanupResults.find((result) => result.status === 'rejected')
      if (cleanupFailure?.status === 'rejected') throw cleanupFailure.reason
      await api.logout()
      queryClient.clear()
      navigate('/login', { replace: true })
    } catch (error) {
      toast.push(`退出登录失败：${serviceErrorMessage(error, '无法结束当前登录会话，请稍后重试')}`, 'error')
    } finally {
      setLoggingOut(false)
    }
  }

  const activeSection = sectionFromHash(location.hash)

  function activateSection(sectionId: SettingsSectionId) {
    navigate(
      {
        pathname: location.pathname,
        search: location.search,
        hash: `#${sectionId}`,
      },
      { replace: true },
    )
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLAnchorElement>, sectionId: SettingsSectionId) {
    const currentIndex = settingsSections.findIndex((section) => section.id === sectionId)
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % settingsSections.length
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + settingsSections.length) % settingsSections.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = settingsSections.length - 1
    if (nextIndex === null) return

    event.preventDefault()
    const nextSection = settingsSections[nextIndex].id
    document.getElementById(`settings-tab-${nextSection}`)?.focus()
    activateSection(nextSection)
  }

  if (runtime.isLoading)
    return (
      <div className="page">
        <SkeletonRows count={5} />
      </div>
    )

  const qb = runtime.data?.config.qbittorrent
  const securityFindings = auth.data?.security.findings ?? []
  const criticalSecurityFindings = securityFindings.filter((finding) => finding.severity === 'critical').length
  return (
    <div className="page settings-page">
      <PageHeader
        title="系统设置"
        description="下载器、访问控制、通知与运行状态"
        actions={
          auth.data?.enabled ? (
            <Button variant="ghost" onClick={() => void logout()} disabled={loggingOut} aria-busy={loggingOut}>
              <LogOut aria-hidden="true" />
              {loggingOut ? '正在退出' : '退出登录'}
            </Button>
          ) : undefined
        }
      />

      {runtime.isError ? <InlineNotice tone="danger">{(runtime.error as Error).message}</InlineNotice> : null}

      <div className="settings-layout">
        <nav className="settings-index" aria-label="设置分类" role="tablist">
          {settingsSections.map(({ id, label, icon: Icon }) => {
            const selected = activeSection === id
            return (
              <Link
                replace
                to={{ pathname: location.pathname, search: location.search, hash: `#${id}` }}
                role="tab"
                id={`settings-tab-${id}`}
                aria-selected={selected}
                aria-controls={id}
                tabIndex={selected ? 0 : -1}
                onKeyDown={(event) => handleTabKeyDown(event, id)}
                key={id}
              >
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </Link>
            )
          })}
        </nav>

        <div className="settings-content">
          <section
            className="settings-section"
            id="qbittorrent-settings"
            role="tabpanel"
            aria-labelledby="settings-tab-qbittorrent-settings"
            hidden={activeSection !== 'qbittorrent-settings'}
          >
            <div className="settings-section-header">
              <div>
                <ServerCog aria-hidden="true" />
                <div>
                  <h2>qBittorrent</h2>
                  <span>Web API、隔离分类与下载目录</span>
                </div>
              </div>
              <div className="settings-section-actions">
                <StatusBadge tone={downloader.data?.ok ? 'success' : downloader.data?.configured ? 'warning' : 'neutral'}>
                  {downloader.data?.ok ? downloader.data.version || '在线' : downloader.data?.configured ? '连接异常' : '未配置'}
                </StatusBadge>
                <Button size="small" variant="ghost" onClick={() => void downloader.refetch()} disabled={downloader.isFetching}>
                  <RefreshCw className={downloader.isFetching ? 'spin' : ''} aria-hidden="true" />
                  检测连接
                </Button>
              </div>
            </div>
            {downloader.data?.error && downloader.data.configured ? <InlineNotice tone="warning">{downloader.data.error}</InlineNotice> : null}
            <form
              className="settings-form-grid"
              onSubmit={(event) => {
                event.preventDefault()
                saveQb.mutate({ form: { ...qbForm }, revision: qbEditRevision.current })
              }}
            >
              <Field label="Web API 地址" className="field-span-2">
                <input type="url" value={qbForm.url} onChange={(event) => updateQbForm({ ...qbForm, url: event.target.value })} placeholder="http://qbittorrent:8080" required />
              </Field>
              <Field label="账号">
                <input value={qbForm.username} onChange={(event) => updateQbForm({ ...qbForm, username: event.target.value })} autoComplete="username" />
              </Field>
              <Field label="密码" hint={qb?.has_password ? '已配置，留空保持不变' : undefined}>
                <input
                  type="password"
                  value={qbForm.password}
                  onChange={(event) =>
                    updateQbForm({
                      ...qbForm,
                      password: event.target.value,
                      clearPassword: false,
                    })
                  }
                  disabled={qbForm.clearPassword}
                  autoComplete="new-password"
                />
              </Field>
              <Field label="JAV 下载分类" hint="必填；所有新任务固定使用此分类">
                <input value={qbForm.category} onChange={(event) => updateQbForm({ ...qbForm, category: event.target.value })} required />
              </Field>
              <Field label="默认标签">
                <input value={qbForm.tags} onChange={(event) => updateQbForm({ ...qbForm, tags: event.target.value })} />
              </Field>
              <Field label="JAV 下载暂存目录" hint="必填；规则只能使用此目录或其子目录">
                <input value={qbForm.save_path} onChange={(event) => updateQbForm({ ...qbForm, save_path: event.target.value })} placeholder="/downloads/jav" required />
              </Field>
              <Field label="完成整理目录" hint="留空将停用完成后自动移动">
                <input value={qbForm.library_path} onChange={(event) => updateQbForm({ ...qbForm, library_path: event.target.value })} placeholder="/media/JAV" />
              </Field>
              <Field label="应用内媒体库挂载" hint="同一目录在 JAV Pilot 容器内的路径；通常为 /media/JAV">
                <input
                  value={qbForm.app_library_path}
                  onChange={(event) => updateQbForm({ ...qbForm, app_library_path: event.target.value })}
                  placeholder="/media/JAV"
                  required={Boolean(qbForm.library_path.trim())}
                />
              </Field>
              <div className="form-footer field-span-full">
                <Toggle
                  label="清空已保存的 qB 密码"
                  checked={qbForm.clearPassword}
                  onChange={(event) =>
                    updateQbForm({
                      ...qbForm,
                      clearPassword: event.target.checked,
                      password: '',
                    })
                  }
                />
                <Button type="submit" variant="primary" disabled={saveQb.isPending}>
                  <Save aria-hidden="true" />
                  {saveQb.isPending ? '保存中' : '保存下载器'}
                </Button>
              </div>
            </form>
          </section>

          <section
            className="settings-section"
            id="access-settings"
            role="tabpanel"
            aria-labelledby="settings-tab-access-settings"
            hidden={activeSection !== 'access-settings'}
          >
            <div className="settings-section-header">
              <div>
                <KeyRound aria-hidden="true" />
                <div>
                  <h2>WebUI 访问控制</h2>
                  <span>{auth.data?.enabled ? '已启用' : '当前未启用'}</span>
                </div>
              </div>
              <div className="settings-section-actions">
                <StatusBadge tone={securityFindings.length ? (criticalSecurityFindings ? 'danger' : 'warning') : 'success'}>
                  {securityFindings.length ? `${securityFindings.length} 项安全风险` : '安全基线通过'}
                </StatusBadge>
                <StatusBadge tone={auth.data?.configured ? 'success' : 'warning'}>{auth.data?.configured ? '密码已配置' : '密码未配置'}</StatusBadge>
              </div>
            </div>
            {securityFindings.map((finding) => (
              <InlineNotice key={finding.code} tone={finding.severity === 'critical' ? 'danger' : 'warning'}>
                <strong>{finding.message}</strong> {finding.remediation}
              </InlineNotice>
            ))}
            <form className="settings-form-grid" onSubmit={submitPassword}>
              <Field label="账号">
                <input
                  value={passwordForm.username}
                  onChange={(event) =>
                    setPasswordForm({
                      ...passwordForm,
                      username: event.target.value,
                    })
                  }
                  autoComplete="username"
                />
              </Field>
              <Field label="当前密码">
                <input
                  type="password"
                  value={passwordForm.current}
                  onChange={(event) =>
                    setPasswordForm({
                      ...passwordForm,
                      current: event.target.value,
                    })
                  }
                  autoComplete="current-password"
                />
              </Field>
              <Field label="新密码">
                <input
                  type="password"
                  value={passwordForm.next}
                  onChange={(event) =>
                    setPasswordForm({
                      ...passwordForm,
                      next: event.target.value,
                    })
                  }
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </Field>
              <Field label="确认新密码">
                <input
                  type="password"
                  value={passwordForm.confirm}
                  onChange={(event) =>
                    setPasswordForm({
                      ...passwordForm,
                      confirm: event.target.value,
                    })
                  }
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </Field>
              <div className="form-footer field-span-full">
                <span />
                <Button type="submit" variant="primary" disabled={changePassword.isPending}>
                  <Save aria-hidden="true" />
                  {changePassword.isPending ? '更新中' : '更新账号密码'}
                </Button>
              </div>
            </form>
          </section>

          <section
            className="settings-section notification-settings-section"
            id="notification-settings"
            role="tabpanel"
            aria-labelledby="settings-tab-notification-settings"
            hidden={activeSection !== 'notification-settings'}
          >
            <div className="settings-section-header">
              <div>
                <BellRing aria-hidden="true" />
                <div>
                  <h2>通知</h2>
                  <span>长任务、磁盘与站点异常</span>
                </div>
              </div>
            </div>
            <NotificationSettingsPanel active={activeSection === 'notification-settings'} />
          </section>

          <section
            className="settings-section runtime-section"
            id="runtime-status"
            role="tabpanel"
            aria-labelledby="settings-tab-runtime-status"
            hidden={activeSection !== 'runtime-status'}
          >
            <div className="settings-section-header">
              <div>
                <ServerCog aria-hidden="true" />
                <div>
                  <h2>运行状态</h2>
                  <span>只读</span>
                </div>
              </div>
            </div>
            <dl className="runtime-grid">
              <div>
                <dt>应用</dt>
                <dd>
                  {runtime.data?.app || 'jav-pilot'} {runtime.data?.version || ''}
                </dd>
              </div>
              <div>
                <dt>JavDB 抓取器</dt>
                <dd>{runtime.data?.javdb_fetcher || 'auto'}</dd>
              </div>
              <div>
                <dt>搜索缓存</dt>
                <dd>
                  {runtime.data?.cache.items ?? 0} / {runtime.data?.cache.max_items ?? 0}
                </dd>
              </div>
              <div>
                <dt>缓存 TTL</dt>
                <dd>{runtime.data?.cache.ttl_seconds ?? 0} 秒</dd>
              </div>
              <div>
                <dt>启用站点</dt>
                <dd>{runtime.data?.settings.sites.filter((site) => site.enabled).length ?? 0}</dd>
              </div>
              <div>
                <dt>qB 地址</dt>
                <dd>{qb?.url || '未配置'}</dd>
              </div>
            </dl>
          </section>
        </div>
      </div>
    </div>
  )
}
