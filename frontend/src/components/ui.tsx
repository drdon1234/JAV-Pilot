import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'
import { Inbox } from 'lucide-react'

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'

export function Button({
  variant = 'secondary',
  size = 'normal',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: 'normal' | 'small' }) {
  return <button className={`button button-${variant} button-${size} ${className}`.trim()} {...props} />
}

export function IconButton({
  label,
  size = 'normal',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; size?: 'normal' | 'small' }) {
  return (
    <button
      className={`icon-button icon-button-${size} ${className}`.trim()}
      aria-label={label}
      title={label}
      {...props}
    />
  )
}

export function StatusBadge({
  tone = 'neutral',
  children,
  className = '',
}: {
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info'
  children: ReactNode
  className?: string
}) {
  return <span className={`status-badge status-${tone} ${className}`.trim()}>{children}</span>
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        <h1 data-page-heading tabIndex={-1}>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  )
}

export function Field({
  label,
  hint,
  error,
  errorId,
  children,
  className = '',
}: {
  label: string
  hint?: string
  error?: string
  errorId?: string
  children: ReactNode
  className?: string
}) {
  return (
    <label className={`field ${className}`.trim()}>
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="field-error" id={errorId}>{error}</span> : hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  )
}

export function Toggle({
  label,
  className = '',
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & { label: string }) {
  return (
    <label className={`toggle ${className}`.trim()}>
      <input type="checkbox" {...props} />
      <span className="toggle-track" aria-hidden="true"><span /></span>
      <span>{label}</span>
    </label>
  )
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)))
  return (
    <div className="progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
      <span style={{ transform: `scaleX(${percent / 100})` }} />
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
  className = '',
  ...props
}: {
  title: string
  description?: string
  action?: ReactNode
} & HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={`empty-state ${className}`.trim()} {...props}>
      <Inbox aria-hidden="true" />
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {action}
    </div>
  )
}

export function InlineNotice({
  tone = 'info',
  children,
  ...props
}: HTMLAttributes<HTMLDivElement> & { tone?: 'info' | 'warning' | 'danger' | 'success' }) {
  return <div className={`inline-notice notice-${tone}`} {...props}>{children}</div>
}

export function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div className="skeleton-list" role="status" aria-label="正在加载">
      {Array.from({ length: count }, (_, index) => <div className="skeleton-row" key={index} />)}
    </div>
  )
}
