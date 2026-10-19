"""Decoder for Next.js App Router flight data (the React Server Components payload) embedded in HTML.

Getmobil renders its pages with the Next.js App Router (so did Easycep, dropped on 14.10.2026). The page data is not
in the visible HTML but in inline scripts that stream the RSC payload::

    <script>self.__next_f.push([1,"0:{...}\\n3:I[68172,[],\\"\\"]\\n"])</script>

Each push is a JSON array. ``[1, "<text>"]`` carries a piece of the flight stream as a JSON string literal and
``[3, "<base64>"]`` a binary piece; ``[0]`` (bootstrap) and ``[2, ...]`` (form state) carry no page data. Joined in
order, the pieces form a stream of rows, as React's flight client reads them:

* ``<hex id>:<json>`` is a model row; the page data lives in these;
* ``<hex id>:T<hex length>,<text>`` is a text row. The length counts UTF-8 bytes, so the text is cut by byte count and
  may contain newlines (JSON-LD sits here);
* ``<hex id>:<tag><json>`` is a tagged row: ``I`` (client module), ``HL`` (resource hint, written with an empty id by
  newer Next.js versions), ``E`` (error) and so on;
* ``<hex id>:X`` and ``<hex id>:C`` open and close a stream and may carry no payload.

Model rows point at each other with strings: ``"$4d"`` is row ``4d`` and ``"$4d:props:children:0:props:priceInfo"``
walks into it, where ``props`` on a React element array ``["$", type, key, props]`` means index 3. ``"$L4d"`` (lazy)
and ``"$@4d"`` (promise) also name rows. Row ids differ from page to page and from deploy to deploy, so callers find
data by key name (``find_dicts``, ``first_value``), never by row id.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

PUSH_CALL = "self.__next_f.push("
# Tags whose rows carry a "<hex length>," prefix and exactly that many bytes (text and binary typed arrays).
LENGTH_TAGS = frozenset(b"TAOoUSsLlGgMmV")
# Tags whose rows run to the end of the line. Hint rows use "H" followed by a one-letter hint code ("HL").
LINE_TAGS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ#rx")
REFERENCE = re.compile(r"\$[L@]?([0-9a-f]+)((?::[^:]*)*)")
ELEMENT_FIELDS = {"type": 1, "key": 2, "props": 3}
MAX_DEPTH = 400  # deeper trees are left unresolved instead of exhausting the Python stack
MAX_HOPS = 64  # reference-to-reference chains longer than this are treated as cycles


class FlightError(ValueError):
    """The page does not contain well-formed flight data."""


@dataclass(frozen=True)
class Row:
    """One flight row. ``value`` is parsed JSON, the text of a ``T`` row, or None for rows without payload."""

    id: str
    tag: str
    value: Any


def flight_bytes(html: str) -> bytes:
    """Join the pushed flight pieces of a page, in order, as the bytes React's flight client would read."""
    decoder = json.JSONDecoder()
    pieces: list[bytes] = []
    start = html.find(PUSH_CALL)
    while start >= 0:
        try:
            chunk, end = decoder.raw_decode(html, start + len(PUSH_CALL))
        except json.JSONDecodeError as err:
            raise FlightError(f"unreadable flight push at offset {start}: {err}") from err
        if not isinstance(chunk, list) or not chunk:
            raise FlightError(f"unexpected flight push at offset {start}")
        kind = chunk[0]
        if kind == 1 and len(chunk) > 1 and isinstance(chunk[1], str):
            pieces.append(chunk[1].encode("utf-8"))
        elif kind == 3 and len(chunk) > 1 and isinstance(chunk[1], str):
            try:
                pieces.append(base64.b64decode(chunk[1], validate=True))
            except binascii.Error as err:
                raise FlightError(f"invalid binary flight push at offset {start}") from err
        start = html.find(PUSH_CALL, end)
    return b"".join(pieces)


def parse_rows(data: bytes) -> list[Row]:
    """Split a flight stream into rows, following the row grammar of React's flight client."""
    rows: list[Row] = []
    pos, size = 0, len(data)
    while pos < size:
        if data[pos] == 0x0A:  # tolerate blank lines
            pos += 1
            continue
        colon = data.find(b":", pos)
        row_id = data[pos:colon].decode("ascii", "replace") if colon >= 0 else ""
        if colon < 0 or (row_id and not all(c in "0123456789abcdef" for c in row_id)):
            raise FlightError(f"malformed row at byte {pos}: {data[pos : pos + 40]!r}")
        start = colon + 1
        tag_byte = data[start] if start < size else 0x0A
        if tag_byte in LENGTH_TAGS:
            comma = data.find(b",", start + 1)
            try:
                length = int(data[start + 1 : comma], 16)
            except ValueError as err:
                raise FlightError(f"malformed length in row {row_id!r}") from err
            body = data[comma + 1 : comma + 1 + length]
            if len(body) != length:
                raise FlightError(f"row {row_id!r} is truncated")
            value: Any = body.decode("utf-8", "replace") if tag_byte == ord("T") else body
            rows.append(Row(row_id, chr(tag_byte), value))
            pos = comma + 1 + length
            continue
        end = data.find(b"\n", start)
        if end < 0:
            end = size
        tag = ""
        if tag_byte in LINE_TAGS:
            tag = chr(tag_byte)
            start += 1
            if tag == "H" and start < end:  # hint rows carry a one-letter code: "HL[...]"
                tag += chr(data[start])
                start += 1
        payload = data[start:end]
        rows.append(Row(row_id, tag, _parse_json(payload, row_id) if payload.strip() else None))
        pos = end + 1
    return rows


