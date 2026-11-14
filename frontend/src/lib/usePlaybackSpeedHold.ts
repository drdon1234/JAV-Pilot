import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

interface SpeedPress {
  source: string
  timer: number
  long: boolean
  element: HTMLVideoElement | null
  previousRate: number | null
  tap?: () => void
}

/** A temporary speed belongs to one press, including release outside the player. */
export function usePlaybackSpeedHold(video: RefObject<HTMLVideoElement | null>, enabled: boolean) {
  const press = useRef<SpeedPress | null>(null)
  const [holding, setHolding] = useState(false)
  const finish = useCallback((source?: string, commit = false) => {
    const current = press.current
    if (!current || source && source !== current.source) return false
    press.current = null
    window.clearTimeout(current.timer)
    if (current.element && current.previousRate !== null) current.element.playbackRate = current.previousRate
    setHolding(false)
    if (commit && !current.long) current.tap?.()
    return current.long
  }, [])
  const start = useCallback((source: string, tap?: () => void) => {
    if (!enabled || document.hidden || press.current?.source === source) return
    finish()
    const current: SpeedPress = { source, timer: 0, long: false, element: null, previousRate: null, tap }
    press.current = current
    current.timer = window.setTimeout(() => {
      if (press.current !== current) return
      current.long = true
      const element = video.current
      if (!element || element.paused) return
      current.element = element
      current.previousRate = element.playbackRate
      element.playbackRate = 2
      setHolding(true)
    }, 400)
  }, [enabled, finish, video])
  useEffect(() => {
    if (!enabled) finish()
    const keyup = (event: KeyboardEvent) => {
      if (event.key === 'ArrowRight' && press.current?.source === 'keyboard') {
        event.preventDefault()
        finish('keyboard', true)
      }
    }
    const keydown = (event: KeyboardEvent) => {
      if (press.current?.source === 'keyboard' && event.key !== 'ArrowRight') finish()
    }
    const pointerup = (event: PointerEvent) => { finish(`pointer:${event.pointerId}`) }
    const blur = () => { finish() }
    const visibility = () => { if (document.hidden) finish() }
    window.addEventListener('keyup', keyup)
    window.addEventListener('keydown', keydown)
    window.addEventListener('pointerup', pointerup)
    window.addEventListener('pointercancel', pointerup)
    window.addEventListener('blur', blur)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      finish()
      window.removeEventListener('keyup', keyup)
      window.removeEventListener('keydown', keydown)
      window.removeEventListener('pointerup', pointerup)
      window.removeEventListener('pointercancel', pointerup)
      window.removeEventListener('blur', blur)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [enabled, finish])
  return { start, finish, holding, isLong: () => press.current?.long === true }
}
