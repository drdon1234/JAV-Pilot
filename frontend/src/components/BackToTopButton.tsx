import { ArrowUp } from 'lucide-react'
import { useEffect, useState } from 'react'

import { scrollPageToTop } from '../lib/pageScroll'
import { IconButton } from './ui'

const REVEAL_SCROLL_PX = 320

export function BackToTopButton() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const update = () => {
      const mainContent = document.querySelector<HTMLElement>('.main-content')
      const scrollTop = Math.max(window.scrollY, mainContent?.scrollTop ?? 0)
      setVisible(scrollTop >= REVEAL_SCROLL_PX)
    }

    window.addEventListener('scroll', update, { passive: true })
    document.addEventListener('scroll', update, { capture: true, passive: true })
    update()
    return () => {
      window.removeEventListener('scroll', update)
      document.removeEventListener('scroll', update, { capture: true })
    }
  }, [])

  return (
    <IconButton
      type="button"
      label="回到顶部"
      className={`back-to-top${visible ? ' is-visible' : ''}`}
      onClick={scrollPageToTop}
      aria-hidden={!visible}
      tabIndex={visible ? 0 : -1}
    >
      <ArrowUp aria-hidden="true" />
    </IconButton>
  )
}