def _parse_json(payload: bytes, row_id: str) -> Any:
    try:
        return json.loads(payload)
    except ValueError as err:
        raise FlightError(f"row {row_id!r} is not valid JSON: {err}") from err


class Flight:
    """The decoded flight rows of one page, with reference resolution and key search."""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self._by_id: dict[str, Row] = {}
        for row in rows:
            # A streamed row id can repeat (X, chunks, C); the first model or text row is the one references mean.
            if row.tag in ("", "T") and row.id not in self._by_id:
                self._by_id[row.id] = row

    @classmethod
    def from_html(cls, html: str) -> Flight:
        data = flight_bytes(html)
        if not data:
            raise FlightError("the page contains no flight data")
        return cls(parse_rows(data))

    def row(self, row_id: str) -> Row | None:
        return self._by_id.get(row_id)

    def texts(self) -> list[str]:
        """The text rows (T) in stream order."""
        return [row.value for row in self.rows if row.tag == "T"]

    def resolve(self, value: Any) -> Any:
        """Follow a reference string to its target, one level deep. Anything else is returned unchanged."""
        return self._follow(value)[0]

    def resolve_deep(self, value: Any) -> Any:
        """Return ``value`` with every reference inside it replaced by its target, recursively.

        A reference whose target encloses a value that is being resolved (a cycle) is left as the reference string,
        and so is a reference to a missing row or to a client module. References to other parts of the same row are
        not cycles: a page can keep a list of sellers and the sellers it points to in one row.
        """
        return self._deep(value, (), 0)

    def find_dicts(self, key: str) -> Iterator[dict[str, Any]]:
        """Yield every dict that has ``key``, from all model rows in stream order, with references resolved."""
        for row in self.rows:
            if row.tag == "" and row.value is not None:
                for node in find_dicts(row.value, key):
                    yield self.resolve_deep(node)

    def first_value(self, key: str, *, kind: type | tuple[type, ...] = dict) -> Any:
        """The first resolved value of ``key`` that has the expected type (a dict by default), or None.

        Pages often hold both the data and references to it (``"$4d:props:priceInfo"``); unresolvable references and
        placeholders such as ``"$undefined"`` are skipped.
        """
        for row in self.rows:
            if row.tag != "" or row.value is None:
                continue
            for node in find_dicts(row.value, key):
                value = self.resolve_deep(node[key])
                if isinstance(value, kind):
                    return value
        return None

    def _follow(self, value: Any, seen: set[str] | None = None) -> tuple[Any, Location | None]:
        """Resolve a reference chain at its top level; returns (target, location of the target).

        ``seen`` holds the reference strings already followed, so a chain that loops ends instead of spinning.
        """
        seen = set() if seen is None else seen
        location: Location | None = None
        for _hop in range(MAX_HOPS):
            if not isinstance(value, str):
                return value, location
            match = REFERENCE.fullmatch(value)
            if match is None:
                return _special(value), location
            row = self._by_id.get(match.group(1))
            if row is None or value in seen:
                return value, location
            seen.add(value)
            path = tuple(filter(None, match.group(2).split(":")))
            target = row.value
            for segment in path:
                target, _ = self._follow(target, seen)
                target = _step(target, segment)
                if target is _MISSING:
                    return value, None
            location = (row.id, path)
            value = target
        return value, location

    def _deep(self, value: Any, stack: tuple[Location, ...], depth: int) -> Any:
        """``stack`` holds the locations being resolved; a target that encloses one of them would recurse forever."""
        if depth > MAX_DEPTH:
            return value
        if isinstance(value, str):
            target, location = self._follow(value)
            if location is None:
                return target
            if any(_encloses(location, inner) for inner in stack):
                return value
            return self._deep(target, (*stack, location), depth + 1)
        if isinstance(value, dict):
            return {key: self._deep(item, stack, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self._deep(item, stack, depth + 1) for item in value]
        return value


Location = tuple[str, tuple[str, ...]]  # (row id, path segments) of a reference target
_MISSING = object()


def _encloses(outer: Location, inner: Location) -> bool:
    """True if the subtree at ``outer`` contains the one at ``inner`` (same row, ``outer`` path is a prefix)."""
    return outer[0] == inner[0] and inner[1][: len(outer[1])] == outer[1]


def _step(node: Any, segment: str) -> Any:
    """One path step of a reference. On a React element array, "props" (and "type", "key") select a position."""
    if isinstance(node, dict):
        return node.get(segment, _MISSING)
    if isinstance(node, list):
        if node and node[0] == "$" and segment in ELEMENT_FIELDS:
            index = ELEMENT_FIELDS[segment]
            return node[index] if index < len(node) else _MISSING
        if segment.isdigit() and int(segment) < len(node):
            return node[int(segment)]
    return _MISSING


def _special(value: str) -> Any:
    """Plain values that React encodes as "$" strings: "$undefined", escaped dollars ("$$5") and dates ("$D...")."""
    if not value.startswith("$") or value == "$":
        return value
    if value == "$undefined":
        return None
    if value.startswith("$$"):
        return value[1:]
    if value.startswith("$D"):
        return value[2:]
    return value


def find_dicts(tree: Any, key: str) -> Iterator[dict[str, Any]]:
    """Yield every dict in a JSON-like tree that has ``key``, depth first in document order."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
