# Object Store Console

A console for the project-06 S3-compatible object store — for demos and for
exercising the API as you build V1–V4.

Stack (per the repo's frontend convention): **Bun · React + TypeScript · Vite ·
Tailwind v4 · shadcn/ui**, dark theme.

## Run

```bash
# 1. start the backend (from projects/06-object-store/) on port 9006
PORT=9006 cargo run -p object-store

# 2. start this console (from projects/06-object-store/web/)
bun install
bun run dev                        # http://localhost:5173
```

> **Why 9006, not the store's default 9000?** MinIO (a Docker dep in this repo)
> squats host port `:9000`, so binding there fails with *Address already in use*.
> 9006 follows the repo's project-scoped port convention (project **06**). Set it
> once in `.env` (`PORT=9006`) so `cargo run` picks it up without the prefix.

Vite proxies everything under `/s3` to `http://localhost:9006` and strips the
prefix, so the browser stays same-origin — **no CORS layer is needed on the Rust
backend**. Point at a different backend with:

```bash
OBJECT_STORE_URL=http://some-host:port bun run dev
```

## What it does

- **Objects tab** — create/select buckets, list with a prefix filter and a
  `delimiter=/` "folder view", single-shot `PUT` upload (with a progress bar),
  download, and delete.
- **Multipart tab** — the showcase: pick a file, choose a part size, and watch it
  get chunked in the browser, uploaded as parallel `PUT …?partNumber=N` requests,
  and assembled with `POST …?uploadId`. The final object ETag ends in `-N` — the
  multipart signature. See `../docs/01-how-multipart-uploads-work.md`.

## Note

The backend ships with `todo!()` bodies (V1–V4). Until you implement them, the
endpoints return errors — this console is the client you build against, not a
sign anything is broken. Buckets you create are remembered in `localStorage`
(there is no ListBuckets endpoint); "forget" only drops the local chip.
