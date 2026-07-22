# How Paths Work in S3 — From First Principles

> A beginner-friendly, ground-up guide to what a "path" actually *is* in an
> object store, using the code in this project as the reference implementation.
> No prior knowledge of S3 or filesystems assumed.
>
> Every claim here is anchored to real code: `[src/routes.rs](../src/routes.rs)`,
> `[src/index.rs](../src/index.rs)`, `[src/store/mod.rs](../src/store/mod.rs)`,
> `[src/object.rs](../src/object.rs)`.

---

## 0. The one sentence to hold onto

**S3 has no folders.** A path like `photos/2024/vacation/beach.jpg` looks like it
walks through four directories, but it is really *one single opaque string* — a
name — with no directory structure behind it at all. Everything that makes it
*feel* like folders (browsing, "open this directory", breadcrumbs) is an illusion
reconstructed on demand from those strings.

If you internalize only that, the rest of this document is just *how* the illusion
is built and *why* it's built that way.

---



## 1. The problem: why not just use real folders?

**The obvious design is: a bucket is a directory, and** `photos/vacation/beach.jpg`
**becomes real nested directories** `photos/` **→** `vacation/` **→** `beach.jpg` **on disk.
Simple, intuitive. And it falls apart at scale for concrete reasons:**


| Real-folders approach                    | What breaks                                                                                           |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `mkdir` per path segment                 | A key like `a/b/c/d/e/f.jpg` means 6 syscalls and 5 directories just to store one file.               |
| Millions of files in one "folder"        | `GET /bucket?prefix=logs/2024-01-01` would `readdir` a directory with 10M entries. Filesystems choke. |
| Renaming a "folder"                      | Users expect `mv photos/ archive/` to be instant. On real folders it's O(number of files).            |
| Deleting a key                           | You'd have to garbage-collect now-empty parent directories. Race conditions everywhere.               |
| Two identical files under different keys | Stored twice. No deduplication.                                                                       |


Object stores sidestep **all** of this by making a radical choice: **the keyspace
is flat.** There is no tree. `a/b/c.jpg` is just a 7-character string that happens
to contain slashes. The slashes have *no special meaning to storage* — they only
mean something to the *listing* algorithm, and only when you ask.

This is stated directly in the code, in `[src/object.rs](../src/object.rs)`:

```rust
/// An object key: the full path within a bucket. It may contain `/`, but the
/// keyspace is **flat** — the slashes are only meaningful to the prefix/delimiter
/// listing in V3, never a real directory tree on disk.
pub type Key = String;
```

---



## 2. The three different "paths" in this system

The word "path" is overloaded. In this project there are **three completely
different** path-like strings, and confusing them is the #1 source of bugs. Keep
them separate:

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  1. HTTP request path      PUT /photos/vacation/beach.jpg       │
   │        │                        └── what the client typed       │
   │        ▼  (axum routing splits it)                              │
   │  2. (bucket, key)          bucket="photos"                      │
   │        │                   key="vacation/beach.jpg"  ← flat name│
   │        ▼  (two independent mappings)                            │
   │  3a. index path            index/photos/objects/vacation%2Fbea..│
   │  3b. blob path             objects/e3/b0/e3b0c44298fc1c14...    │
   └─────────────────────────────────────────────────────────────────┘
```

- **(1) The HTTP path** — the raw URL the client sends. Lives in the request line.
- **(2) The logical name** — the `(bucket, key)` pair. This is the *identity* of
the object. The key is the flat string.
- **(3a) The index path** — where the *pointer* (a small JSON file of metadata)
for that key lives on disk.
- **(3b) The blob path** — where the *bytes* live on disk. Named by the hash of
the content, **not** by the key at all.

The rest of this doc walks (1) → (2) → (3a) / (3b) in order.

---



## 3. Step 1 → 2: HTTP path becomes (bucket, key)

When a client does `PUT /photos/vacation/beach.jpg`, the router in
`[src/routes.rs](../src/routes.rs)` has to decide where the bucket name ends and
the key begins. Look at the route definitions:

```rust
// Bucket-level: create + list.
.route("/{bucket}", put(create_bucket).get(list_objects))
// Object-level: put/get/delete + multipart.
.route("/{bucket}/{*key}", put(put_object).get(get_object)...)
```

The magic is the `*` in `{*key}`. In axum, `{bucket}` matches **one** path segment
(no slashes), but `{*key}` is a **greedy wildcard** — it captures *everything left*,
slashes included. So:

```
   PUT /photos/vacation/beach.jpg
        └────┘ └──────────────────┘
       bucket          key
     "photos"   "vacation/beach.jpg"   ← the slash is captured INTO the key
