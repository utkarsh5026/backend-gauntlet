// What each part of the engine *is*, in plain language, and where its code lives.
//
// The engine is a scaffold: most of it raises `NotImplementedError` until you
// write it. Over HTTP that arrives as a bare `500 Internal Server Error`, which
// in a UI reads as "this app is broken" — the single most confusing thing about
// the old console. It isn't broken; you just haven't written that part yet.
//
// The server can't tell us which vertical failed (the error handler deliberately
// does not leak exception text), but it doesn't need to: which endpoint we called
// determines which vertical is missing. That mapping is fixed by the SPEC, so we
// keep it here and turn every 500 into an honest "not built yet — here's the
// file to open".

export type StageId = 'add' | 'refresh' | 'search' | 'delete' | 'merge'

export interface StageInfo {
  /** SPEC vertical that implements it, or null if it already works. */
  vertical: string | null
  /** Where you'd go to write it. */
  file: string
  /** One sentence, no jargon, on what this step actually does. */
  does: string
}

export const STAGES: Record<StageId, StageInfo> = {
  add: {
    vertical: null,
    file: 'src/full_text_search/analyzer.py',
    does: 'Chops your text into searchable words and holds them in memory.',
  },
  refresh: {
    vertical: 'V2',
    file: 'src/full_text_search/segment.py',
    does: 'Writes everything waiting in memory into a segment file on disk.',
  },
  search: {
    vertical: 'V5',
    file: 'src/full_text_search/shard.py',
    does: 'Asks every shard for its best matches and merges them into one list.',
  },
  delete: {
    vertical: 'V4',
    file: 'src/full_text_search/index.py',
    does: 'Marks a document as deleted so it stops showing up in results.',
  },
  merge: {
    vertical: 'V4',
    file: 'src/full_text_search/merge.py',
    does: 'Squashes many small segment files into one and drops deleted docs.',
  },
}

/** How a call went. `notBuilt` is the expected state on a fresh scaffold — it is
 *  a to-do item, not a failure, and the UI colours it accordingly. */
export type Status = 'unknown' | 'works' | 'notBuilt' | 'broken'

/** A 500 from a stage that is still a `todo` means unwritten code; anything else
 *  (a 4xx, a network drop) is a real problem worth a red banner. */
export function classify(stage: StageId, status: number): Status {
  if (status === 0) return 'broken'
  if (status === 500 && STAGES[stage].vertical !== null) return 'notBuilt'
  return 'broken'
}
