"""The **directory** — a persistent roster of *people* and *groups* for the
admin panel / playground, backed by Postgres.

# Why this is a separate concern

The pub/sub core (V1-V4) is deliberately **store-free**: the hub is the
in-memory source of truth for live subscriptions and Redis is only a *bus*,
never a store (see `SPEC.md` — "Redis is the bus, not the store"). Presence is
*soft state*: a person only exists there while their socket is open.

This module adds the other half the admin panel needs: a **hard roster** that
outlives any connection. A `Person` here is a directory record — a name and a
cute emoji avatar — that exists whether or not they are currently online.
"Online" is **not** a column you flip; it is derived at runtime from *"does this
person have a live WebSocket right now?"* (the presence registry). The only
durable *intent* stored is `autoconnect`: whether the panel should bring them
online on load.

A `Group` is the persistent side of a topic: "Alice belongs to #eng" is a
`Membership` row that is true even when Alice is offline. Bringing her online
means opening a socket and subscribing it to each of her groups.

Keep this out of the hub/presence path — it is admin scaffolding, not a vertical.

# Shape

Two type families on purpose, which is the standard FastAPI + SQLAlchemy split
and worth having in the fingers:

* `PersonRow` / `GroupRow` / `MembershipRow` are the **table** definitions
  (SQLAlchemy 2.0 declarative, `Mapped[...]` annotations).
* `Person` / `Group` / `Membership` are the **wire** models (pydantic), built
  from a row with `model_validate`. They are what the API returns, so a column
  added to a table is not automatically published to clients.

The schema itself still lives in `migrations/0001_directory.sql` — the same file
the Rust version used. It is applied at startup rather than generated from these
classes, so the SQL stays the single source of truth and the two cannot drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Boolean, DateTime, ForeignKey, String, delete, select, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Directory",
    "Group",
    "Membership",
    "Person",
    "connect",
]


class Base(DeclarativeBase):
    pass


class PersonRow(Base):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    emoji: Mapped[str] = mapped_column(String, nullable=False, server_default="🧘")
    color: Mapped[str] = mapped_column(String, nullable=False, server_default="#6366f1")
    autoconnect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class GroupRow(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    emoji: Mapped[str] = mapped_column(String, nullable=False, server_default="🎨")
    color: Mapped[str] = mapped_column(String, nullable=False, server_default="#6366f1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MembershipRow(Base):
    __tablename__ = "memberships"

    person_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# --- wire models --------------------------------------------------------------


class Person(BaseModel):
    """A directory record: someone who *can* be brought online."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    emoji: str
    """Avatar = a chosen emoji rendered on `color` (Notion-style icon)."""
    color: str
    autoconnect: bool
    """Should the panel auto-connect this person on load? (Durable intent.)"""
    created_at: datetime