```

This is exactly why the key can contain slashes: the router deliberately hands the
whole tail to `key` as one string. Two segments become `{bucket}` + `{*key}`; the
key is never split on `/` again — not here, not on disk, not until listing.

**This is "path-style" addressing** (the URL is `host/bucket/key`). Real S3 also
supports "virtual-hosted" style (`bucket.s3.amazonaws.com/key`), where the bucket
is in the hostname instead. This project implements path-style — see the module
doc in `routes.rs`: *"HTTP surface: the path-style S3 API."*

### The bucket/key split, per verb


| Request                 | `{bucket}` | `{*key}`  | Handler         |
| ----------------------- | ---------- | --------- | --------------- |
| `PUT /photos`           | `photos`   | —         | `create_bucket` |
| `GET /photos?prefix=v/` | `photos`   | —         | `list_objects`  |
| `PUT /photos/a/b.jpg`   | `photos`   | `a/b.jpg` | `put_object`    |
| `GET /photos/a/b.jpg`   | `photos`   | `a/b.jpg` | `get_object`    |


Note the same URL shape (`/photos`) means "create the bucket" on `PUT` and "list
the bucket" on `GET`. The verb, not just the path, selects the operation.

---



## 4. The bucket: the only *real* namespace boundary

A key is a free-form string, but a **bucket name** is strict, because unlike the
key it *does* become a real directory on disk. From
`[validate_bucket_name](../src/index.rs)` in `index.rs`:

- 3–63 characters
- only `[a-z0-9-]` (lowercase letters, digits, hyphens)
- no leading or trailing hyphen

Why so strict? Two reasons:

1. **DNS compatibility** — in virtual-hosted style the bucket becomes part of a
  hostname (`my-bucket.s3.amazonaws.com`), and hostnames can't contain `/`, `.`
   segments that break, uppercase, `_`, etc. S3 inherits DNS's rules.
2. **Path-traversal defense** — and this is the clever part. Because `/`, `.`, and
  `_` are all *rejected*, a validated bucket name can only ever be a **single
   directory segment**. It is structurally impossible for a bucket named
   `../../etc` to exist, because `.` and `/` fail validation. The doc comment says
   it plainly:

So the bucket is the one place where a client-controlled string becomes a real
directory — and it's locked down precisely because of that. The key gets a
*different* defense (next section).

---



## 5. Step 2 → 3a: the key becomes ONE filename (percent-encoding)

Here's the crux. The key `vacation/beach.jpg` must be stored as **one flat file**,
not a nested directory. But it contains a `/`, which every filesystem treats as a
directory separator. If we naively wrote a file at that path, the OS would try to
create a `vacation/` directory. That's exactly the folder-tree we're avoiding.

The solution: **percent-encode the key into a single safe filename component.**
From `[encode_key](../src/index.rs)` in `index.rs`:

```rust
fn encode_key(key: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(key.len());
    for byte in key.bytes() {
        match byte {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(byte as char);       // "unreserved" — pass through
            }
            _ => {                                 // everything else → %XX
                encoded.push('%');
                encoded.push(HEX[(byte >> 4) as usize] as char);
                encoded.push(HEX[(byte & 0xf) as usize] as char);
            }
        }
    }
    encoded
}
```

The rule: characters in the "unreserved" set `[A-Za-z0-9-._~]` pass through
unchanged; **every other byte** becomes `%` followed by its two hex digits. Worked
examples:


| Key                  | Encoded filename         | Why                           |
| -------------------- | ------------------------ | ----------------------------- |
| `beach.jpg`          | `beach.jpg`              | all chars unreserved          |
| `vacation/beach.jpg` | `vacation%2Fbeach.jpg`   | `/` is byte `0x2F` → `%2F`    |
| `a/b/c.jpg`          | `a%2Fb%2Fc.jpg`          | every `/` flattened           |
| `my file.txt`        | `my%20file.txt`          | space is `0x20` → `%20`       |
| `../../etc/passwd`   | `..%2F..%2Fetc%2Fpasswd` | the `/` chars are neutralized |


Two things this buys us at once:

1. **Flattening.** `a/b/c.jpg` → `a%2Fb%2Fc.jpg`, a single filename. No
  directories are created. The key stays one opaque name on disk, exactly as the
   flat-keyspace model demands.
2. **Path-traversal defense for keys.** A malicious key like `../../etc/passwd`
  can't escape the bucket directory, because its `/` and (the encoder leaves `.`
   alone, but) crucially the `/` separators become `%2F`. The result
   `..%2F..%2Fetc%2Fpasswd` is a single harmless filename inside the bucket — it
   cannot climb to a parent directory. (Note `.` and `..` alone are safe once the
   slashes are gone: `..` as a *filename* is just a two-character name.)

> ⚠️ **This encoding is the key's security boundary.** The handler comment in
> `routes.rs` flags path traversal as a TODO at the HTTP layer, but `encode_key`
> is the structural backstop: even an un-sanitized key can't write outside its
> bucket once every `/` is `%2F`.

So the on-disk index pointer for `(photos, vacation/beach.jpg)` lands at:

```
index/photos/objects/vacation%2Fbeach.jpg.json
```

Built by `[index_path](../src/index.rs)`:

```rust
fn index_path(&self, bucket: &str, key: &str) -> Result<PathBuf, AppError> {
    Self::validate_bucket_name(bucket)?;                       // bucket safe
    Ok(self.objects_dir(bucket)                                // index/<bucket>/objects
        .join(format!("{}.json", Self::encode_key(key))))      // + <encoded-key>.json
}
```

---



## 6. Step 2 → 3b: the *bytes* live somewhere else entirely

Here's the part that surprises people coming from filesystems: **the key does not
determine where the content is stored.** The bytes are named by their **content
hash**, not their path. This is a *content-addressed store* (CAS).

From `[src/object.rs](../src/object.rs)`:

```rust
/// A content digest: the SHA-256 of an object's bytes, hex-encoded. In a
/// content-addressed store this *is* the blob's name on disk (V1), which is what
/// makes dedup free: identical bytes produce the same digest, stored once.
pub struct Digest(pub String);
```

And `[blob_path](../src/store/mod.rs)` in `store.rs` turns that 64-hex-char digest into
a path:

```rust
pub fn blob_path(&self, digest: &Digest) -> PathBuf {
    self.objects
        .join(&digest.as_str()[0..2])   // first 2 hex chars  → shard dir
        .join(&digest.as_str()[2..4])   // next  2 hex chars  → shard dir
        .join(digest.as_str())          // full 64-char digest → filename
}
```

So content hashing to `e3b0c44298fc1c14...` (64 hex chars) lands at:

```
objects/e3/b0/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
        └┬┘ └┬┘ └──────────────────────────────────────────────────────────────┘
       shard shard                    full digest = filename
