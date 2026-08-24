"""The broker: the top-level owner that maps topic names to topics and holds the
consumer-group coordinator.

Plumbing/wiring — the interesting behaviour lives in the verticals it composes
(`Topic` -> V3, `Log`/`Index` -> V1/V2, `GroupCoordinator` -> V4).

On-disk layout under `data_dir`::

    <data_dir>/
      topics/<topic>/<partition>/{...}.log + {...}.index   <- V1 + V2
      groups/...                                            <- V4 committed offsets

Note where the blocking work goes. Creating a topic makes directories and lays
down files, which is real filesystem I/O; it happens inside a request handler, so
it is pushed to a worker thread with `asyncio.to_thread`. It is a rare admin call
and you could get away with blocking the loop for it — but the scaffold does it
properly on purpose, because the habit is what transfers, and because "it's only
a few milliseconds" is how a p99 dies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from .errors import InvalidRequest, TopicAlreadyExists, UnknownTopic
from .group import GroupCoordinator
from .log import LogConfig
from .topic import Topic

__all__ = ["Broker", "validate_topic_name"]

logger = structlog.get_logger(__name__)

# A topic name becomes a directory name under `data_dir/topics/`, so it is
# attacker-controlled input that reaches the filesystem. 255 is the usual
# per-component limit on ext4/APFS/NTFS.
MAX_TOPIC_NAME_LEN = 255
_TOPIC_NAME_EXTRA = frozenset("-_.")


class Broker:
    """The broker. Constructed once at startup and shared by every handler."""

    def __init__(
        self,
        topics_dir: Path,
        config: LogConfig,
        default_partitions: int,
        topics: dict[str, Topic],
        groups: GroupCoordinator,
    ) -> None:
        self._topics_dir = topics_dir
        self._config = config
        self._default_partitions = default_partitions
        self._topics = topics
        self._groups = groups
        # Guards the topics map against two concurrent creates of the same name.
        # Without it, both would pass the "does it exist?" check and the second
        # would clobber the first's Topic object while its files sat on disk.
        self._lock = asyncio.Lock()

    @classmethod
    def open(cls, data_dir: Path, config: LogConfig, default_partitions: int) -> Broker:
        """Open the broker under `data_dir`, reloading any topics already on
        disk.

        Synchronous, and that is deliberate: this runs once at startup before the
        loop is serving anything, so there is no request to stall. Reloading is
        also the moment every partition's log recovers its own offsets (V1
        recovery), which is why a restart is the cheapest test you have.
        """
        topics_dir = data_dir / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        groups = GroupCoordinator.open(data_dir / "groups")

        topics: dict[str, Topic] = {}
        for entry in sorted(topics_dir.iterdir()):
            if entry.is_dir():
                topics[entry.name] = Topic.open(topics_dir, entry.name, config)
        return cls(topics_dir, config, default_partitions, topics, groups)

    @property
    def groups(self) -> GroupCoordinator:
        """The consumer-group coordinator (V4)."""
        return self._groups

    @property
    def default_partitions(self) -> int:
        return self._default_partitions

    async def create_topic(self, name: str, partitions: int | None = None) -> Topic:
        """Create a topic. `partitions=None` uses the configured default."""
        validate_topic_name(name)
        count = self._default_partitions if partitions is None else partitions
        async with self._lock:
            if name in self._topics:
                raise TopicAlreadyExists()
            topic = await asyncio.to_thread(
                Topic.create, self._topics_dir, name, count, self._config
            )
            self._topics[name] = topic
        logger.info("topic created", topic=name, partitions=topic.partition_count)
        return topic

    def topic(self, name: str) -> Topic:
        """Look up a topic by name.

        No lock and no `await`: a dict read is atomic on one event loop, and
        making the hot fetch path wait behind a topic creation would be an
        unforced error.
        """
        try:
            return self._topics[name]
        except KeyError:
            raise UnknownTopic() from None

    def list_topics(self) -> list[Topic]:
        """Every topic, name-ordered (for `GET /topics`)."""
        return [self._topics[name] for name in sorted(self._topics)]

    async def close(self) -> None:
        """Durably flush everything on shutdown.

        Called from the lifespan on SIGTERM, *after* in-flight requests have
        drained, so nothing is mid-append when the fsyncs happen. Until V1 and V4
        exist this raises `NotImplementedError`, which `main.py` catches — an
        unbuilt flush must not turn a clean shutdown into a crash.
        """
        for topic in self._topics.values():
            await topic.flush()
        await self._groups.flush()


def validate_topic_name(name: str) -> None:
    """Reject topic names that would escape the data dir or make illegal paths.

    An allowlist, not a denylist: the name becomes a directory, and a denylist of
    "bad" sequences loses to the next encoding trick. Alphanumerics plus `-_.`
    can never contain a separator or a traversal, which makes the guarantee
    structural instead of a list of things someone thought of.

    This is the security-horizontal item, and it wants its own test.
    """
    ok = (
        0 < len(name) <= MAX_TOPIC_NAME_LEN
        and all(c.isascii() and (c.isalnum() or c in _TOPIC_NAME_EXTRA) for c in name)
        # `.` and `..` pass the charset check and are exactly the traversal you
        # were guarding against.
        and name not in {".", ".."}
    )
    if not ok:
        raise InvalidRequest(f"illegal topic name: {name!r}")