class Group(BaseModel):
    """The persistent side of a topic. `name` *is* the topic string a socket
    subscribes to."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    emoji: str
    color: str
    created_at: datetime


class Membership(BaseModel):
    """A person-group edge, true whether or not the person is online."""

    model_config = ConfigDict(from_attributes=True)

    person_id: uuid.UUID
    group_id: uuid.UUID


# --- the handle ---------------------------------------------------------------


class Directory:
    """Handle onto the roster tables."""

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # `expire_on_commit=False` so the attributes of a returned row are still
        # readable after the session's commit. Without it, reading `person.name`
        # on the way out triggers a *lazy refresh* — a second round trip, and in
        # async SQLAlchemy an error rather than a silent one.
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine — used by the `/debug/health` probe."""
        return self._engine

    async def list_people(self) -> list[Person]:
        """Every person in the directory, newest first."""
        async with self._session() as session:
            rows = await session.scalars(select(PersonRow).order_by(PersonRow.created_at.desc()))
            return [Person.model_validate(row) for row in rows]

    async def create_person(self, name: str, emoji: str, color: str) -> Person:
        """Insert a new person and return the created row — the DB fills `id`
        and `created_at`."""
        async with self._session.begin() as session:
            row = PersonRow(name=name, emoji=emoji, color=color)
            session.add(row)
            # Force the INSERT now so the server defaults come back and the
            # object is fully populated before the session closes.
            await session.flush()
            await session.refresh(row)
            return Person.model_validate(row)

    async def delete_person(self, person_id: uuid.UUID) -> None:
        """Remove a person; their memberships cascade away via the FK."""
        async with self._session.begin() as session:
            await session.execute(delete(PersonRow).where(PersonRow.id == person_id))

    async def set_autoconnect(self, person_id: uuid.UUID, on: bool) -> None:
        """Persist the auto-connect intent for a person."""
        async with self._session.begin() as session:
            row = await session.get(PersonRow, person_id)
            if row is not None:
                row.autoconnect = on

    async def list_groups(self) -> list[Group]:
        async with self._session() as session:
            rows = await session.scalars(select(GroupRow).order_by(GroupRow.created_at.desc()))
            return [Group.model_validate(row) for row in rows]

    async def create_group(self, name: str, emoji: str, color: str) -> Group:
        async with self._session.begin() as session:
            row = GroupRow(name=name, emoji=emoji, color=color)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return Group.model_validate(row)

    async def delete_group(self, group_id: uuid.UUID) -> None:
        async with self._session.begin() as session:
            await session.execute(delete(GroupRow).where(GroupRow.id == group_id))

    async def memberships(self) -> list[Membership]:
        """Every edge. The panel joins these against people + groups client-side
        to render "who is in what"."""
        async with self._session() as session:
            rows = await session.scalars(select(MembershipRow))
            return [Membership.model_validate(row) for row in rows]

    async def add_member(self, person_id: uuid.UUID, group_id: uuid.UUID) -> None:
        """Add a person to a group.

        Idempotent via `ON CONFLICT DO NOTHING`, so re-adding an existing member
        is not an error — the panel fires this on every toggle and should not
        have to check first.
        """
        async with self._session.begin() as session:
            statement = (
                pg_insert(MembershipRow)
                .values(person_id=person_id, group_id=group_id)
                .on_conflict_do_nothing(index_elements=["person_id", "group_id"])
            )
            await session.execute(statement)

    async def remove_member(self, person_id: uuid.UUID, group_id: uuid.UUID) -> None:
        async with self._session.begin() as session:
            await session.execute(
                delete(MembershipRow).where(
                    MembershipRow.person_id == person_id,
                    MembershipRow.group_id == group_id,
                )
            )

    async def aclose(self) -> None:
        await self._engine.dispose()


def _statements(schema_sql: str) -> list[str]:
    """Split a DDL file into individual statements.

    asyncpg sends everything through the *extended* query protocol, which
    accepts exactly one statement per call — hand it a whole `.sql` file and it
    raises "cannot insert multiple commands into a prepared statement". So the
    file is split here. Comments are stripped first, because a `--` line could
    otherwise contribute a stray semicolon and produce an empty statement.
    """
    without_comments = "\n".join(
        line for line in schema_sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


async def connect(database_url: str, schema_sql: str) -> Directory:
    """Open the pool and apply the schema.

    `schema_sql` is `migrations/0001_directory.sql`, which is idempotent
    (`CREATE TABLE IF NOT EXISTS`), so running it on every boot is safe and
    keeps the `.sql` file authoritative rather than these ORM classes.

    `pool_size` is a knob the SPEC asks you to set deliberately rather than
    inherit: it caps how many concurrent admin queries can be in flight, and it
    wants to be reasoned about next to how many uvicorn workers you run — five
    connections per worker times eight workers is forty against a Postgres whose
    default `max_connections` is a hundred.
    """
    engine = create_async_engine(
        _asyncpg_url(database_url),
        pool_size=5,
        pool_pre_ping=True,
    )
    async with engine.begin() as conn:
        for statement in _statements(schema_sql):
            await conn.execute(text(statement))
    return Directory(engine)


def _asyncpg_url(database_url: str) -> str:
    """Force the asyncpg driver onto a plain Postgres URL.

    The same `DATABASE_URL` is shared with docker-compose and the Rust-era
    tooling, both of which want `postgres://`. SQLAlchemy needs the driver named
    in the scheme, and picking it here means the `.env` does not have to carry a
    Python-specific URL.
    """
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            return "postgresql+asyncpg://" + database_url[len(prefix) :]
    return database_url
