"""A simple size-bounded TTL cache for a long-running server."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """A cache with per-entry expiry and a maximum number of records.

    Once capacity is exceeded the oldest entry is evicted, so the memory of a
    long-running server does not grow without bound.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 128) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        """Return a valid cached value, or None if the entry is missing or expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self._ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        """Store a value, evicting the oldest entries when the cache is full."""
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """Empty the whole cache."""
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
