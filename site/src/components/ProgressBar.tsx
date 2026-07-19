import { cn } from '@/lib/utils'

export function ProgressBar({
  value,
  className,
}: {
  value: number
  className?: string
}) {
  const clamped = Math.max(0, Math.min(100, value))
  return (
    <div
      className={cn('h-1.5 overflow-hidden rounded-full bg-line', className)}
      role="progressbar"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-copper transition-[width] duration-500"
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}
