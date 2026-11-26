from __future__ import annotations

import errno
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vendor_file.pipeline import ensure_free_disk, prune_rapidapi_cache


def _touch(path: Path, age_days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


class PruneRapidApiCacheTests(unittest.TestCase):
    def test_removes_only_old_profile_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "rapidapi_json"
            old_person = cache / "person" / "old.json"
            new_person = cache / "person" / "new.json"
            old_company = cache / "company" / "old.json"
            checkpoint = cache / "checkpoint.json"
            tmp_file = cache / "person" / "partial.json.tmp"
            _touch(old_person, age_days=30)
            _touch(new_person, age_days=1)
            _touch(old_company, age_days=30)
            _touch(checkpoint, age_days=30)
            _touch(tmp_file, age_days=30)

            removed = prune_rapidapi_cache(max_age_days=7, cache_dir=cache)

            self.assertEqual(removed, 2)
            self.assertFalse(old_person.exists())
            self.assertFalse(old_company.exists())
            self.assertTrue(new_person.exists())
            self.assertTrue(checkpoint.exists())
            self.assertTrue(tmp_file.exists())

    def test_missing_cache_dir_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(prune_rapidapi_cache(cache_dir=Path(tmp) / "nope"), 0)


class EnsureFreeDiskTests(unittest.TestCase):
    def test_passes_when_enough_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ensure_free_disk(Path(tmp), min_free_mb=1)

    def test_raises_enospc_when_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = type("Usage", (), {"free": 10 * 1024 * 1024})()
            with patch("vendor_file.pipeline.shutil.disk_usage", return_value=fake):
                with self.assertRaises(OSError) as ctx:
                    ensure_free_disk(Path(tmp), min_free_mb=150)
            self.assertEqual(ctx.exception.errno, errno.ENOSPC)
            self.assertIn("10 MB free", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
