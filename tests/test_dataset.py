import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import dataset


class LegacyNameZipInfo(zipfile.ZipInfo):
    """测试用：模拟未设置 UTF-8 标志、文件名按 GBK 写入的旧 ZIP。"""

    def _encodeFilenameFlags(self):  # type: ignore[no-untyped-def]
        return self.filename.encode("cp437"), self.flag_bits & ~0x800


class FakeHttpResponse:
    def __init__(self, content: bytes, start: int, total: int) -> None:
        self.stream = io.BytesIO(content)
        self.status = 206
        self.headers = {
            "Content-Range": f"bytes {start}-{total - 1}/{total}",
            "Content-Length": str(len(content)),
        }

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.stream.close()


class DatasetToolTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "仅验证 Windows 长路径读取")
    def test_directory_source_reads_windows_long_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "demo"
            source.mkdir()
            long_path = source / ("x" * 220 + ".bin")
            extended_path = Path("\\\\?\\" + str(long_path.resolve()))
            extended_path.write_bytes(b"long-path")
            try:
                with dataset.DirectoryDatasetSource(source) as data_source:
                    entry = data_source.entries[0]
                    self.assertGreater(len(str(entry.token)), 260)
                    with data_source.open_entry(entry) as file_obj:
                        self.assertEqual(file_obj.read(), b"long-path")
            finally:
                extended_path.unlink(missing_ok=True)

    def test_large_file_progress_is_compact(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            for index in range(1, 269_912):
                dataset.print_file_progress(
                    "打包进度",
                    index,
                    269_911,
                    part=1,
                    part_total=5,
                )

        lines = output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 11)
        self.assertIn("100.0%", lines[-1])
        self.assertNotIn(".jpg", output.getvalue())

    def test_interactive_menu_is_goal_oriented(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("builtins.input", side_effect=["", "0"]),
            redirect_stdout(output),
        ):
            dataset.interactive()

        menu = output.getvalue()
        self.assertIn("下载并安装数据集（首次使用）", menu)
        self.assertIn("查看状态或验证已安装数据", menu)
        self.assertIn("转换文件夹或 ZIP（高级功能）", menu)
        self.assertIn("制作或上传数据包（发布者）", menu)
        self.assertNotIn("检查原始数据", menu)
        self.assertIn("或输入 0 退出", menu)

    def test_check_submenu_distinguishes_status_from_integrity(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("builtins.input", side_effect=["2", "0", "0"]),
            redirect_stdout(output),
        ):
            dataset.interactive()

        menu = output.getvalue()
        self.assertIn("查看原始文件、本机数据包和 GitHub 发布状态", menu)
        self.assertIn("验证已安装的数据目录是否完整", menu)
        self.assertGreaterEqual(menu.count("CMR 原始数据集工具"), 2)

    def test_regular_install_uses_published_release_directly(self) -> None:
        spec = dataset.DatasetSpec(
            name="demo",
            description="test",
            archive_root="demo",
            publish_enabled=True,
            upstream="test",
        )
        settings = {"repository": "owner/repo", "release_tag": "datasets"}
        manifest = {"dataset": "demo"}
        parts = [Path("part.zip")]
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp) / "installed"
            output = io.StringIO()
            with (
                mock.patch.object(
                    dataset,
                    "published_dataset_specs",
                    return_value={"demo": spec},
                ),
                mock.patch.object(dataset, "choose_dataset", return_value=spec),
                mock.patch.object(dataset, "prompt_path", return_value=output_root),
                mock.patch.object(
                    dataset,
                    "prepare_remote_bundle",
                    return_value=(manifest, parts),
                ),
                mock.patch.object(dataset, "install_bundle") as install,
                mock.patch.object(
                    dataset,
                    "choose_menu_option",
                    side_effect=AssertionError("普通安装不应再询问数据来源"),
                ),
                redirect_stdout(output),
            ):
                dataset.interactive_install(settings, {"demo": spec})

        self.assertNotIn("安装来源", output.getvalue())
        install.assert_called_once_with(manifest, parts, output_root, force=False)

    def test_dataset_menu_explains_each_choice(self) -> None:
        specs = {
            "demo": dataset.DatasetSpec(
                name="demo",
                description="通用演示数据集",
                archive_root="demo",
                publish_enabled=False,
                upstream="demo",
            )
        }
        output = io.StringIO()
        with (
            mock.patch("builtins.input", return_value="0"),
            redirect_stdout(output),
        ):
            selected = dataset.choose_dataset(specs, show_upload_status=True)

        self.assertIsNone(selected)
        menu = output.getvalue()
        self.assertIn("通用演示数据集", menu)
        self.assertIn("可制作；GitHub 上传暂未开放", menu)

    def test_dataset_menu_hides_upload_status_for_unrelated_actions(self) -> None:
        specs = {
            "demo": dataset.DatasetSpec(
                name="demo",
                description="通用演示数据集",
                archive_root="demo",
                publish_enabled=False,
                upstream="demo",
            )
        }
        output = io.StringIO()
        with (
            mock.patch("builtins.input", return_value="0"),
            redirect_stdout(output),
        ):
            dataset.choose_dataset(specs)

        self.assertNotIn("GitHub 上传", output.getvalue())

    def test_mini_description_and_documentation_are_not_machine_specific(self) -> None:
        _, specs = dataset.load_settings()
        description = specs["coco2017_mini"].description
        self.assertNotIn("CMR_Bench", description)
        self.assertNotIn("端到端", description)

        readme = (dataset.PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        script = (dataset.PROJECT_ROOT / "dataset.py").read_text(encoding="utf-8")
        self.assertNotIn("D:\\\\", readme)
        self.assertNotIn("D:/", readme)
        self.assertNotIn("/hy-tmp", readme)
        self.assertNotIn("D:\\\\", script)
        self.assertNotIn("D:/", script)
        self.assertNotIn("/hy-tmp", script)

    def test_submenu_zero_returns_to_main_menu(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("builtins.input", side_effect=["3", "0", "0"]),
            redirect_stdout(output),
        ):
            dataset.interactive()

        self.assertGreaterEqual(output.getvalue().count("CMR 原始数据集工具"), 2)

    def test_prepare_menu_packs_without_a_separate_inspect_step(self) -> None:
        spec = dataset.DatasetSpec(
            name="demo",
            description="test",
            archive_root="demo",
            publish_enabled=False,
            upstream="test",
        )
        settings = {"part_size_mib": 1900}
        source = Path("demo.zip")
        with (
            mock.patch.object(dataset, "dataset_asset_paths", return_value=[]),
            mock.patch.object(dataset, "local_bundle_summary", return_value="未生成"),
            mock.patch.object(dataset, "prompt_path", return_value=source),
            mock.patch.object(dataset, "choose_menu_option", return_value="1"),
            mock.patch.object(dataset, "pack_dataset") as pack,
            mock.patch.object(dataset, "inspect_dataset_source") as inspect,
        ):
            dataset.interactive_prepare(settings, spec)

        inspect.assert_not_called()
        pack.assert_called_once_with(
            spec,
            source.resolve(),
            1900 * 1024 * 1024,
            force=False,
        )

    def test_prepare_menu_explains_local_pack_without_upload(self) -> None:
        spec = dataset.DatasetSpec(
            name="demo",
            description="test",
            archive_root="demo",
            publish_enabled=False,
            upstream="test",
        )
        output = io.StringIO()
        with (
            mock.patch.object(dataset, "dataset_asset_paths", return_value=[Path("old.zip")]),
            mock.patch.object(dataset, "local_bundle_summary", return_value="大数据 3 个独立 ZIP，5.0 GiB"),
            mock.patch.object(dataset, "choose_menu_option", return_value=None),
            redirect_stdout(output),
        ):
            dataset.interactive_prepare({"part_size_mib": 1900}, spec)

        text = output.getvalue()
        self.assertIn("仍可正常制作本机数据包", text)
        self.assertIn("测试安装现有数据包", text)
        self.assertIn("从原始数据重新制作", text)
        self.assertNotIn("仅本地使用", text)

    def test_publisher_can_test_existing_local_bundle(self) -> None:
        spec = dataset.DatasetSpec(
            name="demo",
            description="test",
            archive_root="demo",
            publish_enabled=True,
            upstream="test",
        )
        with (
            mock.patch.object(dataset, "dataset_asset_paths", return_value=[Path("old.zip")]),
            mock.patch.object(dataset, "local_bundle_summary", return_value="小数据单 ZIP，1.0 MiB"),
            mock.patch.object(dataset, "choose_menu_option", return_value="2"),
            mock.patch.object(dataset, "interactive_install_local_bundle") as install_local,
        ):
            dataset.interactive_prepare({"part_size_mib": 1900}, spec)

        install_local.assert_called_once_with(spec)

    def test_command_help_prioritizes_normal_usage(self) -> None:
        help_text = dataset.build_parser().format_help()

        self.assertIn("常用示例", help_text)
        self.assertIn("python dataset.py install coco2017_mini", help_text)
        self.assertNotRegex(help_text, r"(?m)^\s+list\s+")

    def test_legacy_list_command_still_maps_to_status(self) -> None:
        settings = {"repository": "owner/repo", "release_tag": "datasets"}
        with (
            mock.patch.object(dataset, "load_settings", return_value=(settings, {})),
            mock.patch.object(dataset, "print_dataset_status") as print_status,
        ):
            result = dataset.main(["list", "--remote"])

        self.assertEqual(result, 0)
        print_status.assert_called_once_with(settings, {}, check_remote=True)

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

    def test_directory_single_zip_and_split_zip_can_convert_each_way(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "demo"
            source.mkdir()
            expected: dict[str, bytes] = {}
            for index in range(4):
                content = bytes([index]) * 600_000
                name = f"nested/file-{index}.bin"
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                expected[name] = content

            spec = dataset.DatasetSpec(
                name="demo",
                description="test",
                archive_root="demo",
                publish_enabled=True,
                upstream="test",
            )
            single_zip = root / "converted" / "demo.zip"
            dataset.convert_dataset(
                spec,
                source,
                "single-zip",
                single_zip,
                1024 * 1024,
            )
            self.assertTrue(zipfile.is_zipfile(single_zip))

            split_dir = root / "split"
            split_manifest = dataset.convert_dataset(
                spec,
                single_zip,
                "split-zip",
                split_dir,
                1024 * 1024,
            )
            self.assertIsInstance(split_manifest, dict)
            part_names = [
                part["name"] for part in split_manifest["archive"]["parts"]
            ]
            self.assertGreater(len(part_names), 1)

            from_split = root / "from-split"
            dataset.convert_dataset(
                spec,
                split_dir / part_names[0],
                "directory",
                from_split,
                1024 * 1024,
            )
            for name, content in expected.items():
                self.assertEqual((from_split / "demo" / name).read_bytes(), content)

            rebuilt_zip = root / "rebuilt" / "demo.zip"
            dataset.convert_dataset(
                spec,
                split_dir / dataset.manifest_name("demo"),
                "single-zip",
                rebuilt_zip,
                1024 * 1024,
            )
            from_big_zip = root / "from-big-zip"
            dataset.convert_dataset(
                spec,
                rebuilt_zip,
                "directory",
                from_big_zip,
                1024 * 1024,
            )
            for name, content in expected.items():
                self.assertEqual((from_big_zip / "demo" / name).read_bytes(), content)

    def test_pack_can_read_existing_source_zip_without_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_zip = root / "demo.zip"
            with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("demo/images/one.jpg", b"one")
                archive.writestr("demo/annotations/two.txt", b"two")

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
                manifest = dataset.pack_dataset(spec, source_zip, 1024 * 1024)
                loaded, paths = dataset.validate_local_bundle("demo", assets)
                dataset.install_bundle(loaded, paths, install_root)

            self.assertEqual(manifest["source"]["kind"], "zip")
            self.assertEqual(
                sorted(item["path"] for item in manifest["files"]),
                ["annotations/two.txt", "images/one.jpg"],
            )
            self.assertEqual((install_root / "demo/images/one.jpg").read_bytes(), b"one")

    def test_source_zip_can_decode_legacy_gbk_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_zip = root / "nuswide.zip"
            chinese_name = "类别-数量.txt"
            legacy_name = chinese_name.encode("gbk").decode("cp437")
            info = LegacyNameZipInfo(f"nuswide/ConceptsList/{legacy_name}")
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr(info, b"content")

            spec = dataset.DatasetSpec(
                name="nuswide",
                description="test",
                archive_root="nuswide",
                publish_enabled=False,
                upstream="test",
                source_zip_encoding="gbk",
            )
            assets = root / "assets"
            with dataset.open_dataset_source(spec, source_zip) as source:
                self.assertEqual(
                    [entry.relative_path for entry in source.entries],
                    [f"ConceptsList/{chinese_name}"],
                )
            with mock.patch.object(dataset, "ASSET_DIR", assets):
                manifest = dataset.pack_dataset(spec, source_zip, 1024 * 1024)
                output_zip = assets / manifest["archive"]["parts"][0]["name"]
                with zipfile.ZipFile(output_zip) as output:
                    self.assertIn(
                        f"nuswide/ConceptsList/{chinese_name}",
                        output.namelist(),
                    )

    def test_force_pack_restores_old_assets_if_final_swap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            part_name = "CMR_raw_dataset-demo.zip"
            old_part = assets / part_name
            old_manifest = assets / dataset.manifest_name("demo")
            old_checksum = assets / dataset.checksum_name("demo")
            old_part.write_bytes(b"old-part")
            old_manifest.write_bytes(b"old-manifest")
            old_checksum.write_bytes(b"old-checksum")
            temp_part_path = assets / ".new-part.tmp"
            temp_part_path.write_bytes(b"new-part")
            manifest = {
                "archive": {
                    "parts": [
                        {
                            "name": part_name,
                            "bytes": 8,
                            "sha256": dataset.sha256_file(temp_part_path),
                        }
                    ]
                }
            }
            real_replace = os.replace
            failed = False

            def fail_once(source, target):  # type: ignore[no-untyped-def]
                nonlocal failed
                if Path(target).name == old_checksum.name and not failed:
                    failed = True
                    raise OSError("simulated swap failure")
                return real_replace(source, target)

            with (
                mock.patch.object(dataset, "ASSET_DIR", assets),
                mock.patch.object(dataset.os, "replace", side_effect=fail_once),
                self.assertRaisesRegex(OSError, "simulated"),
            ):
                dataset.finalize_local_bundle(
                    "demo",
                    manifest,
                    [part_name],
                    [
                        dataset.TempPart(
                            temp_part_path,
                            temp_part_path.stat().st_size,
                            dataset.sha256_file(temp_part_path),
                        )
                    ],
                    [old_part, old_manifest, old_checksum],
                )

            self.assertEqual(old_part.read_bytes(), b"old-part")
            self.assertEqual(old_manifest.read_bytes(), b"old-manifest")
            self.assertEqual(old_checksum.read_bytes(), b"old-checksum")

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

    def test_upload_plan_skips_equal_and_requires_replace_for_stale_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "CMR_raw_dataset-demo.zip"
            manifest = root / "CMR_raw_dataset-demo.manifest.json"
            current.write_bytes(b"current")
            manifest.write_bytes(b"manifest")
            remote = {
                current.name: {
                    "name": current.name,
                    "size": current.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{dataset.sha256_file(current)}",
                },
                "CMR_raw_dataset-demo.part002.zip": {
                    "name": "CMR_raw_dataset-demo.part002.zip",
                    "size": 1,
                    "state": "uploaded",
                    "digest": "sha256:" + "0" * 64,
                },
            }

            with self.assertRaisesRegex(dataset.DatasetError, "旧分包"):
                dataset.build_upload_plan(
                    "demo",
                    [current, manifest],
                    remote,
                    replace=False,
                )
            plan = dataset.build_upload_plan(
                "demo",
                [current, manifest],
                remote,
                replace=True,
            )

            self.assertEqual(plan.skip, [current])
            self.assertEqual(plan.upload, [manifest])
            self.assertEqual(plan.delete, ["CMR_raw_dataset-demo.part002.zip"])

    def test_upload_uses_gh_executable_and_supplied_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "asset.zip"
            path.write_bytes(b"asset")
            digest = dataset.sha256_file(path)
            remote = {
                "assets": [
                    {
                        "name": path.name,
                        "state": "uploaded",
                        "size": path.stat().st_size,
                        "digest": f"sha256:{digest}",
                    }
                ]
            }
            with (
                mock.patch.object(dataset.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                mock.patch.object(dataset, "gh_release_view", return_value=remote),
            ):
                dataset.upload_one_asset(
                    "C:/GitHub CLI/gh.exe",
                    "owner/repo",
                    "datasets",
                    path,
                    replace=False,
                    expected_digest=digest,
                )

            self.assertEqual(run.call_args.args[0][0], "C:/GitHub CLI/gh.exe")

    def test_download_resumes_matching_partial_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "asset.zip"
            partial = root / ".asset.zip.download"
            identity = root / ".asset.zip.download.json"
            partial.write_bytes(b"abc")
            identity.write_text(json.dumps({"identity": "asset-v1"}), encoding="utf-8")

            def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
                self.assertEqual(request.get_header("Range"), "bytes=3-")
                self.assertEqual(timeout, 60)
                return FakeHttpResponse(b"def", 3, 6)

            with mock.patch.object(dataset.urllib.request, "urlopen", side_effect=fake_urlopen):
                dataset.download_file(
                    "https://example.invalid/asset.zip",
                    target,
                    6,
                    repo="owner/repo",
                    tag="datasets",
                    asset_name_value="asset.zip",
                    asset_identity="asset-v1",
                )

            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse(partial.exists())
            self.assertFalse(identity.exists())


if __name__ == "__main__":
    unittest.main()
