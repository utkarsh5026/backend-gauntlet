"""V4 — Byte-range delivery: seek and adapt over HTTP.

Module: `src/vod_streaming/delivery.py`.

Video seeking and single-file packaging both ride on HTTP `Range`: the player
asks for `bytes=a-b` and expects `206 Partial Content` with a `Content-Range` and
*only* that slice. This module turns a built resource — an init segment or a
media segment, already `bytes` in memory — plus an optional `Range` header into
the right response.

The ABR half of V4 lives elsewhere: the ladder is in the master playlist
(`manifest.hls_master_playlist`) and the alignment that makes switching seamless
is in the segmenter. This module is the transport half — the `206`/`416`/
`Content-Range` mechanics every media response needs.

## Why this returns a value and raises, instead of returning a three-way enum

The Rust version modelled the outcome as an enum with `Full`, `Partial{start,
end}` and `Unsatisfiable` variants, because that is how Rust says "one of three
things". Porting that shape into Python would give you an enum, a dataclass and a
match statement to express what Python already has two perfectly good mechanisms
for: return the answer, raise the failure.

So `resolve_range` returns `(start, end)` for a partial, `None` for "serve the
whole thing", and raises `RangeNotSatisfiable` for the 416 — which is already in
the error hierarchy, already carries the resource length, and is already wired to
emit `Content-Range: bytes */<total>`. Two of the three outcomes are values; the
one that is genuinely an error is an error.

## Bounded memory, and the header that makes or breaks it

The SPEC wants a media body streamed rather than buffered whole. In Python:

* Slice a `memoryview`, not `bytes`. `memoryview(body)[start : end + 1]` costs
  nothing; `body[start : end + 1]` copies the slice, so serving the first byte of
  a 10 MB segment allocates... one byte, fine — but serving `bytes=0-` allocates a
  second 10 MB. The view is free in both cases.
* `StreamingResponse` over a generator that yields fixed-size chunks from that
  view keeps the peak bounded by the chunk, and gives you backpressure for free:
  the generator is only pulled as fast as the client reads.
* **`StreamingResponse` does not set `Content-Length` for you** — it has no way to
  know it — and the SPEC grades "a `Content-Length` that matches the bytes
  actually returned". So set it explicitly, and set it to the *slice* length on a
  206, not the resource length. Getting this wrong is not a subtle bug: too large
  and the client waits forever for bytes that never come, too small and it
  truncates the segment mid-frame.
* Yield `bytes(chunk)`, not the view itself. A `memoryview` handed to the ASGI
  layer can outlive the buffer it points into.

## The parsing, briefly

RFC 9110 range syntax is small but has three forms that are easy to conflate, and
`bytes=-100` meaning *the last hundred bytes* rather than *bytes 0 through 100* is
the one everybody gets wrong the first time. Do not reach for a regex before you
have written down what each form means; the parse is a `split` and two `int`
calls, and the interesting part is entirely in the clamping rules.
"""

from __future__ import annotations

from fastapi import Response

__all__ = ["resolve_range", "serve_ranged", "text_response"]

CHUNK_SIZE = 64 * 1024
"""Bytes per chunk when streaming a media body.

64 KiB is comfortably above the socket send buffer, so the loop is not woken per
packet, and small enough that peak memory per in-flight response is noise. Tune
it against the numbers in `docs/11-benchmarks.md` rather than by feel — it trades
syscalls against per-connection memory, and the crossover depends on how many
connections you are actually holding."""

PLAYLIST_CACHE_CONTROL = "public, max-age=6"
"""Playlists are regenerated cheaply and a VOD one never changes, but a short TTL
keeps a mistake in the ladder from being pinned in caches for a day."""


