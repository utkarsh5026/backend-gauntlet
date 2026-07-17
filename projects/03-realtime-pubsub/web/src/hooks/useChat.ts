import { useSyncExternalStore } from 'react'

import { store, type Snapshot } from '@/lib/store'

/** Subscribe a component to the chat store's latest immutable snapshot. */
export function useChat(): Snapshot {
  return useSyncExternalStore(store.subscribe, store.getSnapshot)
}
