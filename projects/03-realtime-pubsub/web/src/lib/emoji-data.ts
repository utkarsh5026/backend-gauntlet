// Data for the Notion-style emoji + background picker (see EmojiPicker.tsx).
// Curated, zero-dependency, offline. Edit these lists freely to taste — the DB
// stores whatever emoji/color you pick, so this is purely what the picker offers.

/** Background colors offered for an avatar. Muted-but-lively, dark-theme friendly. */
export const COLORS = [
  '#6366f1', // indigo
  '#8b5cf6', // violet
  '#a855f7', // purple
  '#ec4899', // pink
  '#ef4444', // red
  '#f97316', // orange
  '#f59e0b', // amber
  '#eab308', // yellow
  '#22c55e', // green
  '#10b981', // emerald
  '#14b8a6', // teal
  '#06b6d4', // cyan
  '#0ea5e9', // sky
  '#3b82f6', // blue
  '#64748b', // slate
  '#78716c', // stone
]

/** Emojis offered, grouped like Notion's picker. */
export const EMOJI_CATEGORIES: { name: string; emojis: string[] }[] = [
  {
    name: 'Smileys',
    emojis: [
      '😀', '😄', '😁', '😅', '😂', '🙂', '😊', '😇', '🙃', '😉',
      '😍', '🥰', '😘', '😜', '🤪', '🤗', '🤔', '🤨', '😎', '🥳',
      '😴', '😭', '😡', '🤯', '🥶', '🤠', '🤡', '👻', '💀', '🤖',
    ],
  },
  {
    name: 'People',
    emojis: [
      '👋', '👍', '👎', '👏', '🙌', '🤝', '💪', '🧠', '👀', '🫶',
      '✌️', '🤞', '🙏', '🧑‍💻', '👩‍🚀', '🕵️', '🦸', '🧙', '🧚', '🧑‍🎤',
      '👑', '🎅', '🧘', '💅',
    ],
  },
  {
    name: 'Animals',
    emojis: [
      '🐶', '🐱', '🦊', '🐼', '🐨', '🐯', '🦁', '🐸', '🐵', '🐧',
      '🦉', '🦄', '🐝', '🦋', '🐙', '🐳', '🦖', '🐢', '🐬', '🦩',
      '🐡', '🦔', '🐺', '🦅',
    ],
  },
  {
    name: 'Food',
    emojis: [
      '🍎', '🍌', '🍇', '🍓', '🍑', '🍍', '🥑', '🍅', '🌶️', '🌽',
      '🍔', '🍟', '🍕', '🌮', '🍣', '🍜', '🍩', '🍪', '🎂', '🍰',
      '🍫', '🍿', '☕', '🍺',
    ],
  },
  {
    name: 'Activities',
    emojis: [
      '⚽', '🏀', '🏈', '⚾', '🎾', '🏐', '🎱', '🏓', '🎮', '🎲',
      '🎯', '🎳', '🎸', '🎹', '🎧', '🎨', '🎬', '🚀', '🏆', '🥇',
      '🎉', '🎈', '🔥', '✨',
    ],
  },
  {
    name: 'Objects',
    emojis: [
      '💻', '🖥️', '📱', '⌨️', '🖱️', '💾', '📦', '🔧', '⚙️', '🔒',
      '🔑', '💡', '🔔', '📌', '📎', '📚', '📝', '✏️', '📊', '📈',
      '💰', '💎', '🛰️', '🔭',
    ],
  },
  {
    name: 'Symbols',
    emojis: [
      '💬', '💭', '✅', '❌', '⚠️', '❓', '❗', '➕', '♻️', '💯',
      '🆒', '🆕', '🔴', '🟠', '🟡', '🟢', '🔵', '🟣', '⚫', '⚪',
      '🟥', '🟩', '🟦', '⭐',
    ],
  },
]
