"""Small TTL LRU cache used by the tool server for repeated read queries."""

from __future__ import annotations

import time
from collections import OrderedDict


class TTLCache:
    """Ordered-dict LRU with per-entry expiry; oldest entries evicted first."""

    def __init__(self, ttl: float = 30.0, max_size: int = 128):
        self.ttl = ttl
        self.max_size = max_size
        self._data = OrderedDict()  # key -> (expires_at, value)

    def get(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.monotonic() >= expires:
            del self._data[key]
            return None
        return value

    def put(self, key, value):
        now = time.monotonic()
        if key in self._data:
            del self._data[key]
        self._data[key] = (now + self.ttl, value)
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def clear(self):
        self._data.clear()

    def __len__(self):
        return len(self._data)
