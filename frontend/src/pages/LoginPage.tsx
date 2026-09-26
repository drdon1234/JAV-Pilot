import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, KeyRound, LogIn } from 'lucide-react'
import { type FormEvent, useEffect, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'

import { ThemeButton } from '../components/ThemeProvider'
import { Button, Field, InlineNotice } from '../components/ui'
import { api } from '../lib/api'

export function LoginPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const status = useQuery({ queryKey: ['auth'], queryFn: api.authStatus, retry: 0 })
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')

  useEffect(() => {
    if (status.data?.username) setUsername(status.data.username)
  }, [status.data])

  const login = useMutation({
    mutationFn: () => api.login(username.trim(), password),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['auth'] })
      navigate('/search', { replace: true })
    },
  })

  if (status.data && (!status.data.enabled || status.data.authenticated)) {
    return <Navigate to="/search" replace />
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    login.mutate()
  }

  return (
    <main className="login-page">
      <div className="login-theme"><ThemeButton /></div>
      <section className="login-panel" aria-labelledby="login-title">
        <header className="login-brand">
          <div className="brand-mark" aria-hidden="true"><Clapperboard /></div>
          <div>
            <h1 id="login-title">JAV Pilot</h1>
            <p>媒体搜索与下载控制台</p>
          </div>
        </header>
        {status.isError ? <InlineNotice tone="danger">{(status.error as Error).message}</InlineNotice> : null}
        {login.isError ? <InlineNotice tone="danger" role="alert">{(login.error as Error).message}</InlineNotice> : null}
        <form onSubmit={submit}>
          <Field label="账号">
            <div className="input-with-icon"><KeyRound aria-hidden="true" /><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required autoFocus /></div>
          </Field>
          <Field label="密码">
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />
          </Field>
          <Button type="submit" variant="primary" disabled={login.isPending || status.isLoading}>
            <LogIn aria-hidden="true" />{login.isPending ? '登录中' : '登录'}
          </Button>
        </form>
      </section>
    </main>
  )
}
