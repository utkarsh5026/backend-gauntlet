// A person/group avatar: a chosen emoji on a chosen background color, Notion
// style. Pure display — pick the emoji + color with <EmojiPicker>. Both `emoji`
// and `color` come from the directory (people/groups rows), so what you picked
// persists in the DB.

export interface AvatarProps {
  /** The emoji to show (e.g. "🧘"). */
  emoji: string
  /** Background color (hex, e.g. "#6366f1"). */
  color: string
  /** Diameter in px. */
  size?: number
  /** When set, draw a status ring: green online, muted offline. */
  online?: boolean
  className?: string
}

export function Avatar({ emoji, color, size = 40, online, className }: AvatarProps) {
  const ring =
    online === undefined
      ? undefined
      : online
        ? '0 0 0 2px hsl(142 70% 45%)'
        : '0 0 0 2px hsl(215 12% 40%)'

  return (
    <div
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: '9999px',
        background: color,
        boxShadow: ring,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: size * 0.55,
        lineHeight: 1,
        userSelect: 'none',
        flexShrink: 0,
      }}
    >
      {emoji}
    </div>
  )
}
