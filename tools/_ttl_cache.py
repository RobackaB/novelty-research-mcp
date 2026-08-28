"""Jednoduchá TTL cache s obmedzenou veľkosťou pre dlhobežiaci server."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Cache s časovou platnosťou položiek a maximálnym počtom záznamov.

    Po prekročení kapacity sa odstráni najstaršia položka, takže pamäť
    dlhobežiaceho servera nerastie bez obmedzenia.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 128) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        """Vráti platnú hodnotu z cache alebo None pri chýbajúcej či expirovanej položke."""
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
        """Uloží hodnotu a pri preplnení odstráni najstaršie položky."""
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """Vyprázdni celú cache."""
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
