import { useSyncExternalStore } from 'react'

import { store, type Snapshot } from '@/lib/store'

/** Subscribe a component to the playground store's latest immutable snapshot. */
export function usePlayground(): Snapshot {
  return useSyncExternalStore(store.subscribe, store.getSnapshot)
}
