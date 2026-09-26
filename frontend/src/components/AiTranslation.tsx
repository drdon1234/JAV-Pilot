import { Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useAiTranslationConfig, type AiTranslationState } from '../lib/aiTranslation'
import { Button } from './ui'

import '../styles/aiTranslation.css'

/**
 * The only way to request AI translations. Results appear under the title
 * (and under the ordinary translation when that is on), never in place of it.
 */
export function AiTranslateButton({ state, appearance = 'button' }: { state: AiTranslationState; appearance?: 'button' | 'link' }) {
  const config = useAiTranslationConfig()
  const [needsSetup, setNeedsSetup] = useState(false)
  const label = state.running
    ? 'AI 翻译中…'
    : state.missing > 0
      ? state.missing < state.total ? `AI 翻译其余 ${state.missing} 条` : 'AI 翻译'
      : state.hidden ? '显示 AI 译文' : '隐藏 AI 译文'

  function activate() {
    if (state.running) return
    if (state.missing > 0) {
      if (config.data && !config.data.configured) {
        setNeedsSetup(true)
        return
      }
      setNeedsSetup(false)
      void state.run()
      return
    }
    state.setHidden(!state.hidden)
  }

  const content = (
    <>
      <Sparkles className={state.running ? 'spin' : ''} aria-hidden="true" />
      {label}
    </>
  )
  return (
    <span className="ai-translate-control">
      {appearance === 'link' ? (
        <button type="button" className="ai-translate-link" onClick={activate} disabled={state.running || !state.total} aria-busy={state.running}>
          {content}
        </button>
      ) : (
        <Button type="button" size="small" variant="ghost" onClick={activate} disabled={state.running || !state.total} aria-busy={state.running}>
          {content}
        </Button>
      )}
      {needsSetup ? (
        <span className="ai-translate-message" role="status">
          尚未配置 AI 翻译，<Link to="/workflow-defaults#ai-translation">前往默认参数设置</Link>
        </span>
      ) : state.message ? (
        <span className={`ai-translate-message ai-translate-message-${state.message.tone}`} role="status">{state.message.text}</span>
      ) : null}
    </span>
  )
}

/** One AI translation line, placed below the title and any ordinary translation. */
export function AiTranslationLine({ text, className = '' }: { text: string | null; className?: string }) {
  if (!text) return null
  return (
    <small className={`ai-translation-line ${className}`.trim()}>
      <span className="ai-translation-badge" title="AI 译文">AI</span>
      <span>{text}</span>
    </small>
  )
}
