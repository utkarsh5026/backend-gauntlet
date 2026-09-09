#!/usr/bin/env python3
"""The Definition-of-done harness — the four things the SPEC asks you to prove.

1. **Throughput** — sustained upload and download MB/s over the real HTTP path.
2. **Bounded memory** — RSS stays flat while streaming an object many times
   larger than the process's resident set. This is V2's whole payoff, and it is
   the one number that cannot be faked by a fast disk.
3. **Dedup** — N identical PUTs produce one blob, and the storage saved is
   reported rather than asserted.
4. **Multipart** — a large object uploaded in parallel parts, with the assembled
   ETag checked against S3's `-N` formula.

Crash consistency is *not* here, and deliberately so: proving "a `kill -9`
mid-PUT never yields a truncated object" needs a real process to kill, so it
lives in `crash.py` beside this file. The rest runs in-process against the
actual ASGI app, so the numbers include routing, streaming and the index write,
but not TCP.

One warning that cost an hour the first time: the download is driven against the
**raw ASGI app**, not through httpx. See `drain_get` — measuring RSS around an
httpx `ASGITransport` download measures the client's buffer and makes a
perfectly streaming server look like it holds the whole object.

    make bench
    SIZE=512M PUTS=20 uv run python bench/harness/main.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from object_store.config import Settings  # noqa: E402
from object_store.main import build_state, create_app  # noqa: E402
from object_store.multipart import multipart_etag  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"
MIB = 1024 * 1024


def parse_size(raw: str) -> int:
    text = raw.strip().upper()
    for suffix, scale in (("K", 1024), ("M", MIB), ("G", 1024**3)):
        if text.endswith(suffix):
            return int(float(text[:-1]) * scale)
    return int(text)


def rss_bytes() -> int:
    """Resident set size.

    `ru_maxrss` is a high-water mark, so it only ever climbs — which is exactly
    what makes it the right measurement here. If the stream loop ever buffered a
    whole object, the peak would jump by the object's size and stay there.
    Linux reports it in kibibytes; macOS in bytes.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw * 1024 if sys.platform.startswith("linux") else raw


