"""The identifiers every plane shares — **wired**, not a vertical.

Three names cross module boundaries in this project, and each would otherwise be
spelled three different ways (a `"room/42"` string in one place, a tuple in
another, a dataclass in a third) until two of them disagreed about equality:

* `Address` — a UDP endpoint, normalised to `(host, port)`. The loop hands a
  datagram's source over as a 2-tuple for IPv4 and a 4-tuple for IPv6; it is
  squashed to two fields once, at the socket, so that a peer lookup keyed on an
  address compares like with like.
* `TrackKey` — one publisher's stream, `(room_id, publisher_id)`. The unit a
  relay leg carries (V2), a layer set is routed for (V3) and a recording writes
  (V4). A tuple rather than a formatted string so it hashes, compares and
  destructures without anyone parsing it back apart.
* `LayerId` — a simulcast layer's index in the publisher's ladder, `0` = lowest.
  The same ids project 15's selector picks between.
"""

from __future__ import annotations

__all__ = ["Address", "LayerId", "TrackKey"]

type Address = tuple[str, int]
type TrackKey = tuple[str, int]
type LayerId = int