```



### Why shard into `e3/b0/`?

If every blob were a direct child of `objects/`, that one directory would hold
*every object in the system* — millions of entries. Most filesystems slow down
badly on huge directories. By taking the first two bytes (`e3`, then `b0`) as
intermediate directories, blobs fan out across `256 × 256 = 65,536` buckets.
Because SHA-256 output is uniformly random, they spread evenly. The comment says:

```rust
/// Map a digest to its on-disk path, fanned out by the leading hash bytes
/// (`objects/ab/cd/abcd…`) so no single directory holds millions of entries.
```



### Why hash-name the content at all? → free deduplication

If two different keys hold identical bytes, they hash to the **same** digest, so
they resolve to the **same** blob path, so the bytes are stored **once**. Upload
the same 4 MB photo under 100 keys and you consume 4 MB, not 400 MB. This is why
`store.rs` calls `contains(digest)` before writing — if the blob's already there,
the upload is a no-op.

This is the payoff of separating "the name" (key) from "the bytes" (digest): the
key can move, duplicate, or be deleted without touching the content, and identical
content is automatically shared.

---



## 7. The mapping that ties it together: (bucket, key) → digest

We now have two independent on-disk trees that don't reference each other:

```
   index/                          objects/
   └── photos/                     └── e3/
       └── objects/                    └── b0/
           └── vacation%2F...json          └── e3b0c44...   ← the bytes
                    │                              ▲
                    │  contains {"digest": "e3b0c44...", ...}
                    └──────────────────────────────────────┘
