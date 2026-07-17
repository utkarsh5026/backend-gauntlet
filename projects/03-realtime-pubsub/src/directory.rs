//! The **directory** — a persistent roster of *people* and *groups* for the
//! admin panel / playground, backed by Postgres.
//!
//! # Why this is a separate concern
//!
//! The pub/sub core (V1–V4) is deliberately **store-free**: the hub is the
//! in-memory source of truth for live subscriptions and Redis is only a *bus*,
//! never a store (see `SPEC.md` — "Redis is the bus, not the store"). Presence
//! is *soft state*: a person only exists there while their socket is open.
//!
//! This module adds the other half the admin panel needs: a **hard roster** that
//! outlives any connection. A [`Person`] here is a directory record — a name and
//! a cute emoji avatar (emoji + background color) — that exists whether or not
//! they are currently online.
//! "Online" is **not** a column you flip; it is derived at runtime from *"does
//! this person have a live WebSocket right now?"* (the presence registry). The
//! only durable *intent* we store is `autoconnect`: whether the panel should
//! bring them online on load.
//!
//! A [`Group`] is the persistent side of a topic: "Alice belongs to #eng" is a
//! [`Membership`] row that is true even when Alice is offline. Bringing her
//! online means opening a socket and `subscribe`-ing it to each of her groups.
//!
//! Keep this out of the hub/presence path — it is admin scaffolding, not a
//! vertical.
//!
//! # Your worklist
//!
//! The method bodies are [`todo!()`]. Fill them with `sqlx` — prefer the
//! compile-time-checked `query!` / `query_as!` macros. Because the scaffold has
//! **no** `query!` yet, it builds offline with no `.sqlx` cache; the moment you
//! add one you must regenerate the per-project cache (`make prepare`) or the
//! offline CI build fails. See `CLAUDE.md` → "sqlx offline cache is per-project".

use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::PgPool;
use uuid::Uuid;

/// A directory record: someone who *can* be brought online. Persistent — this
/// outlives any WebSocket. `autoconnect` is the only online/offline state we
/// persist (an *intent*); actual online-ness is live, read from presence.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Person {
    pub id: Uuid,
    pub name: String,
    /// Avatar = a chosen emoji rendered on `color` (Notion-style icon).
    pub emoji: String,
    /// Background color for the avatar (hex, e.g. `#6366f1`).
    pub color: String,
    /// Should the panel auto-connect this person on load? (Durable intent.)
    pub autoconnect: bool,
    pub created_at: DateTime<Utc>,
}

/// The persistent side of a topic. `name` *is* the topic string a socket
/// subscribes to; `emoji` + `color` are its Notion-style avatar for the panel.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Group {
    pub id: Uuid,
    pub name: String,
    /// Avatar emoji for the group, rendered on `color`.
    pub emoji: String,
    pub color: String,
    pub created_at: DateTime<Utc>,
}

/// A person↔group edge. "Alice belongs to #eng", true whether or not Alice is
/// online. Bringing her online projects each edge into a live `subscribe`.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Membership {
    pub person_id: Uuid,
    pub group_id: Uuid,
}

/// Handle onto the roster tables. Cheap to clone (a [`PgPool`] is `Arc` inside),
/// so it lives directly in [`AppState`](crate::AppState).
#[derive(Debug, Clone)]
pub struct Directory {
    pool: PgPool,
}

impl Directory {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Borrow the pool — handy while you wire the queries up.
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Every person in the directory, newest first.
    pub async fn list_people(&self) -> Result<Vec<Person>, sqlx::Error> {
        sqlx::query_as!(Person, "SELECT * FROM people ORDER BY created_at DESC")
            .fetch_all(&self.pool)
            .await
    }

    /// Insert a new person and return the created row — the DB fills `id` and
    /// `created_at` (`INSERT ... RETURNING *`).
    pub async fn create_person(
        &self,
        name: &str,
        emoji: &str,
        color: &str,
    ) -> Result<Person, sqlx::Error> {
        sqlx::query_as!(
            Person,
            "INSERT INTO people (name, emoji, color) VALUES ($1, $2, $3) RETURNING *",
            name,
            emoji,
            color
        )
        .fetch_one(&self.pool)
        .await
    }

