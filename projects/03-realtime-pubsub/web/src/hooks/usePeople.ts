import { useSyncExternalStore } from 'react'

import { peopleStore, type PeopleSnapshot } from '@/lib/people-store'

/** Subscribe a component to the people store's latest snapshot (the roster +
 *  each person's live online/offline status). */
export function usePeople(): PeopleSnapshot {
  return useSyncExternalStore(peopleStore.subscribe, peopleStore.getSnapshot)
}