async def drain_get(app: object, path: str) -> int:
    """GET `path` straight off the ASGI app, discarding every byte.

    **Not** through httpx, and this is the single most important line in the
    harness. `ASGITransport` accumulates the whole response body in a list
    before handing back a response — so measuring RSS around an httpx download
    measures *the client's* buffer and reports a perfectly streaming server as
    using memory proportional to the object. That artifact is roughly 4x the
    object size, which looks exactly like the bug V2 exists to prevent.

    Driving the app directly with a `send` that counts and drops is the only way
    to see what the *server* holds.

    The `receive` below is fussier than it looks. Starlette's `StreamingResponse`
    runs a disconnect listener that calls `receive()` in a loop until it sees
    `http.disconnect`, so a callable that always returns `http.request` spins
    that task forever and the whole request hangs. It has to deliver the (empty)
    request body once, then park until the response is complete, then report the
    disconnect.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"bench")],
        "client": ("127.0.0.1", 0),
        "server": ("bench", 80),
    }
    received = 0
    body_sent = False
    finished = asyncio.Event()

    async def receive() -> dict[str, object]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal received
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            assert isinstance(body, bytes)
            received += len(body)
            if not message.get("more_body", False):
                finished.set()

    await app(scope, receive, send)  # type: ignore[operator]
    finished.set()
    return received


async def chunks(total: int, chunk: int, seed: int = 0) -> AsyncIterator[bytes]:
    """Generate `total` bytes without ever holding them.

    A generator rather than a buffer, because a harness that materialises the
    payload to prove the server does not is measuring its own memory, not the
    server's.
    """
    block = bytes((seed + i) % 256 for i in range(chunk))
    sent = 0
    while sent < total:
        piece = block[: min(chunk, total - sent)]
        sent += len(piece)
        yield piece


async def main() -> None:
    size = parse_size(os.environ.get("SIZE", "256M"))
    puts = int(os.environ.get("PUTS", "8"))
    part_size = parse_size(os.environ.get("PART_SIZE", "16M"))
    chunk = parse_size(os.environ.get("CHUNK", "256K"))

    root = Path(tempfile.mkdtemp(prefix="object-store-bench-"))
    settings = Settings(_env_file=None, data_dir=root, secret_access_key="", index_url="")  # type: ignore[call-arg]
    app = create_app(settings)
    app.state.app_state = build_state(settings)
    results: dict[str, object] = {"size": size, "puts": puts, "part_size": part_size}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://bench", timeout=None
        ) as client:
            await client.put("/bench")
            baseline = rss_bytes()
            print(f"baseline RSS: {baseline / MIB:.1f} MiB\n")

            # ── 1 + 2: streaming throughput and flat memory ──────────────────
            print(f"streaming a {size / MIB:.0f} MiB object …")
            started = time.perf_counter()
            response = await client.put("/bench/big.bin", content=chunks(size, chunk))
            upload_secs = time.perf_counter() - started
            assert response.status_code == 200, response.text
            after_put = rss_bytes()

            started = time.perf_counter()
            downloaded = await drain_get(app, "/bench/big.bin")
            download_secs = time.perf_counter() - started
            after_get = rss_bytes()
            assert downloaded == size, f"got {downloaded} of {size}"

            growth = after_get - baseline
            results |= {
                "upload_mib_per_s": round(size / MIB / upload_secs, 1),
                "download_mib_per_s": round(size / MIB / download_secs, 1),
                "rss_baseline_mib": round(baseline / MIB, 1),
                "rss_after_put_mib": round(after_put / MIB, 1),
                "rss_after_get_mib": round(after_get / MIB, 1),
                "rss_growth_mib": round(growth / MIB, 1),
                "rss_growth_ratio": round(growth / size, 4),
            }
            print(f"  upload   {results['upload_mib_per_s']} MiB/s")
            print(f"  download {results['download_mib_per_s']} MiB/s")
            print(
                f"  RSS grew {growth / MIB:.1f} MiB over a {size / MIB:.0f} MiB object "
                f"({growth / size:.2%} of it)"
            )
            verdict = "FLAT ✅" if growth < size * 0.10 else "NOT FLAT ❌"
            print(f"  memory: {verdict}\n")
            results["memory_flat"] = growth < size * 0.10

            # ── 3: dedup ────────────────────────────────────────────────────
            print(f"PUTting the same {puts} MiB payload under {puts} keys …")
            payload_size = MIB
            for index in range(puts):
                await client.put(f"/bench/dup-{index}", content=chunks(payload_size, chunk, seed=7))
            blobs = [p for p in (root / "objects").rglob("*") if p.is_file()]
            # The big object above is also on disk, so the dedup set is the
            # remainder — one blob if content addressing is doing its job.
            logical = puts * payload_size
            physical = sum(p.stat().st_size for p in blobs) - size
            results |= {
                "dedup_keys": puts,
                "dedup_logical_bytes": logical,
                "dedup_physical_bytes": physical,
                "dedup_blobs": len(blobs) - 1,
                "dedup_saved_ratio": round(1 - physical / logical, 4) if logical else 0,
            }
            print(f"  {puts} keys → {len(blobs) - 1} blob(s)")
            print(
                f"  {logical / MIB:.0f} MiB logical → {physical / MIB:.1f} MiB on disk "
                f"({results['dedup_saved_ratio']:.1%} saved)\n"
            )

            # ── 4: multipart and the -N ETag ────────────────────────────────
            part_count = max(2, min(8, size // part_size))
            print(f"multipart upload in {part_count} parts …")
            initiated = await client.post("/bench/multi.bin", params={"uploads": ""})
            upload_id = initiated.text.split("<UploadId>")[1].split("</UploadId>")[0]

            # Uploaded concurrently and out of order, which is the entire reason
            # multipart exists — a serial loop would not exercise it.
            async def upload(number: int) -> tuple[int, str, bytes]:
                body = bytes((number + i) % 256 for i in range(part_size))
                response = await client.put(
                    "/bench/multi.bin",
                    params={"uploadId": upload_id, "partNumber": number},
                    content=body,
                )
                return number, response.headers["etag"], body

            started = time.perf_counter()
            parts = await asyncio.gather(*(upload(n) for n in reversed(range(1, part_count + 1))))
            multipart_secs = time.perf_counter() - started
            parts.sort()

            body = (
                "<CompleteMultipartUpload>"
                + "".join(
                    f"<Part><PartNumber>{n}</PartNumber><ETag>&quot;{tag}&quot;</ETag></Part>"
                    for n, tag, _ in parts
                )
                + "</CompleteMultipartUpload>"
            )
            completed = await client.post(
                "/bench/multi.bin", params={"uploadId": upload_id}, content=body
            )
            assert completed.status_code == 200, completed.text

            expected = multipart_etag([hashlib.md5(data).digest() for _, _, data in parts])
            head = await client.head("/bench/multi.bin")
            actual = head.headers["etag"]
            total = part_count * part_size

            results |= {
                "multipart_parts": part_count,
                "multipart_mib_per_s": round(total / MIB / multipart_secs, 1),
                "multipart_etag": actual,
                "multipart_etag_matches_formula": actual == expected,
            }
            print(
                f"  {total / MIB:.0f} MiB in {part_count} parts at "
                f"{results['multipart_mib_per_s']} MiB/s"
            )
            print(f"  ETag {actual}")
            print(
                "  matches S3's md5(concat(part md5s))-N formula: "
                f"{'✅' if actual == expected else '❌ ' + expected}\n"
            )

        RESULTS.mkdir(parents=True, exist_ok=True)
        out = RESULTS / f"harness-{int(time.time())}.json"
        out.write_text(json.dumps(results, indent=2))
        print(f"→ {out}")
        print("Curate into docs/06-benchmarks.md, and say *why* the numbers are")
        print("what they are — the finding is the deliverable, not the figure.")
    finally:
        app.state.app_state.store.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
