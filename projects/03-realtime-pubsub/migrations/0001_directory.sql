-- The admin-panel roster: the PERSISTENT half of "a person is two things".
--
-- This is playground scaffolding, NOT part of the pub/sub SPEC. The core
-- (V1–V4) is deliberately store-free — the hub is the in-memory source of truth
-- and Redis is only a bus. These tables hold the *hard state* an admin panel
-- needs: people and groups that exist whether or not anyone is connected.
--
-- "Online" is NOT stored here — it's live state, derived at runtime from "does
-- this person have an open socket?" (the presence registry). The only durable
-- online/offline signal we keep is `autoconnect`: an *intent* the panel reads on
-- load to decide who to reconnect.

CREATE TABLE IF NOT EXISTS people (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    -- Avatar = a chosen emoji on a chosen background color (Notion-style icon).
    -- We store just the pieces to render it; no image bytes.
    emoji        TEXT NOT NULL DEFAULT '🧘',
    color        TEXT NOT NULL DEFAULT '#6366f1',
    -- Durable intent: should the panel bring this person online on load?
    autoconnect  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The persistent side of a topic. `name` IS the topic string a socket
-- subscribes to; `emoji` + `color` are its Notion-style avatar for the panel.
CREATE TABLE IF NOT EXISTS groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    emoji       TEXT NOT NULL DEFAULT '🎨',
    color       TEXT NOT NULL DEFAULT '#6366f1',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A person <-> group edge. "Alice belongs to #eng" — true even while Alice is
-- offline. Bringing her online projects each edge into a live `subscribe`.
-- ON DELETE CASCADE so deleting a person or group cleans up its edges.
CREATE TABLE IF NOT EXISTS memberships (
    person_id  UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    group_id   UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (person_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_group ON memberships (group_id);
