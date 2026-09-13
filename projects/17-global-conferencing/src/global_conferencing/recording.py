"""V4 — Server-side recording. `src/global_conferencing/recording.py`.

A recorded meeting is not a magic side-channel bolted onto the SFU — the clean
design is that the recorder is **another subscriber**: it joins the room like any
viewer, receives each publisher's forwarded RTP, and writes it to disk instead of
a screen. That framing *is* the learning. Recording rides the exact cascade you
built — the recorder subscribes in some region and pulls a remote publisher over
a relay leg, **counting as demand** for it (V3) — and inherits the SFU's
**no-transcode** property: you persist the encoded RTP, you never decode a pixel.

The parts genuinely the recorder's own:

* **Cross-track wall-clock alignment.** Each publisher's RTP timestamp runs on its
  own clock from an arbitrary random offset, so two tracks' timestamps are
  mutually meaningless. An RTCP **Sender Report** pins one RTP timestamp to one
  NTP wall-clock time for its track; with that pair per track, every packet maps
  onto a shared timeline (project 14's clock-mapping idea). Aligning by *arrival*
  time instead bakes network jitter into the recording permanently.
* **Durable, segmented output.** Segments with an index, so a crash loses at most
  the open segment, and `stop` / shutdown finalizes cleanly.

## The trap this module sets for Python specifically

`on_track_packet` is called per packet, on the event loop that also runs the
signaling API, the backbone pump and every other room. A plain `file.write()`
there is a blocking syscall on that loop, and a slow disk becomes a slow
conference. Where the write happens instead — `asyncio.to_thread`, a writer task
draining a **bounded** per-recording queue, something else — is a decision the
"no blocking call on the event loop" checklist item grades, and
`PYTHONASYNCIODEBUG=1` is how you catch yourself getting it wrong.

Scaffold state: construction and the `/status` table are wired; no filesystem I/O
happens until a recording starts. Starting, writing, aligning and finalizing are
the V4 worklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

__all__ = ["ActiveRecording", "Recorder", "RecordingConfig"]


class ActiveRecording(BaseModel):
    """A live recording of one room — the recorder-as-subscriber's bookkeeping."""

    room_id: str
    started_at_ms: int
    """Unix millis the recording started — the origin of the room's common timeline."""

    tracks: int
    """Publisher tracks this recording is subscribed to."""

    segments: int
    """Segments finalized (closed + indexed) so far."""


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    directory: Path
    """Where segmented recordings are written — one subtree per room. Room ids are
    pattern-checked at the HTTP boundary (`rpc.ROOM_ID_PATTERN`), which is what
    keeps a room id from ever being a path traversal here."""

    segment_secs: float
    """Segment length. A crash loses at most one open segment."""


class Recorder:
    """The server-side recorder: a set of recordings, each a durable subscriber to a room."""

    def __init__(self, config: RecordingConfig) -> None:
        self.config = config
        self._recordings: dict[str, ActiveRecording] = {}

        # TODO(V4): per recording, the open segment (file handle or writer task),
        # its index, a bounded buffer of pending writes, and per track the latest
        # Sender Report mapping (an `(rtp_ts, ntp_seconds)` pair is enough to
        # start; clock rate is the other half of the conversion).

    def active(self) -> list[ActiveRecording]:
        """Active recordings (for `/status`)."""
        return [self._recordings[room_id] for room_id in sorted(self._recordings)]

    # ---- V4 worklist: subscribe · write · align · finalize -----------------

    async def start(self, room_id: str) -> None:
        """Start recording `room_id`.

        TODO(V4): subscribe the recorder to **every** publisher in the room —
        pulling remote ones over a cascade leg (V2), and counting as demand for
        their layers (V3) — then create the room's output subtree and first
        segment. **Idempotent**: a second start for a room already recording is a
        no-op, never a second, forked recording. Bump `RECORDINGS_ACTIVE`.

        Directory creation is blocking I/O too (`Path.mkdir`); see the module
        docstring.
        """
        raise NotImplementedError(
            "V4: subscribe the recorder to all publishers, open the first segment"
        )

    async def stop(self, room_id: str) -> None:
        """Stop recording `room_id`: flush and finalize the open segment, close the index.

        TODO(V4): **idempotent** — safe on an unknown or already-stopped room. A
        cleanly stopped recording always has a complete, closed index. Release the
        recorder's subscriptions so its demand leaves V3's legs.
        """
        raise NotImplementedError("V4: finalize open segments, close the index, mark stopped")

    def on_track_packet(
        self,
        room_id: str,
        publisher: int,
        packet: bytes,
        sender_report: bytes | None = None,
    ) -> None:
        """Accept one track's encoded RTP `packet` for recording.

        TODO(V4): the bytes are written **byte-identical** — no re-encode. Roll to a
        new segment every `config.segment_secs` (finalizing and indexing the old
        one, bump `RECORDING_SEGMENTS_TOTAL`). When `sender_report` is present it is
        the raw RTCP SR for this track: parse the NTP timestamp and RTP timestamp
        out of it (bounds-checked — it came off the network) and update the track's
        clock mapping. Bump `RECORDED_BYTES_TOTAL`.

        Synchronous and called per packet on the event loop: it must not block,
        and its buffering must be bounded. See the module docstring.
        """
        raise NotImplementedError(
            "V4: append encoded RTP to the segment, roll segments, track SR wall-clock mapping"
        )

    async def flush_all(self) -> None:
        """Finalize **every** open recording on graceful shutdown.

        TODO(V4): no recording left with a truncated segment or an unclosed index.
        Called from the lifespan after the backbone pump stops, so no new packets
        arrive while you flush.
        """
        raise NotImplementedError("V4: finalize every active recording (called on SIGTERM)")
