"""The S3 HTTP surface, driven end to end through the real ASGI app."""

from __future__ import annotations

import hashlib
from pathlib import Path

from httpx import AsyncClient

from object_store.s3_xml import parse_error, parse_list_bucket
from object_store.state import AppState


async def make_bucket(client: AsyncClient, name: str = "photos") -> str:
    response = await client.put(f"/{name}")
    assert response.status_code == 200
    return name


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_metrics_are_exposed(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "object_store_objects_put_total" in response.text


async def test_create_bucket_twice_conflicts(client: AsyncClient) -> None:
    await make_bucket(client)
    response = await client.put("/photos")

    assert response.status_code == 409
    assert parse_error(response.content).code == "BucketAlreadyExists"


async def test_an_invalid_bucket_name_is_a_client_error(client: AsyncClient) -> None:
    response = await client.put("/Bad_Name")

    assert response.status_code == 400
    assert parse_error(response.content).code == "InvalidRequest"


async def test_put_get_delete_round_trip(client: AsyncClient) -> None:
    await make_bucket(client)
    payload = b"hello object store"

    put = await client.put("/photos/greeting.txt", content=payload)
    assert put.status_code == 200
    assert put.headers["etag"] == hashlib.md5(payload).hexdigest()

    got = await client.get("/photos/greeting.txt")
    assert got.status_code == 200
    assert got.content == payload
    assert got.headers["content-length"] == str(len(payload))
    assert "last-modified" in got.headers

    deleted = await client.delete("/photos/greeting.txt")
    assert deleted.status_code == 204

    missing = await client.get("/photos/greeting.txt")
    assert missing.status_code == 404
    assert parse_error(missing.content).code == "NoSuchKey"


async def test_delete_is_idempotent(client: AsyncClient) -> None:
    await make_bucket(client)
    assert (await client.delete("/photos/never-existed")).status_code == 204


async def test_a_key_with_slashes_is_one_flat_key(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/a/b/c.jpg", content=b"nested")

    got = await client.get("/photos/a/b/c.jpg")
    assert got.status_code == 200
    assert got.content == b"nested"


async def test_a_traversal_shaped_key_cannot_escape(client: AsyncClient, data_dir: Path) -> None:
    """It is stored, and it is stored *inside* the data dir as one flat name."""
    await make_bucket(client)
    assert (await client.put("/photos/../../etc/passwd", content=b"nope")).status_code == 200

    assert (data_dir / "index" / "photos" / "objects").is_dir()
    assert not (data_dir.parent / "etc").exists()

    got = await client.get("/photos/../../etc/passwd")
    assert got.status_code == 200
    assert got.content == b"nope"


async def test_a_zero_byte_object_round_trips(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/empty", content=b"")

    got = await client.get("/photos/empty")
    assert got.status_code == 200
    assert got.content == b""
    assert got.headers["content-length"] == "0"


async def test_content_type_is_preserved(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put(
        "/photos/doc.json", content=b"{}", headers={"content-type": "application/json"}
    )

    got = await client.get("/photos/doc.json")
    assert got.headers["content-type"].startswith("application/json")


async def test_head_returns_metadata_without_a_body(client: AsyncClient) -> None:
    await make_bucket(client)
    payload = b"x" * 512
    await client.put("/photos/sized", content=payload)

    head = await client.head("/photos/sized")
    assert head.status_code == 200
    assert head.headers["content-length"] == "512"
    assert head.headers["etag"] == hashlib.md5(payload).hexdigest()
    assert head.content == b""


async def test_an_object_larger_than_the_cap_is_rejected(
    client: AsyncClient, app_state: AppState
) -> None:
    await make_bucket(client)
    # Shrink the cap on the *live* state the app is serving from — the app was
    # already built by the time this runs, so a fresh `Settings` would change
    # nothing.
    app_state.settings.max_object_size = 16

    response = await client.put("/photos/too-big", content=b"y" * 100)
    assert response.status_code == 413
    assert parse_error(response.content).code == "EntityTooLarge"


# ── ranges and conditional requests ─────────────────────────────────────────


async def test_a_range_request_serves_only_the_slice(client: AsyncClient) -> None:
    await make_bucket(client)
    payload = bytes(range(256))
    await client.put("/photos/blob.bin", content=payload)

    response = await client.get("/photos/blob.bin", headers={"range": "bytes=10-19"})

    assert response.status_code == 206
    assert response.content == payload[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(payload)}"
    assert response.headers["content-length"] == "10"


async def test_a_range_past_the_end_is_rejected(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/small", content=b"0123456789")

    response = await client.get("/photos/small", headers={"range": "bytes=0-99"})
    assert response.status_code == 400


async def test_a_malformed_range_is_rejected(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/small", content=b"0123456789")

    for value in ("items=0-1", "bytes=abc-def", "bytes0-1", "bytes=5-1"):
        response = await client.get("/photos/small", headers={"range": value})
        assert response.status_code == 400, value


async def test_if_none_match_on_the_etag_returns_304(client: AsyncClient) -> None:
    await make_bucket(client)
    put = await client.put("/photos/cached", content=b"cache me")
    etag = put.headers["etag"]

    response = await client.get("/photos/cached", headers={"if-none-match": etag})
    assert response.status_code == 304
    assert response.content == b""

    quoted = await client.get("/photos/cached", headers={"if-none-match": f'"{etag}"'})
    assert quoted.status_code == 304


async def test_if_none_match_with_a_stale_etag_serves_the_body(
    client: AsyncClient,
) -> None:
    await make_bucket(client)
    await client.put("/photos/cached", content=b"cache me")

    response = await client.get("/photos/cached", headers={"if-none-match": "stale"})
    assert response.status_code == 200
    assert response.content == b"cache me"


async def test_if_none_match_star_is_create_once(client: AsyncClient) -> None:
    await make_bucket(client)
    headers = {"if-none-match": "*"}

    first = await client.put("/photos/once", content=b"winner", headers=headers)
    assert first.status_code == 200

    second = await client.put("/photos/once", content=b"loser", headers=headers)
    assert second.status_code == 412
    assert parse_error(second.content).code == "PreconditionFailed"

    assert (await client.get("/photos/once")).content == b"winner"


async def test_if_match_is_compare_and_swap(client: AsyncClient) -> None:
    await make_bucket(client)
    first = await client.put("/photos/cas", content=b"base")
    etag = first.headers["etag"]

    ok = await client.put("/photos/cas", content=b"next", headers={"if-match": etag})
    assert ok.status_code == 200

    stale = await client.put("/photos/cas", content=b"stale", headers={"if-match": etag})
    assert stale.status_code == 412


# ── listing ─────────────────────────────────────────────────────────────────


async def test_list_returns_s3_xml(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/one.txt", content=b"1")
    await client.put("/photos/two.txt", content=b"22")

    response = await client.get("/photos")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    parsed = parse_list_bucket(response.content)
    assert parsed.object_keys == ["one.txt", "two.txt"]
    assert [c.size for c in parsed.contents] == [1, 2]
    assert not parsed.is_truncated


async def test_list_with_a_delimiter_shows_folders(client: AsyncClient) -> None:
    await make_bucket(client)
    for key in ("a/b/c.jpg", "a/d.jpg", "top.jpg"):
        await client.put(f"/photos/{key}", content=key.encode())

    response = await client.get("/photos", params={"delimiter": "/"})

    parsed = parse_list_bucket(response.content)
    assert parsed.object_keys == ["top.jpg"]
    assert parsed.common_prefixes == ["a/"]


async def test_list_pagination_uses_a_continuation_token(client: AsyncClient) -> None:
    await make_bucket(client)
    for index in range(5):
        await client.put(f"/photos/key-{index}", content=b"x")

    first = parse_list_bucket((await client.get("/photos", params={"max-keys": 2})).content)
    assert len(first.contents) == 2
    assert first.is_truncated
    assert first.next_continuation_token is not None

    second = parse_list_bucket(
        (
            await client.get(
                "/photos",
                params={
                    "max-keys": 2,
                    "continuation-token": first.next_continuation_token,
                },
            )
        ).content
    )
    assert [c.key for c in second.contents] == ["key-2", "key-3"]


async def test_list_escapes_xml_special_characters_in_keys(
    client: AsyncClient,
) -> None:
    """One `&` in a filename would otherwise break the listing for the bucket."""
    await make_bucket(client)
    await client.put("/photos/a&b<c>.txt", content=b"tricky")

    parsed = parse_list_bucket((await client.get("/photos")).content)
    assert parsed.object_keys == ["a&b<c>.txt"]


async def test_listing_a_missing_bucket_is_404(client: AsyncClient) -> None:
    response = await client.get("/no-such-bucket")
    assert response.status_code == 404
    assert parse_error(response.content).code == "NoSuchBucket"


# ── versioning ──────────────────────────────────────────────────────────────


async def test_an_overwrite_keeps_the_previous_version_addressable(
    client: AsyncClient,
) -> None:
    await make_bucket(client)
    await client.put("/photos/doc", content=b"v1")
    await client.put("/photos/doc", content=b"v2")

    assert (await client.get("/photos/doc")).content == b"v2"
    pinned = await client.get("/photos/doc", params={"versionId": 1})
    assert pinned.status_code == 200
    assert pinned.content == b"v1"


async def test_an_unknown_version_id_is_404(client: AsyncClient) -> None:
    await make_bucket(client)
    await client.put("/photos/doc", content=b"v1")

    assert (await client.get("/photos/doc", params={"versionId": 99})).status_code == 404


# ── multipart over HTTP ─────────────────────────────────────────────────────


async def test_multipart_over_the_wire(client: AsyncClient) -> None:
    await make_bucket(client)

    initiated = await client.post("/photos/big.bin", params={"uploads": ""})
    assert initiated.status_code == 200
    upload_id = initiated.text.split("<UploadId>")[1].split("</UploadId>")[0]

    payloads = {1: b"part-one-", 2: b"part-two"}
    etags: dict[int, str] = {}
    for number, payload in payloads.items():
        response = await client.put(
            "/photos/big.bin",
            params={"uploadId": upload_id, "partNumber": number},
            content=payload,
        )
        assert response.status_code == 200
        etags[number] = response.headers["etag"]

    body = (
        "<CompleteMultipartUpload>"
        + "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>&quot;{etags[n]}&quot;</ETag></Part>"
            for n in sorted(etags)
        )
        + "</CompleteMultipartUpload>"
    )
    completed = await client.post("/photos/big.bin", params={"uploadId": upload_id}, content=body)
    assert completed.status_code == 200

    from object_store.multipart import multipart_etag

    expected = multipart_etag([hashlib.md5(payloads[n]).digest() for n in (1, 2)])
    assert expected in completed.text

    got = await client.get("/photos/big.bin")
    assert got.content == b"part-one-part-two"
    assert got.headers["etag"] == expected


async def test_multipart_abort_over_the_wire(client: AsyncClient) -> None:
    await make_bucket(client)
    initiated = await client.post("/photos/aborted.bin", params={"uploads": ""})
    upload_id = initiated.text.split("<UploadId>")[1].split("</UploadId>")[0]
    await client.put(
        "/photos/aborted.bin",
        params={"uploadId": upload_id, "partNumber": 1},
        content=b"discarded",
    )

    aborted = await client.delete("/photos/aborted.bin", params={"uploadId": upload_id})
    assert aborted.status_code == 204
    assert (await client.get("/photos/aborted.bin")).status_code == 404


async def test_multipart_accepts_the_console_json_body(client: AsyncClient) -> None:
    """The web playground finishes uploads without building XML in the browser."""
    await make_bucket(client)
    initiated = await client.post("/photos/json.bin", params={"uploads": ""})
    upload_id = initiated.text.split("<UploadId>")[1].split("</UploadId>")[0]
    part = await client.put(
        "/photos/json.bin",
        params={"uploadId": upload_id, "partNumber": 1},
        content=b"json completed",
    )

    completed = await client.post(
        "/photos/json.bin",
        params={"uploadId": upload_id},
        json={"parts": [{"partNumber": 1, "etag": part.headers["etag"]}]},
    )
    assert completed.status_code == 200
    assert (await client.get("/photos/json.bin")).content == b"json completed"


async def test_a_post_with_no_multipart_params_is_a_client_error(
    client: AsyncClient,
) -> None:
    await make_bucket(client)
    response = await client.post("/photos/thing", content=b"")
    assert response.status_code == 400


async def test_multipart_on_a_missing_bucket_is_404(client: AsyncClient) -> None:
    response = await client.post("/no-bucket/key", params={"uploads": ""})
    assert response.status_code == 404


# ── observability ───────────────────────────────────────────────────────────


async def test_counters_move_with_traffic(client: AsyncClient) -> None:
    """Deltas, not absolutes.

    `prometheus_client` metrics are process-global by design (see
    `object_store.metrics`), so they carry values from every earlier test in the
    session. Asserting `... 2.0` passes alone and fails in a suite — which is
    itself the lesson about where these counters live.
    """
    from prometheus_client import REGISTRY

    def sample(name: str) -> float:
        value: float | None = REGISTRY.get_sample_value(name)
        return value or 0.0

    before = {
        name: sample(name)
        for name in (
            "object_store_objects_put_total",
            "object_store_objects_get_total",
            "object_store_objects_deleted_total",
            "object_store_dedup_hits_total",
            "object_store_range_requests_served_total",
        )
    }

    await make_bucket(client)
    payload = b"counted"
    await client.put("/photos/counted", content=payload)
    await client.put("/photos/counted-again", content=payload)  # a dedup hit
    await client.get("/photos/counted")
    await client.get("/photos/counted", headers={"range": "bytes=0-2"})
    await client.delete("/photos/counted")

    assert sample("object_store_objects_put_total") - before["object_store_objects_put_total"] == 2
    assert sample("object_store_objects_get_total") - before["object_store_objects_get_total"] == 2
    assert (
        sample("object_store_objects_deleted_total") - before["object_store_objects_deleted_total"]
        == 1
    )
    assert sample("object_store_dedup_hits_total") - before["object_store_dedup_hits_total"] == 1
    assert (
        sample("object_store_range_requests_served_total")
        - before["object_store_range_requests_served_total"]
        == 1
    )


async def test_the_request_id_comes_back_on_the_response(
    client: AsyncClient,
) -> None:
    response = await client.get("/healthz", headers={"x-request-id": "trace-me"})
    assert response.headers["x-request-id"] == "trace-me"