    /// Remove a person; their memberships cascade away via the FK.
    pub async fn delete_person(&self, id: Uuid) -> Result<(), sqlx::Error> {
        sqlx::query!("DELETE FROM people WHERE id = $1", id)
            .execute(&self.pool)
            .await
            .map(|_| ())
    }

    /// Persist the auto-connect intent for a person.
    pub async fn set_autoconnect(&self, id: Uuid, on: bool) -> Result<(), sqlx::Error> {
        sqlx::query!("UPDATE people SET autoconnect = $2 WHERE id = $1", id, on)
            .execute(&self.pool)
            .await
            .map(|_| ())
    }

    pub async fn list_groups(&self) -> Result<Vec<Group>, sqlx::Error> {
        sqlx::query_as!(Group, "SELECT * FROM groups ORDER BY created_at DESC")
            .fetch_all(&self.pool)
            .await
    }

    pub async fn create_group(
        &self,
        name: &str,
        emoji: &str,
        color: &str,
    ) -> Result<Group, sqlx::Error> {
        sqlx::query_as!(
            Group,
            "INSERT INTO groups (name, emoji, color) VALUES ($1, $2, $3) RETURNING *",
            name,
            emoji,
            color
        )
        .fetch_one(&self.pool)
        .await
    }

    pub async fn delete_group(&self, id: Uuid) -> Result<(), sqlx::Error> {
        sqlx::query!("DELETE FROM groups WHERE id = $1", id)
            .execute(&self.pool)
            .await
            .map(|_| ())
    }

    /// Every edge. The panel joins these against people+groups client-side to
    /// render "who is in what".
    pub async fn memberships(&self) -> Result<Vec<Membership>, sqlx::Error> {
        sqlx::query_as!(Membership, "SELECT person_id, group_id FROM memberships")
            .fetch_all(&self.pool)
            .await
    }

    /// Add a person to a group. Make it idempotent (`ON CONFLICT DO NOTHING`) so
    /// re-adding an existing member isn't an error.
    pub async fn add_member(&self, person_id: Uuid, group_id: Uuid) -> Result<(), sqlx::Error> {
        sqlx::query!(
            "INSERT INTO memberships (person_id, group_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            person_id,
            group_id
        )
        .execute(&self.pool)
        .await
        .map(|_| ())
    }

