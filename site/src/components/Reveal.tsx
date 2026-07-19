import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * Progressive disclosure, TUI-flavored: a native <details> whose summary reads
 * `[+] label` closed and `[−] label` open. Zero JS, keyboard-accessible.
 */
export function Reveal({
  label,
  children,
  className,
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <details className={cn('group', className)}>
      <summary className="cursor-pointer select-none list-none text-[0.8rem] text-accent [&::-webkit-details-marker]:hidden">
        <span className="group-open:hidden">[+] {label}</span>
        <span className="hidden group-open:inline">[−] {label}</span>
      </summary>
      <div className="pt-2">{children}</div>
    </details>
  )
}