```

The index pointer is a small JSON file — an
`[ObjectMeta](../src/object.rs)` — that records the digest (plus size, etag,
content-type, last-modified). **That JSON is the only link** from a key to its
bytes:

```rust
pub struct ObjectMeta {
    pub bucket: Bucket,
    pub key: Key,          // "vacation/beach.jpg"
    pub digest: Digest,    // "e3b0c44..."  ← points at the blob
    pub size: u64,
    pub etag: ETag,
    pub content_type: String,
    pub last_modified: DateTime<Utc>,
}
```

So the two-level lookup for `GET /photos/vacation/beach.jpg` (see `get_object` in
`routes.rs`) is:

```
   (bucket, key)  ──index.get()──▶  ObjectMeta { digest }  ──store.open_blob()──▶  bytes
   "photos",                        read the JSON pointer     open objects/e3/b0/e3b0…
   "vacation/beach.jpg"             at index/photos/objects/
                                    vacation%2Fbeach.jpg.json
```

Two hops: **key → digest** (index), then **digest → bytes** (store). The key never
touches the blob tree; the digest never touches the index filename.

---



## 8. Rebuilding the folder illusion: prefix + delimiter

Now the big question: if there are no folders, how does the AWS console show you
folders? How does `aws s3 ls s3://photos/vacation/` work?

The answer: **listing reconstructs folders from the flat key strings, on the fly**,
using two parameters — `prefix` and `delimiter`. This is
`[Index::list](../src/index.rs)`, exposed over HTTP as `GET /{bucket}` with query
params (`list_objects` in `routes.rs`).

Suppose the bucket `photos` holds these flat keys:

```
vacation/beach.jpg
vacation/sunset.jpg
vacation/2024/roadtrip.jpg
profile.png
```



### `prefix` = a filter

`prefix` keeps only keys that **start with** the given string. It's a pure string
`starts_with`, nothing more (`index.rs`: `objects.retain(|m| m.key.starts_with(prefix))`).

```
GET /photos?prefix=vacation/
   → vacation/beach.jpg
     vacation/sunset.jpg
     vacation/2024/roadtrip.jpg
   (profile.png filtered out — doesn't start with "vacation/")
```

With only a prefix, you get a *flat, recursive* listing of everything "under"
`vacation/`, including the deeper `2024/` key. No folders yet.

### `delimiter` = the folder-maker

`delimiter` (almost always `/`) is what synthesizes folders. The algorithm, per
surviving key: strip the prefix, then look at the remainder — **is there another
delimiter in it?**

- **Yes** → this key lives "deeper". Don't list it as an object; instead roll it up
into a **common prefix** = everything up to and including that first delimiter.
- **No** → the remainder is a bare leaf name; list it as a real object.

```
GET /photos?prefix=vacation/&delimiter=/

   key                          remainder (after "vacation/")   has "/"?   result
   ───────────────────────────  ─────────────────────────────   ───────   ────────────────────
   vacation/beach.jpg           beach.jpg                        no        object: vacation/beach.jpg
   vacation/sunset.jpg          sunset.jpg                       no        object: vacation/sunset.jpg
   vacation/2024/roadtrip.jpg   2024/roadtrip.jpg                YES       folder: vacation/2024/

   → objects:        [vacation/beach.jpg, vacation/sunset.jpg]
     commonPrefixes: [vacation/2024/]        ← the synthesized "subfolder"
```

That `commonPrefixes: ["vacation/2024/"]` is exactly what a file browser draws as a
📁 folder. Note **many** keys collapse into one common prefix — if there were 500
keys under `vacation/2024/`, they'd still produce the single folder
`vacation/2024/`. A `HashSet` in the code dedupes them.

To "open" that folder, the client just lists again with the deeper prefix:

```
GET /photos?prefix=vacation/2024/&delimiter=/
   → object: vacation/2024/roadtrip.jpg
```

**The folder never existed.** "Opening" it is just another prefix query. This is
the whole trick: folders are a *view*, recomputed per request from string prefixes,
never a stored structure.

The relevant code:

```rust
if let Some(delim) = delimiter {
    let remainder = meta.key.strip_prefix(prefix).unwrap_or(&meta.key);
    if let Some(idx) = remainder.find(delim) {
        let end = idx + delim.len();
        rolled.insert(format!("{}{}", prefix, &remainder[..end]));   // → common prefix
    } else {
        leaves.push(meta);                                            // → object
    }
}
```

> For a deeper treatment of listing — pagination via continuation tokens, sort
> order, and a known bug where common prefixes bypass `max_keys` — see the `list`
> function and its tests in `[src/index.rs](../src/index.rs)`.

---



