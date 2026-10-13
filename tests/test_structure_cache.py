from __future__ import annotations

import unittest

from literature import structure as structure_module


class StructureRequestCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        structure_module._STRUCTURE_CACHE.clear()
        structure_module._STRUCTURE_CACHE_ORDER.clear()

    def test_cache_hit_and_invalidation_on_context_change(self) -> None:
        structure_module._structure_cache_put("key", "ctx-a", "content", {"model": "a"})
        hit = structure_module._structure_cache_get("key", "ctx-a")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["content"], "content")
        # 上下文（模型配置/文档版本）变化 → 立即失效。
        self.assertIsNone(structure_module._structure_cache_get("key", "ctx-b"))

    def test_cache_is_bounded(self) -> None:
        for index in range(structure_module._STRUCTURE_CACHE_MAX + 50):
            structure_module._structure_cache_put(f"key-{index}", "ctx", "c", {})
        self.assertLessEqual(len(structure_module._STRUCTURE_CACHE), structure_module._STRUCTURE_CACHE_MAX)

    def test_expired_entry_is_dropped(self) -> None:
        structure_module._structure_cache_put("key", "ctx", "c", {})
        entry = structure_module._STRUCTURE_CACHE["ctx:key"]
        entry["created_at"] = entry["created_at"] - structure_module._STRUCTURE_CACHE_TTL_SECONDS - 1
        self.assertIsNone(structure_module._structure_cache_get("key", "ctx"))


if __name__ == "__main__":
    unittest.main()