    pub async fn remove_member(&self, person_id: Uuid, group_id: Uuid) -> Result<(), sqlx::Error> {
        sqlx::query!(
            "DELETE FROM memberships WHERE person_id = $1 AND group_id = $2",
            person_id,
            group_id
        )
        .execute(&self.pool)
        .await
        .map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    //! Directory integration tests. `#[sqlx::test]` gives each test its own
    //! freshly-migrated Postgres database (needs `DATABASE_URL`).

    use super::*;

    fn dir(pool: PgPool) -> Directory {
        Directory::new(pool)
    }

    #[sqlx::test]
    async fn create_person_returns_row_with_defaults(pool: PgPool) {
        let d = dir(pool);
        let p = d
            .create_person("Alice", "🧘", "#6366f1")
            .await
            .expect("create");

        assert_eq!(p.name, "Alice");
        assert_eq!(p.emoji, "🧘");
        assert_eq!(p.color, "#6366f1");
        assert!(!p.autoconnect, "autoconnect defaults to false");
        assert!(!p.id.is_nil());
    }

    #[sqlx::test]
    async fn list_people_returns_newest_first(pool: PgPool) {
        let d = dir(pool);
        let older = d.create_person("Older", "👴", "#111111").await.unwrap();
        let newer = d.create_person("Newer", "👶", "#222222").await.unwrap();

        let people = d.list_people().await.expect("list");
        assert_eq!(people.len(), 2);
        assert_eq!(people[0].id, newer.id);
        assert_eq!(people[1].id, older.id);
        assert!(people[0].created_at >= people[1].created_at);
    }

    #[sqlx::test]
    async fn delete_person_removes_row(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Gone", "👋", "#000000").await.unwrap();

        d.delete_person(p.id).await.expect("delete");

        let people = d.list_people().await.unwrap();
        assert!(people.is_empty());
    }

    #[sqlx::test]
    async fn set_autoconnect_persists_intent(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Bob", "🤖", "#ff0000").await.unwrap();
        assert!(!p.autoconnect);

        d.set_autoconnect(p.id, true).await.expect("set on");
        let on = d
            .list_people()
            .await
            .unwrap()
            .into_iter()
            .find(|x| x.id == p.id)
            .expect("person still listed");
        assert!(on.autoconnect);

        d.set_autoconnect(p.id, false).await.expect("set off");
        let off = d
            .list_people()
            .await
            .unwrap()
            .into_iter()
            .find(|x| x.id == p.id)
            .unwrap();
        assert!(!off.autoconnect);
    }

    #[sqlx::test]
    async fn create_group_returns_row(pool: PgPool) {
        let d = dir(pool);
        let g = d
            .create_group("eng", "🎨", "#10b981")
            .await
            .expect("create");

        assert_eq!(g.name, "eng");
        assert_eq!(g.emoji, "🎨");
        assert_eq!(g.color, "#10b981");
        assert!(!g.id.is_nil());
    }

    #[sqlx::test]
    async fn list_groups_returns_newest_first(pool: PgPool) {
        let d = dir(pool);
        let older = d.create_group("alpha", "🅰️", "#111111").await.unwrap();
        let newer = d.create_group("beta", "🅱️", "#222222").await.unwrap();

        let groups = d.list_groups().await.expect("list");
        assert_eq!(groups.len(), 2);
        assert_eq!(groups[0].id, newer.id);
        assert_eq!(groups[1].id, older.id);
    }

    #[sqlx::test]
    async fn delete_group_removes_row(pool: PgPool) {
        let d = dir(pool);
        let g = d.create_group("temp", "🗑️", "#000000").await.unwrap();

        d.delete_group(g.id).await.expect("delete");

        assert!(d.list_groups().await.unwrap().is_empty());
    }

    #[sqlx::test]
    async fn add_member_and_list_memberships(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Alice", "🧘", "#6366f1").await.unwrap();
        let g = d.create_group("eng", "🎨", "#10b981").await.unwrap();

        d.add_member(p.id, g.id).await.expect("add");

        let edges = d.memberships().await.expect("memberships");
        assert_eq!(edges.len(), 1);
        assert_eq!(edges[0].person_id, p.id);
        assert_eq!(edges[0].group_id, g.id);
    }

    #[sqlx::test]
    async fn add_member_is_idempotent(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Alice", "🧘", "#6366f1").await.unwrap();
        let g = d.create_group("eng", "🎨", "#10b981").await.unwrap();

        d.add_member(p.id, g.id).await.expect("first add");
        d.add_member(p.id, g.id)
            .await
            .expect("second add must not error");

        assert_eq!(d.memberships().await.unwrap().len(), 1);
    }

    #[sqlx::test]
    async fn remove_member_deletes_edge(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Alice", "🧘", "#6366f1").await.unwrap();
        let g = d.create_group("eng", "🎨", "#10b981").await.unwrap();
        d.add_member(p.id, g.id).await.unwrap();

        d.remove_member(p.id, g.id).await.expect("remove");

        assert!(d.memberships().await.unwrap().is_empty());
    }

    #[sqlx::test]
    async fn delete_person_cascades_memberships(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Alice", "🧘", "#6366f1").await.unwrap();
        let g = d.create_group("eng", "🎨", "#10b981").await.unwrap();
        d.add_member(p.id, g.id).await.unwrap();

        d.delete_person(p.id).await.expect("delete person");

        assert!(d.memberships().await.unwrap().is_empty());
        assert_eq!(d.list_groups().await.unwrap().len(), 1, "group survives");
    }

    #[sqlx::test]
    async fn delete_group_cascades_memberships(pool: PgPool) {
        let d = dir(pool);
        let p = d.create_person("Alice", "🧘", "#6366f1").await.unwrap();
        let g = d.create_group("eng", "🎨", "#10b981").await.unwrap();
        d.add_member(p.id, g.id).await.unwrap();

        d.delete_group(g.id).await.expect("delete group");

        assert!(d.memberships().await.unwrap().is_empty());
        assert_eq!(d.list_people().await.unwrap().len(), 1, "person survives");
    }
}