## 9. Trailing slashes, empty segments, and other sharp edges

Because keys are opaque strings, several things that *look* the same are actually
different objects. This trips up everyone at first:


| These are **different keys** | Because...                                                    |
| ---------------------------- | ------------------------------------------------------------- |
| `docs` and `docs/`           | The trailing `/` is just another byte in the string.          |
| `a/b.jpg` and `a//b.jpg`     | The empty segment between the slashes is preserved literally. |
| `Photo.JPG` and `photo.jpg`  | Keys are case-sensitive (bucket names are not).               |


Real S3 exploits the "trailing slash is just a character" fact for a UI trick: the
console creates an **empty object** named `myfolder/` (zero bytes, key ends in `/`)
so that an *empty* folder shows up in the listing — otherwise a folder with no
files inside it wouldn't appear at all, since folders only exist as a side-effect
of keys under them. It's a real object masquerading as an empty directory.

---



## 10. End-to-end: one PUT, traced through every path

Let's follow `PUT /photos/vacation/beach.jpg` with a 12-byte body `hello world!`
all the way down, touching every path form:

```
1. HTTP path       PUT /photos/vacation/beach.jpg
                        │
2. Router splits   {bucket}="photos"   {*key}="vacation/beach.jpg"
                        │
3. Stream body →   SHA-256("hello world!") = 7509e5bda0c762d2...
   store (V2/V1)        │
                        ▼
   blob path       objects/75/09/7509e5bda0c762d2...   ← 12 bytes written here
                   (only if not already present — dedup)
                        │
4. Record pointer  ObjectMeta {
   (V3 index)         bucket: "photos",
                      key:    "vacation/beach.jpg",
                      digest: "7509e5bda0c762d2...",
                      size: 12, etag, content_type, last_modified }
                        │
                        ▼
   index path      index/photos/objects/vacation%2Fbeach.jpg.json
                                          └── encode_key("vacation/beach.jpg")
```

Now `GET /photos/vacation/beach.jpg` reverses it: split → read the JSON pointer at
`index/photos/objects/vacation%2Fbeach.jpg.json` → get `digest` → open
`objects/43/0c/430ce34d...` → stream those bytes back.

And `GET /photos?prefix=vacation/&delimiter=/` never touches the blob tree at all —
it just scans the index pointers, filters by prefix, and rolls up deeper keys into
folders.

---



## 11. Mental model summary


| Concept             | What it *looks* like    | What it *actually* is                                             |
| ------------------- | ----------------------- | ----------------------------------------------------------------- |
| Object key          | A file path `a/b/c.jpg` | One flat, opaque string. Slashes are ordinary bytes.              |
| "Folders"           | A directory tree        | A view recomputed per-list from `prefix` + `delimiter`.           |
| Bucket              | A folder                | The one real directory + a DNS-strict, traversal-safe namespace.  |
| Where bytes live    | Under the key's path    | Under `objects/<hash>` — named by content, not by key.            |
| Same file, two keys | Stored twice            | Stored once (same hash → same blob). Dedup.                       |
| Key → bytes         | Direct                  | Two hops: key → digest (index), digest → bytes (store).           |
| Path safety         | Trust the input         | `/` in keys → `%2F` (can't escape); buckets reject `/`, `.`, `_`. |


The single idea underneath all of it: **separate the *name* of a thing from the
*bytes* of a thing, and keep the name flat.** Once you do that, folders become a
query, deduplication becomes free, and path traversal becomes structurally
impossible. Everything in this project's `index.rs` and `store.rs` is an expression
of that one decision.

---



## 12. Where to look in the code


| You want to understand...          | Read...                                                                 |
| ---------------------------------- | ----------------------------------------------------------------------- |
| How a URL becomes `(bucket, key)`  | `router()` and the handlers in `[src/routes.rs](../src/routes.rs)`      |
| Why keys can hold slashes safely   | `encode_key` in `[src/index.rs](../src/index.rs)`                       |
| Why buckets are locked down        | `validate_bucket_name` in `[src/index.rs](../src/index.rs)`             |
| How content is addressed & sharded | `blob_path` / `digest_from_path` in `[src/store/mod.rs](../src/store/mod.rs)`   |
| How the key→bytes link is stored   | `ObjectMeta` in `[src/object.rs](../src/object.rs)`, `Index::put`/`get` |
| How folders are faked              | `Index::list` in `[src/index.rs](../src/index.rs)`                      |


