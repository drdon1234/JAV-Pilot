import { createContext, useContext, useLayoutEffect, useRef, type MutableRefObject } from 'react'

export const PlaybackActivity = createContext(true)
export interface PlaybackSnapshot { image: string; position: number; duration: number; aspect: number }
export const PlaybackSession = createContext<{ autoplay: MutableRefObject<boolean>; capture: (snapshot: PlaybackSnapshot) => void; snapshot?: PlaybackSnapshot | null; exit?: () => void }>({ autoplay: { current: true }, capture: () => undefined })

export function usePlaybackActivity() {
  const active = useContext(PlaybackActivity)
  const session = useContext(PlaybackSession)
  const current = useRef(active)
  useLayoutEffect(() => { current.current = active }, [active])
  return { active, current, ...session }
}