def resolve_range(range_header: str | None, total: int) -> tuple[int, int] | None:
    """Resolve a `Range` header against a resource of `total` bytes — the V4 core.

    Returns inclusive `(start, end)` byte bounds for a partial response, or `None`
    when the whole resource should be served as `200`. Raises
    `RangeNotSatisfiable` (which renders as a 416 with the required
    `Content-Range: bytes */<total>`) when the range cannot be answered.

    TODO(V4): parse RFC 9110 single-range syntax and resolve it:

      * `None` — no header at all — is `None`: a plain `200`.
      * `bytes=a-b`: clamp `b` down to `total - 1`. If `a > b` or `a >= total`,
        it is unsatisfiable; otherwise `(a, b)`.
      * `bytes=a-`: open-ended, `(a, total - 1)`. Still unsatisfiable if
        `a >= total`.
      * `bytes=-n`: the *last* `n` bytes, `(total - n, total - 1)`, with `n`
        clamped to `total`. Note `n == 0` asks for the last zero bytes, which is
        unsatisfiable rather than empty.
      * A resource of length 0 can satisfy no range at all.

    Then decide, and write down in `docs/11-design.md`, what you do with the cases
    the spec leaves open to you: syntax you cannot parse, a unit other than
    `bytes`, and a multi-range request (`bytes=0-99,200-299`). Only single ranges
    are required here. Serving the whole resource as `200` is a legal answer to
    anything you decline to handle — a `Range` header is a request, not a demand —
    and it is a much friendlier failure than a 416 for a player that sent
    something slightly odd.

    Whatever you choose, it must not raise anything but `RangeNotSatisfiable`: a
    client-supplied header reaching `int()` unguarded turns `bytes=abc-` into a
    `ValueError` and a 500. That is the "malformed request is never a panic"
    criterion, and a header is the easiest place in the whole project for a
    stranger to reach your parser.
    """
    del range_header, total
    raise NotImplementedError("V4: parse `bytes=` (a-b / a- / -n) against total and resolve it")


def serve_ranged(body: bytes, content_type: str, range_header: str | None) -> Response:
    """Serve `body` as a media response, honoring an optional `Range` header (V4).

    TODO(V4): build the response from `resolve_range(range_header, len(body))`:

      * `None` -> `200`, with `Accept-Ranges: bytes`, the `Content-Type`, and
        `Content-Length: len(body)`; the body is the whole resource.
      * `(start, end)` -> `206`, adding `Content-Range: bytes {start}-{end}/{total}`
        and `Content-Length: end - start + 1`; the body is that slice.
      * The 416 needs nothing here — `resolve_range` raises, and `errors.py`
        renders it with the `Content-Range: bytes */<total>` the spec requires.

    `Accept-Ranges: bytes` belongs on *every* media response including the `200`,
    because that is how a player learns it may seek at all. Without it a client is
    entitled to assume the resource is not seekable and will download from the
    start every time.

    TODO(caching, horizontal): init segments and media segments are immutable once
    cut, so they want a long-lived `Cache-Control: public, max-age=31536000,
    immutable` and a stable `ETag`. `hashlib.blake2b(body).hexdigest()` is a fine
    ETag and cheap enough at these sizes; it must be quoted in the header
    (`ETag: "abc123"`) or it is not a valid entity-tag. Then handle
    `If-None-Match` with a `304`, which is the other half of that checklist item.
    The ETag is only meaningful if V2's output is deterministic — see
    `build_init_segment`.

    See the module docstring on streaming the body rather than handing over the
    whole `bytes`, and on the `Content-Length` that `StreamingResponse` will not
    set for you.
    """
    del body, content_type, range_header
    raise NotImplementedError("V4: turn the resolved range into a 200 / 206 response")


def text_response(body: str, content_type: str) -> Response:
    """A plain `200 OK` for a text resource (playlists, MPD).

    Plumbing — no `Range` involved. A player does not seek within a manifest, and
    manifests are small enough that streaming them would be theatre. Media goes
    through `serve_ranged` instead.
    """
    return Response(
        content=body,
        media_type=content_type,
        headers={"cache-control": PLAYLIST_CACHE_CONTROL},
    )
