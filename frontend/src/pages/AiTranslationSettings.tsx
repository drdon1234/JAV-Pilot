import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Save } from 'lucide-react'
import { useEffect, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, Field, InlineNotice, SkeletonRows, Toggle } from '../components/ui'
import { useAiTranslationConfig } from '../lib/aiTranslation'
import { api } from '../lib/api'
import { serviceErrorMessage } from '../lib/presentation'
import type { AiTranslationConfig, AiTranslationConfigUpdate, AiTranslationProvider } from '../types'

type Draft = Omit<AiTranslationConfig, 'api_key_configured'>

function draftFrom(config: AiTranslationConfig): Draft {
  return {
    provider: config.provider,
    base_url: config.base_url,
    model: config.model,
    api_version: config.api_version,
    allow_private_network: config.allow_private_network,
    instructions: config.instructions,
    daily_limit: config.daily_limit,
  }
}

function isLocalUrl(value: string): boolean {
  try {
    const host = new URL(value).hostname.toLowerCase()
    return host === 'localhost'
      || !host.includes('.')
      || /\.(localhost|local|lan|home|internal)$/.test(host)
      || /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|\[?f[cd])/.test(host)
      || host === 'host.docker.internal'
  } catch {
    return false
  }
}

/**
 * AI 翻译 settings. Kept apart from the ordinary translation switches: the
 * ordinary translation may run automatically, AI translation only on a button.
 */
