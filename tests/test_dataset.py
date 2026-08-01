import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import dataset


class DatasetToolTests(unittest.TestCase):
    def test_safe_archive_path_rejects_traversal(self) -> None:
        for name in ("../escape.txt", "/absolute.txt", "demo/../../escape.txt", "C:/x"):
            with self.subTest(name=name):
                with self.assertRaises(dataset.DatasetError):
                    dataset.safe_archive_path(name, "demo")

    def test_pack_split_install_and_deep_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "demo"
            source.mkdir()
            # 每个文件按未压缩大小接近 600 KiB，可稳定触发多个 1 MiB 独立 ZIP。
            for index in range(4):
                (source / f"file-{index}.bin").write_bytes(bytes([index]) * 600_000)

            assets = root / "assets"
            install_root = root / "installed"
            spec = dataset.DatasetSpec(
                name="demo",
                description="test",
                archive_root="demo",
                publish_enabled=True,
                upstream="test",
            )
            with mock.patch.object(dataset, "ASSET_DIR", assets):
                manifest = dataset.pack_dataset(
                    spec,
                    source,
                    1024 * 1024,
                )
                self.assertGreater(len(manifest["archive"]["parts"]), 1)
                for part in manifest["archive"]["parts"]:
                    self.assertTrue(zipfile.is_zipfile(assets / part["name"]))
                loaded, paths = dataset.validate_local_bundle("demo", assets)
                dataset.install_bundle(loaded, paths, install_root)
                count, total = dataset.verify_dataset_tree(
                    loaded,
                    install_root,
                    deep=True,
                )

            self.assertEqual(count, 4)
            self.assertEqual(total, 2_400_000)
            self.assertEqual(manifest["source"]["file_count"], 4)

    def test_upload_is_blocked_before_license_review(self) -> None:
        spec = dataset.DatasetSpec(
            name="demo",
            description="test",
            archive_root="demo",
            publish_enabled=False,
            upstream="test",
        )
        with self.assertRaisesRegex(dataset.DatasetError, "许可复核"):
            dataset.upload_dataset(
                spec,
                {"release_tag": "datasets"},
                "owner/repo",
            )


if __name__ == "__main__":
    unittest.main()
