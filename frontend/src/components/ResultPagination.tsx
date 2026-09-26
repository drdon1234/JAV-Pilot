import { ChevronLeft, ChevronRight } from 'lucide-react'
import { type KeyboardEvent, useEffect, useId, useRef, useState } from 'react'

import { Button } from './ui'

const EDGE_PAGE_COUNT = 3

function pageRange(start: number, end: number): number[] {
  return Array.from({ length: Math.max(0, end - start + 1) }, (_, index) => start + index)
}

function jumpValue(page: number, pageCount: number): string {
  return pageCount > EDGE_PAGE_COUNT * 2 ? String(page) : ''
}

function positiveInteger(value: number, fallback: number): number {
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : fallback
}

export interface ResultPaginationProps {
  ariaLabel: string
  page: number
  pageCount: number
  disabled?: boolean
  className?: string
  onPageChange: (page: number) => void
}

export function ResultPagination({
  ariaLabel,
  page,
  pageCount,
  disabled = false,
  className = '',
  onPageChange,
}: ResultPaginationProps) {
  const safePageCount = positiveInteger(pageCount, 1)
  const safePage = Math.min(safePageCount, positiveInteger(page, 1))
  const [draftPage, setDraftPage] = useState(() => jumpValue(safePage, safePageCount))
  const [jumpError, setJumpError] = useState('')
  const jumpErrorId = useId()
  const draftDirty = useRef(false)
  const previousPage = useRef(safePage)

  useEffect(() => {
    const pageChanged = previousPage.current !== safePage
    previousPage.current = safePage
    if (pageChanged || !draftDirty.current) {
      setDraftPage(jumpValue(safePage, safePageCount))
    }
    if (pageChanged) draftDirty.current = false
    setJumpError('')
  }, [safePage, safePageCount])

  const allPagesVisible = safePageCount <= EDGE_PAGE_COUNT * 2
  const leadingPages = allPagesVisible
    ? pageRange(1, safePageCount)
    : pageRange(1, EDGE_PAGE_COUNT)
  const trailingPages = allPagesVisible
    ? []
    : pageRange(safePageCount - EDGE_PAGE_COUNT + 1, safePageCount)

  // Keep the current page in the jump input and build one ordered set for the
  // surrounding links. This prevents boundary pages (1-3 and N-2-N) from
  // being rendered twice when the current page is near an edge.
  const compactPages = allPagesVisible
    ? []
    : Array.from(new Set([
        ...leadingPages,
        ...trailingPages,
        safePage - 1,
        safePage,
        safePage + 1,
      ].filter((pageNumber) => pageNumber >= 1 && pageNumber <= safePageCount))).sort((left, right) => left - right)

  function goToPage(targetPage: number) {
    if (disabled || targetPage < 1 || targetPage > safePageCount) return
    setJumpError('')
    onPageChange(targetPage)
  }

  function submitJump(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      draftDirty.current = false
      setDraftPage(jumpValue(safePage, safePageCount))
      setJumpError('')
      return
    }
    if (event.key !== 'Enter') return
    event.preventDefault()
    const value = draftPage.trim()
    const targetPage = /^\d+$/.test(value) ? Number(value) : Number.NaN
    if (!Number.isSafeInteger(targetPage) || targetPage < 1 || targetPage > safePageCount) {
      setJumpError(`请输入 1 到 ${safePageCount} 之间的页码`)
      return
    }
    draftDirty.current = false
    goToPage(targetPage)
  }

  function renderPageButton(pageNumber: number) {
    const current = pageNumber === safePage
    return (
      <Button
        type="button"
        size="small"
        variant="ghost"
        className="result-pagination-page"
        aria-label={`第 ${pageNumber} 页`}
        aria-current={current ? 'page' : undefined}
        disabled={disabled}
        onClick={() => goToPage(pageNumber)}
        key={pageNumber}
      >
        {pageNumber}
      </Button>
    )
  }

  return (
    <nav
      className={`result-pagination ${className}`.trim()}
      aria-label={ariaLabel}
      aria-busy={disabled || undefined}
    >
      <Button
        type="button"
        size="small"
        variant="ghost"
        className="result-pagination-step"
        aria-label="上一页"
        disabled={disabled || safePage <= 1}
        onClick={() => goToPage(safePage - 1)}
      >
        <ChevronLeft aria-hidden="true" />
        <span className="result-pagination-step-label">上一页</span>
      </Button>

      {allPagesVisible ? (
        <span className="result-pagination-pages">
          {leadingPages.map(renderPageButton)}
        </span>
      ) : (
        <span className="result-pagination-jump">
          {compactPages.map((pageNumber, index) => {
            const previousPageNumber = compactPages[index - 1]
            const gap = previousPageNumber !== undefined && pageNumber > previousPageNumber + 1
            return (
              <span className="result-pagination-token" key={pageNumber}>
                {gap ? <span className="result-pagination-gap" aria-hidden="true">…</span> : null}
                {pageNumber === safePage ? (
                  <input
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    autoComplete="off"
                    enterKeyHint="go"
                    value={draftPage}
                    placeholder="页码"
                    aria-label={`跳转页码，当前第 ${safePage} 页，共 ${safePageCount} 页`}
                    aria-invalid={Boolean(jumpError)}
                    aria-describedby={jumpError ? jumpErrorId : undefined}
                    disabled={disabled}
                    maxLength={String(safePageCount).length}
                    onChange={(event) => {
                      draftDirty.current = true
                      setDraftPage(event.target.value)
                      setJumpError('')
                    }}
                    onFocus={(event) => event.currentTarget.select()}
                    onKeyDown={submitJump}
                  />
                ) : renderPageButton(pageNumber)}
              </span>
            )
          })}
        </span>
      )}

      <Button
        type="button"
        size="small"
        variant="ghost"
        className="result-pagination-step"
        aria-label="下一页"
        disabled={disabled || safePage >= safePageCount}
        onClick={() => goToPage(safePage + 1)}
      >
        <span className="result-pagination-step-label">下一页</span>
        <ChevronRight aria-hidden="true" />
      </Button>

      {jumpError ? <span id={jumpErrorId} className="sr-only" role="status">{jumpError}</span> : null}
    </nav>
  )
}
