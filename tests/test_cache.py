"""Tests for the small TTL LRU cache used by the tool server."""

import time
import unittest

from codegraph.cache import TTLCache


class TTLCacheTest(unittest.TestCase):
    def test_hit_and_miss(self):
        cache = TTLCache(ttl=10.0, max_size=8)
        self.assertIsNone(cache.get("k"))
        cache.put("k", "v")
        self.assertEqual(cache.get("k"), "v")

    def test_ttl_expiry(self):
        cache = TTLCache(ttl=0.05, max_size=8)
        cache.put("k", "v")
        time.sleep(0.08)
        self.assertIsNone(cache.get("k"))

    def test_size_eviction(self):
        cache = TTLCache(ttl=10.0, max_size=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)  # evicts oldest: a
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_refresh_on_put(self):
        cache = TTLCache(ttl=10.0, max_size=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 1)  # refresh makes a newest
        cache.put("c", 3)  # evicts b now
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))

    def test_falsy_values_cached(self):
        cache = TTLCache(ttl=10.0, max_size=2)
        cache.put("empty", [])
        self.assertEqual(cache.get("empty"), [])

    def test_clear(self):
        cache = TTLCache(ttl=10.0, max_size=8)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.clear()
        self.assertIsNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
