"""V2 - Metainfo & the infohash: identity without a registry.
`src/bittorrent/metainfo.py`.

Parse a `.torrent` (bencoded, V1) into typed fields, and compute the **infohash
= SHA-1 of the exact bytes of the `info` dictionary**. That 20-byte hash *is*
the torrent's identity. There is no registry that hands them out and no
authority that says two files are the same — two clients agree they are talking
about the same content because they independently hashed the same bytes. Get the
bytes wrong (re-encode instead of using the original span) and your infohash
matches nobody, every tracker reports zero peers, and nothing about the error
tells you why.

Also parse `magnet:?xt=urn:btih:<hash>&tr=<tracker>&dn=<name>` links. A magnet
carries the infohash and some trackers but **not** the metainfo — for a magnet
you learn the piece length and hashes later, from peers (BEP 9). So a freshly
parsed magnet legitimately has no `piece_hashes` at all, and every consumer has
to cope with that rather than assuming a torrent always knows its own shape.

*Concept to internalize:* content-addressing — identity derived from bytes — and
the single-file vs multi-file layouts.

## SHA-1, and why you cannot substitute something better

SHA-1 has been collision-broken since 2017 and you should not choose it for
anything new. It is not a choice here: the infohash is *defined* as SHA-1 by
BEP 3, and swapping in SHA-256 produces a client that talks to nobody. That is
the difference between a hash used as a **security primitive** and one used as a
**content identifier**, and it is worth being able to state, because the same
distinction decides whether a "we still use SHA-1" finding in a review is a real
problem or a misread of the protocol.

One Python consequence: on a FIPS-enabled build `hashlib.sha1()` raises
`ValueError` outright, and `hashlib.sha1(data, usedforsecurity=False)` is the
spelling that keeps working. Writing it that way also documents, at the call
site, which of the two things you are doing.

## What "consistency-checked" is protecting you from

Bencode that parses is not a torrent that makes sense. A `.torrent` claiming
40 piece hashes for a file that is 10 pieces long is syntactically perfect, and
believing it means allocating a piece table with 30 entries no peer will ever
fill — a download that hangs at 25% forever with nothing in the logs. Structure
(V1) and meaning (here) fail in different places and for different reasons, and
checking meaning at the parse boundary is what keeps every later module able to
trust the `Metainfo` it was handed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .types import InfoHash

__all__ = ["FileEntry", "MagnetLink", "Metainfo", "safe_relative_path"]

PIECE_HASH_LEN = 20
"""One SHA-1 digest per piece. The `pieces` field is these concatenated with no
separator, so its length must be an exact multiple of 20 — a `pieces` string
that is not is a torrent to reject, not to round down."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file inside a (possibly multi-file) torrent.

    `path` is kept as **raw byte components**, exactly as the torrent gave them,
    and deliberately not turned into a `Path` here. Building a filesystem path
    out of attacker-supplied bytes is the security boundary this project is
    graded on, and it happens in one place — `safe_relative_path` — so that
    there is exactly one function to get right and to test. A `FileEntry` whose
    `path` were already a `Path` would be a `FileEntry` that had already made
    the dangerous decision somewhere you were not looking.
    """

    path: tuple[bytes, ...]
    """Relative path components, as they appeared in `info.files[].path`."""

    length: int


@dataclass(frozen=True, slots=True)
class Metainfo:
    """A parsed `.torrent`."""

    name: str
    """The suggested filename, or the root directory for a multi-file torrent.

    A display string — and note that it is *also* a path component when the
    torrent is multi-file, which means it goes through `safe_relative_path` like
    any other. A torrent named `../../etc` is not a hypothetical."""

    announce: str | None
    """The primary tracker, if the torrent names one."""

    announce_list: tuple[str, ...]
    """Additional trackers from `announce-list`, flattened.

    BEP 12 nests these in tiers — a list of lists, where the inner grouping
    carries a fallback ordering. Flattening loses that ordering, which is a
    reasonable simplification to start with and a real one to notice: it is why
    a client that flattens can hammer a slow tracker that the tiering was trying
    to demote."""

    piece_length: int
    """Bytes per piece. Every piece is exactly this long except the last."""

    piece_hashes: tuple[bytes, ...]
    """One 20-byte SHA-1 per piece, in order — `info.pieces` split into 20s.
    Empty for a magnet that has not yet fetched its metadata from peers."""

    files: tuple[FileEntry, ...]
    """One entry for a single-file torrent, N for a multi-file one."""

    total_length: int
    """The sum of every file's length."""

    info_hash: InfoHash
    """SHA-1 of the exact `info` bytes."""

    @property
    def piece_count(self) -> int:
        return len(self.piece_hashes)

    @property
    def is_multi_file(self) -> bool:
        return len(self.files) > 1

    def piece_size(self, index: int) -> int:
        """The length of piece `index`, which is `piece_length` for every piece
        but the last.

        Wired, because getting it wrong is not interesting — it is just the
        off-by-one that makes the final piece fail its SHA-1 check while every
        other piece passes, and then you go looking for the bug in your hashing.
        """
        if not 0 <= index < self.piece_count:
            raise IndexError(f"piece {index} out of range (have {self.piece_count})")
        if index < self.piece_count - 1:
            return self.piece_length
        remainder = self.total_length % self.piece_length
        return remainder or self.piece_length

    @classmethod
    def from_bytes(cls, raw: bytes) -> Metainfo:
        """Parse a `.torrent`'s bytes into a validated `Metainfo`.

        TODO(V2): decode `raw` with `bencode.decode_with_spans`, then pull
        `announce` / `announce-list`, and out of `info`: `name`, `piece length`,
        `pieces` (split into 20-byte digests), and the file list — single-file
        torrents carry `info.length`, multi-file ones carry
        `info.files[].{length, path}`.

        Then compute the infohash from the **span**, not from a re-encode::

            info_hash = InfoHash(hashlib.sha1(raw[spans[(b"info",)]],
                                              usedforsecurity=False).digest())

        Finish by calling `check_consistency` — a `Metainfo` that leaves this
        method has been validated, and every module downstream is written
        assuming exactly that.

        Two Python details that bite here. Bencode gives you `bytes` keys, so it
        is `info[b"piece length"]` and a `KeyError` on the `str` spelling; catch
        `KeyError` and raise `InvalidTorrent` naming the missing field, because
        "which key was missing" is the entire diagnostic. And splitting `pieces`
        is `[blob[i:i + 20] for i in range(0, len(blob), 20)]` — check
        `len(blob) % 20 == 0` first, since slicing past the end silently gives
        you a short final digest instead of an error.
        """
        raise NotImplementedError("V2: parse the torrent and SHA-1 the exact info bytes")

    def check_consistency(self) -> None:
        """Verify the parse means something, raising `InvalidTorrent` if not.

        TODO(V2): assert `piece_count == ceil(total_length / piece_length)`,
        that every entry in `piece_hashes` is exactly 20 bytes, that
        `total_length == sum(f.length for f in files)`, and that `piece_length`
        is positive. A doctored torrent is rejected here, not trusted and
        discovered later.

        `math.ceil(a / b)` goes through a float and is wrong for large torrents
        — a 4 GiB file's byte count is well past the point where float division
        is exact. `-(-a // b)` stays in integers and is right at every size.

        A magnet-derived `Metainfo` has no pieces yet, so decide what this means
        for it: skipping the check entirely when `piece_hashes` is empty is
        defensible, silently passing a torrent that claims 0 pieces for a 4 GiB
        file is not, and telling those two cases apart is the whole judgement.
        """
        raise NotImplementedError("V2: verify piece count, hash lengths, and total length")