export function AiTranslationSettings() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const snapshot = useAiTranslationConfig()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [testResult, setTestResult] = useState<{ tone: 'success' | 'danger'; text: string } | null>(null)

  useEffect(() => {
    if (snapshot.data && draft === null) setDraft(draftFrom(snapshot.data.config))
  }, [snapshot.data, draft])

  const ready = draft !== null
  useEffect(() => {
    // “前往默认参数设置” links here from the AI 翻译 buttons.
    if (ready && globalThis.location?.hash === '#ai-translation') {
      document.getElementById('ai-translation')?.scrollIntoView({ block: 'start' })
    }
  }, [ready])

  const providers = snapshot.data?.providers ?? []
  const provider: AiTranslationProvider | undefined = providers.find((item) => item.id === draft?.provider) ?? providers[0]

  function update(next: Partial<Draft>) {
    setDraft((current) => current ? { ...current, ...next } : current)
    setTestResult(null)
  }

  function submission(): AiTranslationConfigUpdate | null {
    if (!draft) return null
    const value: AiTranslationConfigUpdate = { ...draft }
    if (apiKey.trim()) value.api_key = apiKey.trim()
    else if (clearKey) value.api_key = ''
    return value
  }

  const save = useMutation({
    mutationFn: (value: AiTranslationConfigUpdate) => api.saveAiTranslationConfig(value),
    onSuccess: (next) => {
      queryClient.setQueryData(['ai-translation-config'], next)
      setDraft(draftFrom(next.config))
      setApiKey('')
      setClearKey(false)
      toast.push('AI 翻译设置已保存', 'success')
    },
    onError: (error) => toast.push(serviceErrorMessage(error, 'AI 翻译设置保存失败'), 'error'),
  })
  const test = useMutation({
    mutationFn: (value: AiTranslationConfigUpdate) => api.testAiTranslation(value),
    onSuccess: (result) => setTestResult({ tone: 'success', text: `试译成功（${(result.elapsed_ms / 1000).toFixed(1)} 秒）：${result.translation}` }),
    onError: (error) => setTestResult({ tone: 'danger', text: serviceErrorMessage(error, '试译失败，请检查设置') }),
  })

  if (snapshot.isLoading || !draft || !provider) {
    return snapshot.isError
      ? <InlineNotice tone="warning">暂时无法读取 AI 翻译设置，请刷新页面重试。</InlineNotice>
      : <SkeletonRows count={3} />
  }

  const saved = snapshot.data?.config
  const keyConfigured = Boolean(saved?.api_key_configured) && !clearKey
  const dirty = Boolean(apiKey.trim()) || clearKey || (saved ? JSON.stringify(draftFrom(saved)) !== JSON.stringify(draft) : false)
  const effectiveBase = draft.base_url.trim() || provider.default_base_url
  const needsVersion = provider.protocol === 'azure_openai' || provider.protocol === 'anthropic'
  const localEndpoint = effectiveBase ? isLocalUrl(effectiveBase) : false
  const busy = save.isPending || test.isPending

  return (
    <div className="ai-translation-settings">
      <p className="ai-translation-intro">
        AI 翻译只在你点击搜索结果、作品详情或排行榜上的“AI 翻译”按钮时调用，译文显示在标题和普通翻译的下方，不会替换它们。
        标题会发送给你配置的 AI 服务，按服务商规则计费。
      </p>
      <div className="settings-form-grid">
        <Field label="服务商">
          <select
            value={draft.provider}
            onChange={(event) => {
              const next = providers.find((item) => item.id === event.target.value)
              update({
                provider: event.target.value,
                api_version: '',
                allow_private_network: next?.protocol === 'ollama' ? true : draft.allow_private_network,
              })
            }}
          >
            {providers.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </Field>
        <Field
          label="Base URL"
          hint={provider.default_base_url ? `留空使用默认地址 ${provider.default_base_url}` : provider.protocol === 'azure_openai' ? '填写 Azure 资源地址，如 https://<资源名>.openai.azure.com' : '必填，填写服务的 API 地址（通常以 /v1 结尾）'}
        >
          <input
            type="url"
            value={draft.base_url}
            placeholder={provider.default_base_url || '请输入接口地址'}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => update({ base_url: event.target.value })}
          />
        </Field>
        <Field label="模型" hint={provider.protocol === 'azure_openai' ? '填写 Azure 中的部署名称' : '填写服务商提供的模型 ID'}>
          <input
            value={draft.model}
            placeholder="请输入模型名称"
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => update({ model: event.target.value })}
          />
        </Field>
        <Field
          label="API Key"
          hint={keyConfigured ? '已保存；留空保持不变，输入新值即可替换' : provider.requires_api_key ? '只保存在服务器数据目录，页面不会再显示' : '可选；本地服务通常不需要'}
        >
          <input
            type="password"
            value={apiKey}
            placeholder={keyConfigured ? '已保存，留空保持不变' : provider.requires_api_key ? '请输入 API Key' : '可选'}
            autoComplete="new-password"
            spellCheck={false}
            onChange={(event) => {
              setApiKey(event.target.value)
              if (event.target.value) setClearKey(false)
              setTestResult(null)
            }}
          />
        </Field>
        {needsVersion ? (
          <Field label="API 版本" hint={`留空使用 ${provider.default_api_version}`}>
            <input
              value={draft.api_version}
              placeholder={provider.default_api_version}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => update({ api_version: event.target.value })}
            />
          </Field>
        ) : null}
        <Field label="每日请求上限" hint={`0 表示不限制；每 20 条标题约计 1 次请求，已缓存的译文不计。今日已请求 ${snapshot.data?.usage_today ?? 0} 次`}>
          <input
            type="number"
            min={0}
            max={100000}
            value={draft.daily_limit}
            onChange={(event) => update({ daily_limit: Math.min(100000, Math.max(0, Math.trunc(Number(event.target.value) || 0))) })}
          />
        </Field>
      </div>
      <Field label="附加要求" hint="可选，会追加到翻译指令中，最多 500 字">
        <textarea
          rows={2}
          maxLength={500}
          value={draft.instructions}
          placeholder="可选：对译文风格或用词的额外要求"
          onChange={(event) => update({ instructions: event.target.value })}
        />
      </Field>
      <div className="workflow-defaults-toggles">
        <Toggle
          label="允许本机或局域网地址"
          checked={draft.allow_private_network}
          onChange={(event) => update({ allow_private_network: event.target.checked })}
        />
        <span>
          用于 Ollama、LM Studio 等运行在本机或局域网的服务。以 Docker 部署时，容器里的 localhost 不是宿主机，
          请改用 http://host.docker.internal:端口 或宿主机的局域网 IP。公网服务必须使用 HTTPS。
        </span>
      </div>
      {localEndpoint && !draft.allow_private_network ? (
        <InlineNotice tone="warning">当前地址是本机或局域网地址，需要开启“允许本机或局域网地址”。</InlineNotice>
      ) : null}
      {snapshot.data && !snapshot.data.configured && !dirty ? (
        <InlineNotice tone="info">尚未配置完整（缺少 {snapshot.data.missing.join('、')}），“AI 翻译”按钮会提示前往这里设置。</InlineNotice>
      ) : null}
      {testResult ? <InlineNotice tone={testResult.tone} role="status">{testResult.text}</InlineNotice> : null}
      <div className="ai-translation-actions">
        {keyConfigured ? (
          <Button type="button" variant="ghost" size="small" disabled={busy} onClick={() => { setClearKey(true); setApiKey('') }}>
            清除已保存的 API Key
          </Button>
        ) : clearKey ? <span className="ai-translation-key-note">保存后将删除已保存的 API Key</span> : null}
        <Button type="button" disabled={busy} onClick={() => { const value = submission(); if (value) test.mutate(value) }}>
          <FlaskConical className={test.isPending ? 'spin' : ''} aria-hidden="true" />
          {test.isPending ? '正在试译' : '试译一句'}
        </Button>
        <Button type="button" variant="primary" disabled={!dirty || busy} onClick={() => { const value = submission(); if (value) save.mutate(value) }}>
          <Save aria-hidden="true" />
          {save.isPending ? '正在保存' : '保存 AI 翻译设置'}
        </Button>
      </div>
    </div>
  )
}

