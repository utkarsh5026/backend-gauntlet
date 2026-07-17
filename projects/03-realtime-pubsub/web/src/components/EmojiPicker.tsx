// A Notion-style icon picker: click the avatar to open a popover with a row of
// background-color swatches and a categorized emoji grid. Choosing a color
// updates the background live; choosing an emoji sets it and closes.
//
// Self-contained (no popover dependency): an invisible full-screen backdrop
// catches outside clicks. Swap for a shadcn <Popover> later if you like.
//
// Used for BOTH people and groups — the value it edits is just `{ emoji, color }`,
// which maps straight onto the directory rows (persisted in Postgres).

import { useState } from 'react'

import { COLORS, EMOJI_CATEGORIES } from '@/lib/emoji-data'

import { Avatar } from './Avatar'

export interface EmojiValue {
  emoji: string
  color: string
}

interface EmojiPickerProps {
  value: EmojiValue
  onChange: (next: EmojiValue) => void
  /** Diameter of the trigger avatar, px. */
  size?: number
}

export function EmojiPicker({ value, onChange, size = 44 }: EmojiPickerProps) {
  const [open, setOpen] = useState(false)

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="focus-visible:ring-ring rounded-full transition hover:opacity-80 focus:outline-none focus-visible:ring-2"
        aria-label="Pick an emoji and color"
      >
        <Avatar emoji={value.emoji} color={value.color} size={size} />
      </button>

      {open && (
        <>
          {/* outside-click catcher */}
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="bg-popover text-popover-foreground absolute left-0 z-50 mt-2 w-72 rounded-lg border p-3 shadow-lg">
            {/* background color swatches */}
            <div className="mb-3 flex flex-wrap gap-1.5">
              {COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  aria-label={`Background ${c}`}
                  onClick={() => onChange({ ...value, color: c })}
                  className={`size-5 rounded-full transition hover:scale-110 ${
                    c === value.color ? 'ring-foreground ring-offset-popover ring-2 ring-offset-2' : ''
                  }`}
                  style={{ background: c }}
                />
              ))}
            </div>

            {/* categorized emoji grid */}
            <div className="max-h-56 overflow-y-auto pr-1">
              {EMOJI_CATEGORIES.map((cat) => (
                <div key={cat.name} className="mb-2">
                  <div className="text-muted-foreground mb-1 px-0.5 text-[10px] font-medium uppercase tracking-wide">
                    {cat.name}
                  </div>
                  <div className="grid grid-cols-8 gap-0.5">
                    {cat.emojis.map((e) => (
                      <button
                        key={e}
                        type="button"
                        onClick={() => {
                          onChange({ ...value, emoji: e })
                          setOpen(false)
                        }}
                        className={`hover:bg-accent flex aspect-square items-center justify-center rounded text-lg ${
                          e === value.emoji ? 'bg-accent' : ''
                        }`}
                      >
                        {e}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