@dataclass(frozen=True, slots=True)
class MagnetLink:
    """A parsed `magnet:` link — enough to start finding peers, and no more."""

    info_hash: InfoHash
    trackers: tuple[str, ...]
    name: str | None

    @classmethod
    def parse(cls, uri: str) -> MagnetLink:
        """Parse `magnet:?xt=urn:btih:<hash>&tr=<url>&dn=<name>`.

        TODO(V2): split the query, require an `xt` of `urn:btih:`, decode the
        hash, collect every `tr=` tracker, and take the optional `dn=` display
        name. Anything else raises `BadRequest`.

        **The hash comes in two spellings and real magnets use both**: 40
        characters of hex, or 32 characters of base32. Both decode to the same 20
        bytes, and a client that only handles hex fails on roughly half the
        magnets it meets. Length is what tells them apart — 40 vs 32 — and
        `base64.b32decode(text.upper())` is the second one, uppercased because
        it rejects lowercase input outright.

        `urllib.parse.urlsplit` handles the `magnet:` scheme fine and puts
        everything after the `?` in `.query`; `parse_qs` percent-decodes the
        tracker URLs for you and, by default, **drops empty values** — which is
        the difference between "no `dn`" and "`dn=`" if you care. `parse_qs`
        also returns a list per key, which is exactly right for `tr` (magnets
        routinely carry several) and something to collapse for `xt`.
        """
        raise NotImplementedError("V2: parse a magnet URI (xt=urn:btih hex|base32, tr, dn)")


def safe_relative_path(components: Sequence[bytes], root: Path) -> Path:
    """Turn attacker-supplied path components into a path **inside** `root`,
    or raise `InvalidTorrent`.

    TODO(security · path traversal): this is the horizontal checklist item, and
    it is the one function in the project where a bug writes to a file the user
    did not ask for. A hostile `.torrent` can put anything in `info.files[].path`
    — `..`, an absolute path, a NUL byte, a Windows drive letter — and the
    criterion is that none of it escapes the download directory.

    What to reject, and why each one is on the list:

    * an empty component, `.`, or `..` — the direct traversal;
    * a component containing `/`, `\\`, or a NUL byte — `b"a/../../etc"` is
      *one* component as far as the torrent is concerned, and joining it
      re-introduces the separators you just checked for;
    * a Windows drive or UNC prefix (`C:`, a leading `\\\\`), which is
      traversal on one platform and a strange filename on another;
    * an empty component list.

    Then join under `root` and **verify the result independently**:
    `resolved.is_relative_to(root.resolve())`. Belt and braces on purpose — the
    component checks are a denylist, and the containment check is the property
    you actually want, so failing either is a rejection.

    Two Python-specific traps worth internalizing:

    `Path.resolve()` follows symlinks, which is what makes it the right final
    check (a symlink already inside `root` pointing at `/etc` is exactly the
    attack a purely textual check misses) — and also why the containment test
    has to run on the resolved form of *both* sides.

    `Path.joinpath` and `/` **discard everything to the left when the right side
    is absolute**: `Path("/downloads") / "/etc/passwd"` is `Path("/etc/passwd")`,
    silently. That single line of surprising behaviour is most of why this
    function exists.

    Decoding the bytes to `str` is part of the job and part of the danger: pick
    an explicit codec and an explicit error handler rather than letting the
    default decide, and remember the torrent's `name` is a path component too
    when the torrent is multi-file.
    """
    raise NotImplementedError("security: sanitize torrent path components before joining")
