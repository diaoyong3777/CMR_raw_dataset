#!/usr/bin/env python3
"""CMR_raw_dataset 的检查、打包、发布、下载和安装工具。

直接运行 ``python dataset.py`` 使用交互菜单；也可以使用子命令自动化。
脚本读取原目录、单 ZIP 或带 manifest 的多 ZIP，所有中间文件都写在忽略目录中。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "datasets.json"
ASSET_DIR = PROJECT_ROOT / "release_assets"
CACHE_DIR = PROJECT_ROOT / ".dataset_cache"
DEFAULT_RAW_DIR = PROJECT_ROOT / "raw_dataset"
DEFAULT_INSTALL_DIR = PROJECT_ROOT.parent / "datasets"
ASSET_PREFIX = "CMR_raw_dataset-"
MANIFEST_SCHEMA = 1
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


# 基础配置与通用数据结构


class DatasetError(RuntimeError):
    """可以直接向使用者展示的预期错误。"""


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    description: str
    archive_root: str
    publish_enabled: bool
    upstream: str
    source_zip_encoding: str | None = None


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    size: int


@dataclass(frozen=True)
class SourceEntry:
    relative_path: str
    size: int
    token: object
    sha256: str | None = None


@dataclass(frozen=True)
class TempPart:
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class UploadPlan:
    skip: list[Path]
    upload: list[Path]
    replace: list[Path]
    delete: list[str]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def format_part_limit(size: int) -> str:
    if size == 1900 * 1024 * 1024:
        return "1900 MiB（约 2 GB，已预留安全空间）"
    return format_size(size)


def print_file_progress(
    action: str,
    current: int,
    total: int,
    *,
    part: int | None = None,
    part_total: int | None = None,
) -> None:
    """按约 10% 的间隔显示进度，避免大数据集产生数百行文件名。"""
    if total < 1 or current < 1 or current > total:
        return
    crossed_ten_percent = (current * 10 // total) != ((current - 1) * 10 // total)
    if current != 1 and current != total and not crossed_ten_percent:
        return
    part_text = ""
    if part is not None and part_total is not None:
        part_text = f"，分包 {part}/{part_total}"
    percentage = current * 100 / total
    print(
        f"  {action}：{percentage:5.1f}%（{current:,}/{total:,} 个文件{part_text}）"
    )


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_bytes(content)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_settings() -> tuple[dict, dict[str, DatasetSpec]]:
    if not CONFIG_PATH.is_file():
        raise DatasetError(f"缺少配置文件：{CONFIG_PATH}")
    try:
        settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise DatasetError(f"无法读取配置文件：{exc}") from exc
    if settings.get("schema_version") != 1:
        raise DatasetError("datasets.json 的 schema_version 不受支持")
    raw_datasets = settings.get("datasets")
    if not isinstance(raw_datasets, dict) or not raw_datasets:
        raise DatasetError("datasets.json 没有配置数据集")

    specs: dict[str, DatasetSpec] = {}
    for name, raw in raw_datasets.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise DatasetError(f"数据集名称无效：{name!r}")
        if not isinstance(raw, dict):
            raise DatasetError(f"数据集配置无效：{name}")
        description = str(raw.get("description", "")).strip()
        upstream = str(raw.get("upstream", "")).strip()
        if not description:
            raise DatasetError(f"数据集缺少 description：{name}")
        if not upstream:
            raise DatasetError(f"数据集缺少 upstream：{name}")
        archive_root = str(raw.get("archive_root", ""))
        if archive_root != name:
            raise DatasetError(f"{name} 的 archive_root 必须与数据集名一致")
        specs[name] = DatasetSpec(
            name=name,
            description=description,
            archive_root=archive_root,
            publish_enabled=bool(raw.get("publish_enabled", False)),
            upstream=upstream,
            source_zip_encoding=(
                str(raw["source_zip_encoding"])
                if raw.get("source_zip_encoding")
                else None
            ),
        )
    return settings, specs


# 原始文件夹 / 原始 ZIP 数据源


def should_ignore(relative_path: PurePosixPath) -> bool:
    return any(part == "__pycache__" for part in relative_path.parts) or (
        relative_path.name in IGNORED_NAMES
    )


def scan_source(source: Path) -> list[SourceFile]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise DatasetError(f"原始数据目录不存在：{source}")

    records: list[SourceFile] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise DatasetError(f"无法读取目录 {directory}：{exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if should_ignore(relative):
                continue
            if entry.is_symlink():
                raise DatasetError(f"原始数据包含符号链接，拒绝打包：{relative}")
            if entry.is_dir(follow_symlinks=False):
                walk(path)
            elif entry.is_file(follow_symlinks=False):
                records.append(
                    SourceFile(
                        path=path,
                        relative_path=relative.as_posix(),
                        size=entry.stat(follow_symlinks=False).st_size,
                    )
                )
            else:
                raise DatasetError(f"原始数据包含非常规文件，拒绝打包：{relative}")

    walk(source)
    if not records:
        raise DatasetError(f"原始数据目录没有可打包文件：{source}")

    # Linux 允许仅大小写不同的文件，Windows 解压时会发生覆盖，因此提前拒绝。
    folded: dict[str, str] = {}
    for record in records:
        key = record.relative_path.casefold()
        if key in folded:
            raise DatasetError(
                "存在仅大小写不同的路径，无法跨平台安全发布："
                f"{folded[key]} / {record.relative_path}"
            )
        folded[key] = record.relative_path
    return sorted(records, key=lambda record: record.relative_path.casefold())


class DatasetSource:
    """原始文件夹和原始 ZIP 共用的只读数据源接口。"""

    kind = "unknown"

    def __init__(self, path: Path, entries: list[SourceEntry]) -> None:
        self.path = path
        self.entries = entries

    def open_entry(self, entry: SourceEntry) -> BinaryIO:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "DatasetSource":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class DirectoryDatasetSource(DatasetSource):
    kind = "directory"

    def __init__(self, path: Path) -> None:
        records = scan_source(path)
        entries = [
            SourceEntry(record.relative_path, record.size, record.path)
            for record in records
        ]
        super().__init__(path.expanduser().resolve(), entries)

    def open_entry(self, entry: SourceEntry) -> BinaryIO:
        path = entry.token
        if not isinstance(path, Path):
            raise DatasetError("文件夹数据源记录无效")

        # Windows 未启用长路径策略时，普通路径接口无法打开超过 260 字符的文件。
        # 扫描结果保存的是绝对路径，因此可安全转换为扩展长度路径后再读取。
        if os.name == "nt" and len(str(path)) >= 260:
            raw_path = str(path)
            if not raw_path.startswith("\\\\?\\"):
                if raw_path.startswith("\\\\"):
                    path = Path("\\\\?\\UNC\\" + raw_path[2:])
                else:
                    path = Path("\\\\?\\" + raw_path)
        return path.open("rb")


class ZipDatasetSource(DatasetSource):
    kind = "zip"

    def __init__(self, path: Path, spec: DatasetSpec) -> None:
        resolved = path.expanduser().resolve()
        try:
            self.archive = zipfile.ZipFile(resolved, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise DatasetError(f"无法打开原始 ZIP：{resolved}：{exc}") from exc

        entries: list[SourceEntry] = []
        try:
            for index, info in enumerate(self.archive.infolist()):
                filename = self._decoded_name(info, spec.source_zip_encoding)
                _, relative = safe_archive_path(filename, spec.archive_root)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise DatasetError(f"原始 ZIP 包含符号链接：{filename}")
                if info.flag_bits & 0x1:
                    raise DatasetError(f"原始 ZIP 包含加密文件：{filename}")
                if info.is_dir():
                    continue
                if relative is None:
                    raise DatasetError(f"原始 ZIP 包含无文件名条目：{filename}")
                if should_ignore(PurePosixPath(relative)):
                    continue
                entries.append(SourceEntry(relative, info.file_size, index))
            validate_source_entries(entries, resolved)
        except Exception:
            self.archive.close()
            raise
        super().__init__(resolved, entries)

    @staticmethod
    def _decoded_name(info: zipfile.ZipInfo, encoding: str | None) -> str:
        if not encoding or info.flag_bits & 0x800 or info.filename.isascii():
            return info.filename
        try:
            return info.filename.encode("cp437").decode(encoding)
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError) as exc:
            raise DatasetError(
                f"无法使用 {encoding} 解码原始 ZIP 文件名：{ascii(info.filename)}"
            ) from exc

    def open_entry(self, entry: SourceEntry) -> BinaryIO:
        if not isinstance(entry.token, int):
            raise DatasetError("ZIP 数据源记录无效")
        try:
            return self.archive.open(self.archive.infolist()[entry.token], "r")
        except (IndexError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise DatasetError(f"无法读取原始 ZIP 条目：{entry.relative_path}") from exc

    def close(self) -> None:
        self.archive.close()


class BundleDatasetSource(DatasetSource):
    """把一组带 manifest 的独立 ZIP 作为一个连续只读数据源。"""

    kind = "split-zip"

    def __init__(self, directory: Path, spec: DatasetSpec) -> None:
        resolved = directory.expanduser().resolve()
        manifest, part_paths = load_local_bundle(
            spec.name,
            resolved,
            verify_part_hashes=False,
        )
        self.part_paths = part_paths
        expected = manifest_file_map(manifest)
        self.archives: list[zipfile.ZipFile] = []
        tokens: dict[str, tuple[int, int]] = {}
        try:
            for archive_index, part_path in enumerate(part_paths):
                try:
                    archive = zipfile.ZipFile(part_path, "r")
                except zipfile.BadZipFile as exc:
                    raise DatasetError(f"ZIP 文件损坏：{part_path.name}") from exc
                self.archives.append(archive)
                for info_index, info in enumerate(archive.infolist()):
                    _, relative = safe_archive_path(info.filename, spec.archive_root)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise DatasetError(f"ZIP 包含符号链接：{info.filename}")
                    if info.flag_bits & 0x1:
                        raise DatasetError(f"ZIP 包含加密文件：{info.filename}")
                    if info.is_dir():
                        continue
                    if relative is None or relative not in expected:
                        raise DatasetError(f"ZIP 包含 manifest 未登记的文件：{info.filename}")
                    if relative in tokens:
                        raise DatasetError(f"多个 ZIP 包含重复文件：{info.filename}")
                    if info.file_size != int(expected[relative]["bytes"]):
                        raise DatasetError(f"ZIP 文件大小与 manifest 不一致：{info.filename}")
                    tokens[relative] = (archive_index, info_index)
            missing = sorted(set(expected) - set(tokens))
            if missing:
                raise DatasetError(f"发布包缺少 {len(missing)} 个文件，例如：{missing[0]}")
        except Exception:
            self.close()
            raise

        self.kind = "zip-bundle" if len(part_paths) == 1 else "split-zip"
        entries = [
            SourceEntry(
                relative_path=relative,
                size=int(raw["bytes"]),
                token=tokens[relative],
                sha256=str(raw["sha256"]),
            )
            for relative, raw in expected.items()
        ]
        super().__init__(resolved, entries)

    def open_entry(self, entry: SourceEntry) -> BinaryIO:
        if (
            not isinstance(entry.token, tuple)
            or len(entry.token) != 2
            or not all(isinstance(index, int) for index in entry.token)
        ):
            raise DatasetError("多 ZIP 数据源记录无效")
        archive_index, info_index = entry.token
        try:
            archive = self.archives[archive_index]
            return archive.open(archive.infolist()[info_index], "r")
        except (IndexError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise DatasetError(f"无法读取多 ZIP 条目：{entry.relative_path}") from exc

    def close(self) -> None:
        for archive in getattr(self, "archives", []):
            archive.close()


def validate_source_entries(entries: Sequence[SourceEntry], source: Path) -> None:
    if not entries:
        raise DatasetError(f"原始数据没有可打包文件：{source}")
    folded: dict[str, str] = {}
    for entry in entries:
        key = entry.relative_path.casefold()
        if key in folded:
            raise DatasetError(
                "存在重复或仅大小写不同的路径，无法跨平台安全发布："
                f"{folded[key]} / {entry.relative_path}"
            )
        folded[key] = entry.relative_path


def open_dataset_source(spec: DatasetSpec, source: Path) -> DatasetSource:
    resolved = source.expanduser().resolve()
    bundle_directory: Path | None = None
    if resolved.is_dir() and (resolved / manifest_name(spec.name)).is_file():
        bundle_directory = resolved
    elif resolved.is_file() and resolved.parent.joinpath(manifest_name(spec.name)).is_file():
        if resolved.name == manifest_name(spec.name) or (
            resolved.suffix.casefold() == ".zip"
            and dataset_asset_pattern(spec.name).fullmatch(resolved.name)
        ):
            bundle_directory = resolved.parent
    if bundle_directory is not None:
        bundle_source = BundleDatasetSource(bundle_directory, spec)
        if (
            resolved.is_file()
            and resolved.suffix.casefold() == ".zip"
            and resolved.name not in {path.name for path in bundle_source.part_paths}
        ):
            bundle_source.close()
            raise DatasetError(f"所选 ZIP 不属于 manifest 当前分包：{resolved.name}")
        return bundle_source
    if resolved.is_dir():
        return DirectoryDatasetSource(resolved)
    if resolved.is_file() and resolved.suffix.casefold() == ".zip":
        return ZipDatasetSource(resolved, spec)
    raise DatasetError(
        "数据源必须是原目录、单 ZIP、发布包目录、manifest 或任意 part ZIP："
        f"{resolved}"
    )


def print_source_report(
    dataset: str,
    source: Path,
    records: Sequence[SourceEntry],
    source_kind: str,
    part_size: int | None = None,
) -> None:
    total_size = sum(record.size for record in records)
    suffixes: dict[str, int] = {}
    for record in records:
        suffix = Path(record.relative_path).suffix.lower() or "[无扩展名]"
        if len(suffix) > 16 or any(character.isspace() for character in suffix):
            suffix = "[其他扩展名]"
        suffixes[suffix] = suffixes.get(suffix, 0) + 1
    common = sorted(suffixes.items(), key=lambda item: (-item[1], item[0]))[:8]
    types = "，".join(f"{suffix}={count}" for suffix, count in common)
    print(f"{dataset} 检查通过")
    print(f"  来源：{source.expanduser().resolve()}")
    source_labels = {
        "directory": "原始文件夹",
        "zip": "单个大 ZIP",
        "zip-bundle": "单 ZIP 发布包",
        "split-zip": "多个独立小 ZIP",
    }
    print(f"  类型：{source_labels.get(source_kind, source_kind)}")
    print(f"  文件：{len(records)}")
    print(f"  大小：{format_size(total_size)}")
    print(f"  格式：{types}")
    if part_size is not None:
        groups = split_file_groups(records, part_size, dataset)
        if len(groups) == 1:
            print(f"  整理：小数据，生成 1 个普通 ZIP（上限 {format_part_limit(part_size)}）")
        else:
            print(
                f"  整理：大数据，生成 {len(groups)} 个相互独立的 ZIP"
                f"（单包上限 {format_part_limit(part_size)}）"
            )


def inspect_dataset_source(
    spec: DatasetSpec,
    source: Path,
    part_size: int | None = None,
) -> None:
    with open_dataset_source(spec, source) as dataset_source:
        print_source_report(
            spec.name,
            dataset_source.path,
            dataset_source.entries,
            dataset_source.kind,
            part_size,
        )


# 发布资产生成与本地校验


def asset_base(dataset: str) -> str:
    return f"{ASSET_PREFIX}{dataset}"


def manifest_name(dataset: str) -> str:
    return f"{asset_base(dataset)}.manifest.json"


def checksum_name(dataset: str) -> str:
    return f"{asset_base(dataset)}.sha256"


def dataset_asset_paths(dataset: str, directory: Path | None = None) -> list[Path]:
    asset_dir = directory or ASSET_DIR
    if not asset_dir.is_dir():
        return []
    pattern = dataset_asset_pattern(dataset)
    return sorted(
        (path for path in asset_dir.iterdir() if path.is_file() and pattern.fullmatch(path.name)),
        key=lambda path: path.name,
    )


def dataset_asset_pattern(dataset: str) -> re.Pattern[str]:
    base = re.escape(asset_base(dataset))
    return re.compile(
        rf"^{base}(?:\.zip|\.part\d{{3}}\.zip|\.manifest\.json|\.sha256)$"
    )


def split_file_groups(
    records: Sequence[SourceEntry], part_size: int, archive_root: str
) -> list[list[SourceEntry]]:
    """按未压缩大小和 ZIP 元数据开销预分组，每组都会生成独立可打开的 ZIP。"""
    groups: list[list[SourceEntry]] = []
    current: list[SourceEntry] = []
    estimated_size = 0
    for record in records:
        archive_name = f"{archive_root}/{record.relative_path}"
        # ZIP local header、central directory、ZIP64 和 UTF-8 文件名均预留空间。
        estimate = record.size + 1024 + len(archive_name.encode("utf-8")) * 4
        if estimate > part_size:
            raise DatasetError(
                f"单个文件超过分包上限 {format_size(part_size)}：{record.relative_path}"
            )
        if current and estimated_size + estimate > part_size:
            groups.append(current)
            current = []
            estimated_size = 0
        current.append(record)
        estimated_size += estimate
    if current:
        groups.append(current)
    return groups


def zip_info_for(archive_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 & 0xFFFF) << 16
    info.create_system = 3
    return info


def copy_source_entry(
    source: DatasetSource,
    record: SourceEntry,
    output: BinaryIO,
) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        source_file = source.open_entry(record)
        with source_file:
            while chunk := source_file.read(4 * 1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                bytes_read += len(chunk)
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DatasetError(f"读取来源数据失败：{record.relative_path}：{exc}") from exc
    if bytes_read != record.size:
        raise DatasetError(f"转换期间文件大小发生变化：{record.relative_path}")
    file_digest = digest.hexdigest()
    if record.sha256 is not None and file_digest != record.sha256:
        raise DatasetError(f"来源文件 SHA-256 校验失败：{record.relative_path}")
    return file_digest


def write_zip_parts(
    spec: DatasetSpec,
    source: DatasetSource,
    groups: Sequence[Sequence[SourceEntry]],
    part_size: int,
    *,
    output_dir: Path | None = None,
    enforce_github_limit: bool = True,
) -> tuple[list[TempPart], list[dict[str, str | int]]]:
    asset_dir = output_dir or ASSET_DIR
    asset_dir.mkdir(parents=True, exist_ok=True)
    base = asset_base(spec.name)
    token = uuid.uuid4().hex
    temp_parts: list[TempPart] = []
    file_manifest: list[dict[str, str | int]] = []
    processed = 0
    total = sum(len(group) for group in groups)
    try:
        for part_index, group in enumerate(groups, start=1):
            temp_path = asset_dir / f".{base}.{token}.part{part_index:03d}.tmp"
            with zipfile.ZipFile(
                temp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=1,
                allowZip64=True,
            ) as archive:
                for record in group:
                    archive_path = f"{spec.archive_root}/{record.relative_path}"
                    with archive.open(
                        zip_info_for(archive_path), "w", force_zip64=True
                    ) as output:
                        file_digest = copy_source_entry(source, record, output)
                    file_manifest.append(
                        {
                            "path": record.relative_path,
                            "bytes": record.size,
                            "sha256": file_digest,
                        }
                    )
                    processed += 1
                    print_file_progress(
                        "打包进度",
                        processed,
                        total,
                        part=part_index,
                        part_total=len(groups),
                    )
            actual_size = temp_path.stat().st_size
            if actual_size > part_size:
                raise DatasetError(
                    "ZIP 元数据使分包超过设定上限，请减小分包目标后重试："
                    f"{format_size(actual_size)} > {format_size(part_size)}"
                )
            if enforce_github_limit and actual_size >= 2 * 1024 * 1024 * 1024:
                raise DatasetError(
                    f"ZIP 分包超过 GitHub 2 GiB 硬限制：{format_size(actual_size)}"
                )
            temp_parts.append(TempPart(temp_path, actual_size, sha256_file(temp_path)))
        return temp_parts, file_manifest
    except Exception:
        for part in temp_parts:
            part.path.unlink(missing_ok=True)
        for path in asset_dir.glob(f".{base}.{token}.part*.tmp"):
            path.unlink(missing_ok=True)
        raise


def build_manifest(
    spec: DatasetSpec,
    source: DatasetSource,
    records: Sequence[SourceEntry],
    temp_parts: Sequence[TempPart],
    file_manifest: list[dict[str, str | int]],
) -> tuple[dict, list[str]]:
    base = asset_base(spec.name)
    if len(temp_parts) == 1:
        final_names = [f"{base}.zip"]
    else:
        final_names = [
            f"{base}.part{index:03d}.zip"
            for index in range(1, len(temp_parts) + 1)
        ]
    parts = [
        {"name": name, "bytes": part.size, "sha256": part.sha256}
        for name, part in zip(final_names, temp_parts)
    ]
    bundle_hash = hashlib.sha256(
        "\n".join(f"{part['sha256']}  {part['name']}" for part in parts).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "dataset": spec.name,
        "description": spec.description,
        "upstream": spec.upstream,
        "archive": {
            "format": "independent-zip-parts",
            "root": spec.archive_root,
            "bytes": sum(part.size for part in temp_parts),
            "bundle_sha256": bundle_hash,
            "parts": parts,
        },
        "source": {
            "kind": source.kind,
            "name": source.path.name,
            "file_count": len(records),
            "bytes": sum(record.size for record in records),
        },
        "files": file_manifest,
    }
    return manifest, final_names


def finalize_local_bundle(
    dataset: str,
    manifest: dict,
    final_names: Sequence[str],
    temp_parts: Sequence[TempPart],
    existing: Sequence[Path],
    *,
    output_dir: Path | None = None,
) -> tuple[list[Path], Path, Path]:
    asset_dir = output_dir or ASSET_DIR
    asset_dir.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_temp = asset_dir / f".{manifest_name(dataset)}.{uuid.uuid4().hex}.tmp"
    checksum_temp = asset_dir / f".{checksum_name(dataset)}.{uuid.uuid4().hex}.tmp"
    manifest_temp.write_bytes(manifest_bytes)
    checksum_lines = [
        f"{part['sha256']}  {part['name']}" for part in manifest["archive"]["parts"]
    ]
    checksum_lines.append(
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  {manifest_name(dataset)}"
    )
    checksum_temp.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8", newline="\n")
    backup_suffix = uuid.uuid4().hex
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    committed = False
    try:
        # 同盘重命名几乎不占额外空间；全部新文件就位后才删除旧资产。
        for path in existing:
            if not path.exists():
                continue
            backup = path.with_name(f".{path.name}.{backup_suffix}.backup")
            os.replace(path, backup)
            backups.append((path, backup))
        final_paths: list[Path] = []
        for temp_part, final_name in zip(temp_parts, final_names):
            final_path = asset_dir / final_name
            os.replace(temp_part.path, final_path)
            final_paths.append(final_path)
            installed.append(final_path)
        final_manifest = asset_dir / manifest_name(dataset)
        final_checksum = asset_dir / checksum_name(dataset)
        os.replace(manifest_temp, final_manifest)
        installed.append(final_manifest)
        os.replace(checksum_temp, final_checksum)
        installed.append(final_checksum)
        committed = True
        return final_paths, final_manifest, final_checksum
    finally:
        manifest_temp.unlink(missing_ok=True)
        checksum_temp.unlink(missing_ok=True)
        if committed:
            for _, backup in backups:
                backup.unlink(missing_ok=True)
        else:
            for path in reversed(installed):
                path.unlink(missing_ok=True)
            for original, backup in reversed(backups):
                if backup.exists() and not original.exists():
                    os.replace(backup, original)


def pack_dataset(
    spec: DatasetSpec,
    source: Path,
    part_size: int,
    *,
    force: bool = False,
    output_dir: Path | None = None,
) -> dict:
    asset_dir = (output_dir or ASSET_DIR).expanduser().resolve()
    existing = dataset_asset_paths(spec.name, asset_dir)
    source_resolved = source.expanduser().resolve()
    if existing and not force:
        raise DatasetError(
            "本地已有同名发布文件，默认不覆盖："
            + ", ".join(path.name for path in existing)
            + "。确认后使用 --force。"
        )

    asset_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    temp_parts: list[TempPart] = []
    try:
        with open_dataset_source(spec, source_resolved) as dataset_source:
            if isinstance(dataset_source, DirectoryDatasetSource) and (
                asset_dir == dataset_source.path
                or asset_dir.is_relative_to(dataset_source.path)
            ):
                raise DatasetError("多 ZIP 输出目录不能位于作为来源的原目录内部")
            records = dataset_source.entries
            print_source_report(spec.name, dataset_source.path, records, dataset_source.kind)
            groups = split_file_groups(records, part_size, spec.archive_root)
            package_kind = "单 ZIP" if len(groups) == 1 else f"{len(groups)} 个独立 ZIP"
            print(f"  方案：{package_kind}（单包上限 {format_part_limit(part_size)}）")
            temp_parts, file_manifest = write_zip_parts(
                spec,
                dataset_source,
                groups,
                part_size,
                output_dir=asset_dir,
            )
            manifest, final_part_names = build_manifest(
                spec,
                dataset_source,
                records,
                temp_parts,
                file_manifest,
            )
        final_paths, final_manifest, final_checksum = finalize_local_bundle(
            spec.name,
            manifest,
            final_part_names,
            temp_parts,
            existing,
            output_dir=asset_dir,
        )

        elapsed = max(time.monotonic() - started, 0.001)
        print(f"已生成 {len(final_paths)} 个数据分卷：")
        for path in final_paths:
            print(f"  {path.name}  {format_size(path.stat().st_size)}")
        print(f"  {final_manifest.name}")
        print(f"  {final_checksum.name}")
        print(
            f"ZIP 总大小：{format_size(sum(part.size for part in temp_parts))}，"
            f"耗时 {elapsed:.1f} 秒，bundle SHA-256={manifest['archive']['bundle_sha256']}"
        )
        return manifest
    except Exception:
        for part in temp_parts:
            part.path.unlink(missing_ok=True)
        raise


def safely_replace_file(temp_path: Path, target: Path, *, force: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_dir():
        raise DatasetError(f"目标是文件夹，不能写入 ZIP：{target}")
    if target.exists() and not force:
        raise DatasetError(f"目标文件已存在，默认不覆盖：{target}。确认后使用 --force。")
    backup: Path | None = None
    if target.exists():
        backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.backup")
        os.replace(target, backup)
    try:
        os.replace(temp_path, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup is not None:
        backup.unlink(missing_ok=True)


def convert_to_single_zip(
    spec: DatasetSpec,
    source_path: Path,
    output: Path,
    *,
    force: bool,
) -> Path:
    target = output.expanduser().resolve()
    if target.suffix.casefold() != ".zip":
        raise DatasetError("单个大 ZIP 的输出路径必须以 .zip 结尾")
    if target.exists() and target.is_dir():
        raise DatasetError(f"目标是文件夹，不能写入 ZIP：{target}")
    if target.exists() and not force:
        raise DatasetError(f"目标文件已存在，默认不覆盖：{target}。确认后使用 --force。")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    started = time.monotonic()
    try:
        with open_dataset_source(spec, source_path) as source:
            if isinstance(source, DirectoryDatasetSource):
                try:
                    target.relative_to(source.path)
                except ValueError:
                    pass
                else:
                    raise DatasetError("大 ZIP 输出不能放在作为来源的原目录内部")
            if isinstance(source, BundleDatasetSource):
                canonical = dataset_asset_pattern(spec.name).fullmatch(target.name)
                if canonical and target.parent == source.path:
                    raise DatasetError("大 ZIP 不能覆盖多 ZIP 发布包中的受 manifest 管理资产")
            print_source_report(spec.name, source.path, source.entries, source.kind)
            print(f"  目标：单个大 ZIP → {target}")
            with zipfile.ZipFile(
                temp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=1,
                allowZip64=True,
            ) as archive:
                total = len(source.entries)
                for index, record in enumerate(source.entries, start=1):
                    archive_path = f"{spec.archive_root}/{record.relative_path}"
                    with archive.open(
                        zip_info_for(archive_path),
                        "w",
                        force_zip64=True,
                    ) as destination:
                        copy_source_entry(source, record, destination)
                    print_file_progress("转换进度", index, total)
        if not zipfile.is_zipfile(temp_path):
            raise DatasetError("生成的大 ZIP 终检失败")
        safely_replace_file(temp_path, target, force=force)
        elapsed = max(time.monotonic() - started, 0.001)
        print(f"转换完成：{target}（{format_size(target.stat().st_size)}，{elapsed:.1f} 秒）")
        return target
    finally:
        temp_path.unlink(missing_ok=True)


def convert_to_directory(
    spec: DatasetSpec,
    source_path: Path,
    output_root: Path,
    *,
    force: bool,
) -> Path:
    root = output_root.expanduser().resolve()
    target = root / spec.name
    if target.exists() and not force:
        raise DatasetError(f"目标目录已存在，默认不覆盖：{target}。确认后使用 --force。")
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{spec.name}.convert-", dir=root))
    staging_target = staging / spec.name
    started = time.monotonic()
    try:
        with open_dataset_source(spec, source_path) as source:
            if isinstance(source, DirectoryDatasetSource):
                if target == source.path:
                    raise DatasetError("来源已经是目标原目录，无需转换")
                if root == source.path or root.is_relative_to(source.path):
                    raise DatasetError("原目录输出不能位于作为来源的原目录内部")
                if source.path.is_relative_to(target):
                    raise DatasetError("来源目录不能位于将被替换的目标目录内部")
            print_source_report(spec.name, source.path, source.entries, source.kind)
            print(f"  目标：原目录 → {target}")
            staging_target.mkdir(parents=True)
            total = len(source.entries)
            for index, record in enumerate(source.entries, start=1):
                destination = staging_target / Path(
                    *PurePosixPath(record.relative_path).parts
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output_file:
                    copy_source_entry(source, record, output_file)
                print_file_progress("转换进度", index, total)

        backup: Path | None = None
        if target.exists():
            backup = root / (
                f".{spec.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(target, backup)
        try:
            os.replace(staging_target, target)
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        elapsed = max(time.monotonic() - started, 0.001)
        print(f"转换完成：{target}（{elapsed:.1f} 秒）")
        if backup is not None:
            print(f"原目录已保留为备份：{backup}")
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def convert_dataset(
    spec: DatasetSpec,
    source: Path,
    target_format: str,
    output: Path,
    part_size: int,
    *,
    force: bool = False,
) -> Path | dict:
    if target_format == "directory":
        return convert_to_directory(spec, source, output, force=force)
    if target_format == "single-zip":
        return convert_to_single_zip(spec, source, output, force=force)
    if target_format == "split-zip":
        print(f"目标：多个独立小 ZIP → {output.expanduser().resolve()}")
        return pack_dataset(
            spec,
            source,
            part_size,
            force=force,
            output_dir=output,
        )
    raise DatasetError(f"不支持的转换目标格式：{target_format}")


def parse_checksum_file(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"无法读取校验文件 {path}：{exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise DatasetError(f"SHA-256 文件格式无效：{path.name}")
        name = parts[1].strip().lstrip("*")
        if Path(name).name != name:
            raise DatasetError(f"SHA-256 文件包含非法文件名：{name}")
        if name in checksums:
            raise DatasetError(f"SHA-256 文件包含重复文件名：{name}")
        checksums[name] = parts[0].lower()
    if not checksums:
        raise DatasetError(f"SHA-256 文件为空：{path.name}")
    return checksums


def load_manifest(path: Path, expected_dataset: str) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"无法读取 manifest {path}：{exc}") from exc
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise DatasetError(f"manifest 版本不受支持：{path.name}")
    if manifest.get("dataset") != expected_dataset:
        raise DatasetError(f"manifest 数据集名称不匹配：{path.name}")
    archive = manifest.get("archive")
    files = manifest.get("files")
    if not isinstance(archive, dict) or archive.get("format") != "independent-zip-parts":
        raise DatasetError(f"manifest 归档格式无效：{path.name}")
    if archive.get("root") != expected_dataset:
        raise DatasetError(f"manifest 顶层目录无效：{path.name}")
    if not isinstance(archive.get("parts"), list) or not archive["parts"]:
        raise DatasetError(f"manifest 没有数据分卷：{path.name}")
    if not isinstance(files, list) or not files:
        raise DatasetError(f"manifest 没有文件清单：{path.name}")
    seen_parts: set[str] = set()
    bundle_lines: list[str] = []
    for part in archive["parts"]:
        if not isinstance(part, dict):
            raise DatasetError(f"manifest 分卷记录无效：{path.name}")
        name = str(part.get("name", ""))
        size = part.get("bytes")
        digest = str(part.get("sha256", "")).lower()
        if (
            Path(name).name != name
            or not name.endswith(".zip")
            or not dataset_asset_pattern(expected_dataset).fullmatch(name)
            or name in seen_parts
            or not isinstance(size, int)
            or size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise DatasetError(f"manifest 分卷记录无效：{name or path.name}")
        seen_parts.add(name)
        bundle_lines.append(f"{digest}  {name}")
    bundle_digest = str(archive.get("bundle_sha256", "")).lower()
    expected_bundle = hashlib.sha256("\n".join(bundle_lines).encode("utf-8")).hexdigest()
    if bundle_digest != expected_bundle:
        raise DatasetError(f"manifest bundle SHA-256 无效：{path.name}")
    return manifest


def load_local_bundle(
    dataset: str,
    directory: Path = ASSET_DIR,
    *,
    verify_part_hashes: bool,
) -> tuple[dict, list[Path]]:
    manifest_path = directory / manifest_name(dataset)
    checksum_path = directory / checksum_name(dataset)
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise DatasetError(f"缺少 {dataset} 的 manifest 或 SHA-256，请先打包")
    checksums = parse_checksum_file(checksum_path)
    expected_manifest_hash = checksums.get(manifest_path.name)
    if expected_manifest_hash != sha256_file(manifest_path):
        raise DatasetError(f"manifest SHA-256 校验失败：{manifest_path.name}")
    manifest = load_manifest(manifest_path, dataset)
    part_paths: list[Path] = []
    for part in manifest["archive"]["parts"]:
        if not isinstance(part, dict):
            raise DatasetError("manifest 分卷记录无效")
        name = str(part.get("name", ""))
        if Path(name).name != name:
            raise DatasetError(f"manifest 分卷名称不安全：{name}")
        path = directory / name
        if not path.is_file():
            raise DatasetError(f"缺少数据分卷：{name}")
        if path.stat().st_size != int(part.get("bytes", -1)):
            raise DatasetError(f"数据分卷大小不匹配：{name}")
        expected = str(part.get("sha256", "")).lower()
        if checksums.get(name) != expected:
            raise DatasetError(f"数据分卷 SHA-256 清单不一致：{name}")
        if verify_part_hashes and sha256_file(path) != expected:
            raise DatasetError(f"数据分卷 SHA-256 校验失败：{name}")
        part_paths.append(path)
    return manifest, part_paths


def validate_local_bundle(dataset: str, directory: Path = ASSET_DIR) -> tuple[dict, list[Path]]:
    return load_local_bundle(
        dataset,
        directory,
        verify_part_hashes=True,
    )


# GitHub Release 上传


def find_gh() -> str | None:
    executable = shutil.which("gh")
    if executable:
        return executable
    if os.name == "nt":
        candidates: list[Path] = []
        program_files = os.environ.get("ProgramFiles")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if program_files:
            candidates.append(Path(program_files) / "GitHub CLI" / "gh.exe")
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "Programs" / "GitHub CLI" / "gh.exe"
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def require_gh() -> str:
    executable = find_gh()
    if executable is None:
        raise DatasetError("未找到 GitHub CLI（gh），请先安装并执行 gh auth login")
    result = subprocess.run(
        [executable, "auth", "status"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DatasetError("GitHub CLI 尚未登录，请先执行 gh auth login")
    return executable


def gh_release_view(executable: str, repo: str, tag: str) -> dict | None:
    result = subprocess.run(
        [
            executable,
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "assets,body,url",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "release not found" in combined or "not found" in combined:
            return None
        raise DatasetError(result.stderr.strip() or "查询 GitHub Release 失败")
    return json.loads(result.stdout)


def release_notes(specs: dict[str, DatasetSpec], published: set[str]) -> str:
    lines = [
        "# CMR 原始数据集",
        "",
        "本 Release 提供可直接下载和安装的原始图片与标注。项目特定的预处理特征或 PKL 不包含在数据包中。",
        "请运行 `python dataset.py install <DATASET> --output <DIR>` 下载、校验并安全解压。",
        "",
        f"> 页面底部的 Source code 由 GitHub 自动生成，不是数据集；请下载本 Release 中以 `{ASSET_PREFIX}` 开头的资产。",
        "",
        "## 发布状态",
        "",
        "| 数据集 | 状态 | 解压目录 | 上游来源 |",
        "|---|---|---|---|",
    ]
    for name, spec in specs.items():
        state = "已发布" if name in published else "待发布"
        lines.append(f"| `{name}` | {state} | `{spec.archive_root}/` | {spec.upstream} |")
    lines.extend(
        [
            "",
            "## 资产结构",
            "",
            "```text",
            f"{ASSET_PREFIX}<DATASET>.zip",
            f"{ASSET_PREFIX}<DATASET>.partNNN.zip",
            f"{ASSET_PREFIX}<DATASET>.manifest.json",
            f"{ASSET_PREFIX}<DATASET>.sha256",
            "```",
            "",
            "大数据集会拆成多个相互独立、可在 Windows 直接打开的 ZIP。下载脚本会按 manifest 顺序完整安装。",
        ]
    )
    return "\n".join(lines) + "\n"


def remote_asset_map(remote: dict | None) -> dict[str, dict]:
    return {
        str(asset.get("name")): asset
        for asset in (remote or {}).get("assets", [])
        if asset.get("name")
    }


def build_upload_plan(
    dataset: str,
    local_paths: Sequence[Path],
    remote_assets: dict[str, dict],
    *,
    replace: bool,
    local_digests: dict[str, str] | None = None,
) -> UploadPlan:
    skip: list[Path] = []
    upload: list[Path] = []
    replacements: list[Path] = []
    local_names = {path.name for path in local_paths}
    for path in local_paths:
        remote = remote_assets.get(path.name)
        if remote is None:
            upload.append(path)
            continue
        digest = (local_digests or {}).get(path.name) or sha256_file(path)
        local_digest = f"sha256:{digest}"
        same = (
            remote.get("digest") == local_digest
            and int(remote.get("size", -1)) == path.stat().st_size
            and remote.get("state") == "uploaded"
        )
        if same:
            skip.append(path)
        elif replace:
            replacements.append(path)
        else:
            raise DatasetError(
                f"远程同名资产内容不同：{path.name}。确认替换后使用 --replace。"
            )

    pattern = dataset_asset_pattern(dataset)
    stale = sorted(
        name
        for name in remote_assets
        if pattern.fullmatch(name) and name not in local_names
    )
    if stale and not replace:
        raise DatasetError(
            "远程存在本地清单不再使用的旧分包："
            + ", ".join(stale)
            + "。确认清理后使用 --replace。"
        )
    return UploadPlan(skip=skip, upload=upload, replace=replacements, delete=stale)


def run_gh(executable: str, arguments: Sequence[str], error_message: str) -> None:
    result = subprocess.run(
        [executable, *arguments],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise DatasetError(error_message)


def ensure_release(
    executable: str,
    repo: str,
    tag: str,
    title: str,
    specs: dict[str, DatasetSpec],
    remote: dict | None,
) -> dict:
    if remote is not None:
        return remote
    notes_path = ASSET_DIR / "release_notes.md"
    atomic_write_bytes(notes_path, release_notes(specs, set()).encode("utf-8"))
    run_gh(
        executable,
        [
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--target",
            "main",
            "--title",
            title,
            "--notes-file",
            str(notes_path),
        ],
        "创建 GitHub Release 失败",
    )
    created = gh_release_view(executable, repo, tag)
    if created is None:
        raise DatasetError("Release 创建后仍无法读取")
    return created


def upload_one_asset(
    executable: str,
    repo: str,
    tag: str,
    path: Path,
    *,
    replace: bool,
    expected_digest: str | None = None,
) -> None:
    remote_digest = f"sha256:{expected_digest or sha256_file(path)}"
    for attempt in range(1, 4):
        command = [executable, "release", "upload", tag, str(path), "--repo", repo]
        if replace or attempt > 1:
            command.append("--clobber")
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        remote = gh_release_view(executable, repo, tag)
        asset = remote_asset_map(remote).get(path.name)
        if (
            asset is not None
            and asset.get("state") == "uploaded"
            and asset.get("digest") == remote_digest
            and int(asset.get("size", -1)) == path.stat().st_size
        ):
            return
        if attempt < 3:
            reason = "远程摘要尚未匹配" if result.returncode == 0 else "上传连接中断"
            print(f"  {reason}，正在重试（{attempt}/3）……")
            time.sleep(attempt)
    if result.returncode == 0:
        raise DatasetError(f"上传后远程摘要不匹配：{path.name}")
    raise DatasetError(f"GitHub Release 上传失败：{path.name}")


def update_release_notes(
    executable: str,
    repo: str,
    tag: str,
    title: str,
    specs: dict[str, DatasetSpec],
    remote_assets: dict[str, dict],
) -> None:
    published = {
        name for name in specs if manifest_name(name) in remote_assets
    }
    notes_path = ASSET_DIR / "release_notes.md"
    atomic_write_bytes(notes_path, release_notes(specs, published).encode("utf-8"))
    run_gh(
        executable,
        [
            "release",
            "edit",
            tag,
            "--repo",
            repo,
            "--title",
            title,
            "--notes-file",
            str(notes_path),
        ],
        "资产已上传，但 Release 说明更新失败",
    )


def upload_dataset(
    spec: DatasetSpec,
    settings: dict,
    repo: str,
    *,
    replace: bool = False,
) -> str:
    if not spec.publish_enabled:
        raise DatasetError(
            f"{spec.name} 尚未完成再分发许可复核，datasets.json 中 publish_enabled=false，拒绝上传"
        )
    manifest, part_paths = validate_local_bundle(spec.name)
    local_paths = [
        *part_paths,
        ASSET_DIR / manifest_name(spec.name),
        ASSET_DIR / checksum_name(spec.name),
    ]
    # validate_local_bundle 已经核验过大分包，直接复用 manifest 中的摘要，
    # 避免上传前后重复读取几十 GiB 数据。
    local_digests = {
        str(part["name"]): str(part["sha256"])
        for part in manifest["archive"]["parts"]
    }
    local_digests[manifest_name(spec.name)] = parse_checksum_file(
        ASSET_DIR / checksum_name(spec.name)
    )[manifest_name(spec.name)]
    local_digests[checksum_name(spec.name)] = sha256_file(
        ASSET_DIR / checksum_name(spec.name)
    )
    executable = require_gh()
    tag = str(settings.get("release_tag", "datasets"))
    title = str(settings.get("release_title", "CMR Raw Datasets"))
    _, specs = load_settings()
    remote = ensure_release(
        executable,
        repo,
        tag,
        title,
        specs,
        gh_release_view(executable, repo, tag),
    )
    plan = build_upload_plan(
        spec.name,
        local_paths,
        remote_asset_map(remote),
        replace=replace,
        local_digests=local_digests,
    )

    for path in plan.skip:
        print(f"跳过远程相同资产：{path.name}")
    pending = [(path, False) for path in plan.upload] + [
        (path, True) for path in plan.replace
    ]
    for index, (path, use_clobber) in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] 正在上传 {path.name}（{format_size(path.stat().st_size)}）")
        upload_one_asset(
            executable,
            repo,
            tag,
            path,
            replace=use_clobber,
            expected_digest=local_digests[path.name],
        )

    for name in plan.delete:
        print(f"清理远程旧分包：{name}")
        run_gh(
            executable,
            ["release", "delete-asset", tag, name, "--repo", repo, "--yes"],
            f"删除远程旧分包失败：{name}",
        )

    final_remote = gh_release_view(executable, repo, tag)
    final_assets = remote_asset_map(final_remote)
    final_plan = build_upload_plan(
        spec.name,
        local_paths,
        final_assets,
        replace=False,
        local_digests=local_digests,
    )
    if final_plan.upload or final_plan.replace or final_plan.delete:
        raise DatasetError("远程资产终检未通过")
    update_release_notes(executable, repo, tag, title, specs, final_assets)
    return f"https://github.com/{repo}/releases/tag/{tag}"


# 下载、校验与安全安装


def github_release_json(repo: str, tag: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CMR_raw_dataset/dataset.py",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        executable = find_gh()
        if executable is not None:
            result = subprocess.run(
                [executable, "api", f"repos/{repo}/releases/tags/{tag}"],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            raise DatasetError(f"没有找到公开 Release：{repo} / {tag}") from exc
        raise DatasetError(f"无法读取 GitHub Release：{exc}") from exc


def download_file(
    url: str,
    target: Path,
    expected_size: int,
    *,
    repo: str,
    tag: str,
    asset_name_value: str,
    asset_identity: str | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.download")
    identity_path = target.with_name(f".{target.name}.download.json")
    identity = asset_identity or f"{url}|{expected_size}"
    current_identity = None
    if identity_path.is_file():
        try:
            current_identity = json.loads(identity_path.read_text(encoding="utf-8")).get("identity")
        except (OSError, json.JSONDecodeError, AttributeError):
            current_identity = None
    if current_identity != identity or (temp_path.is_file() and temp_path.stat().st_size > expected_size):
        temp_path.unlink(missing_ok=True)
    atomic_write_bytes(
        identity_path,
        (json.dumps({"identity": identity}, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            existing = temp_path.stat().st_size if temp_path.is_file() else 0
            if expected_size and existing == expected_size:
                os.replace(temp_path, target)
                identity_path.unlink(missing_ok=True)
                return
            headers = {"User-Agent": "CMR_raw_dataset/dataset.py"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                status = int(getattr(response, "status", response.getcode()))
                append = existing > 0 and status == 206
                if append:
                    content_range = str(response.headers.get("Content-Range", ""))
                    if not content_range.startswith(f"bytes {existing}-"):
                        raise DatasetError(f"服务器返回的续传范围无效：{asset_name_value}")
                mode = "ab" if append else "wb"
                downloaded = existing if append else 0
                started = time.monotonic()
                with temp_path.open(mode) as output:
                    while chunk := response.read(4 * 1024 * 1024):
                        output.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(time.monotonic() - started, 0.001)
                        transferred = downloaded - (existing if append else 0)
                        percent = (
                            f"{downloaded / expected_size * 100:5.1f}%"
                            if expected_size
                            else "  ---%"
                        )
                        print(
                            f"\r  {percent}  {format_size(downloaded):>10}  "
                            f"{format_size(int(transferred / elapsed))}/s",
                            end="",
                            flush=True,
                        )
            print()
            if expected_size and temp_path.stat().st_size != expected_size:
                raise DatasetError(f"下载大小不匹配：{asset_name_value}")
            os.replace(temp_path, target)
            identity_path.unlink(missing_ok=True)
            return
        except Exception as exc:  # 网络异常需要保留现场并有限重试
            print()
            last_error = exc
            if attempt < 3:
                print(f"下载连接中断，正在重试（{attempt}/3）……")
                time.sleep(attempt)

    executable = find_gh()
    if executable is not None:
        fallback_dir = Path(tempfile.mkdtemp(prefix="gh-download-", dir=CACHE_DIR))
        try:
            result = subprocess.run(
                [
                    executable,
                    "release",
                    "download",
                    tag,
                    "--repo",
                    repo,
                    "--pattern",
                    asset_name_value,
                    "--dir",
                    str(fallback_dir),
                ],
                cwd=PROJECT_ROOT,
                check=False,
            )
            downloaded = fallback_dir / asset_name_value
            if result.returncode == 0 and downloaded.is_file():
                if expected_size and downloaded.stat().st_size != expected_size:
                    raise DatasetError(f"gh 下载大小不匹配：{asset_name_value}")
                os.replace(downloaded, target)
                temp_path.unlink(missing_ok=True)
                identity_path.unlink(missing_ok=True)
                return
        finally:
            shutil.rmtree(fallback_dir, ignore_errors=True)
    raise DatasetError(f"下载失败：{asset_name_value}：{last_error}")


def prepare_remote_bundle(dataset: str, repo: str, tag: str) -> tuple[dict, list[Path]]:
    release = github_release_json(repo, tag)
    assets = {str(asset["name"]): asset for asset in release.get("assets", [])}
    required_small = [manifest_name(dataset), checksum_name(dataset)]
    cache = CACHE_DIR / dataset
    cache.mkdir(parents=True, exist_ok=True)
    for name in required_small:
        asset = assets.get(name)
        if asset is None:
            raise DatasetError(f"Release 中缺少资产：{name}")
        target = cache / name
        expected_size = int(asset.get("size", 0))
        # manifest 和校验表很小，每次刷新可正确感知远程资产替换，也能自愈损坏缓存。
        target.unlink(missing_ok=True)
        print(f"正在下载 {name}：{format_size(expected_size)}")
        download_file(
            str(asset["browser_download_url"]),
            target,
            expected_size,
            repo=repo,
            tag=tag,
            asset_name_value=name,
            asset_identity=str(
                asset.get("digest") or asset.get("updated_at") or asset.get("id")
            ),
        )

    manifest_path = cache / manifest_name(dataset)
    checksum_path = cache / checksum_name(dataset)
    checksums = parse_checksum_file(checksum_path)
    if checksums.get(manifest_path.name) != sha256_file(manifest_path):
        raise DatasetError(f"远程 manifest SHA-256 校验失败：{manifest_path.name}")
    manifest = load_manifest(manifest_path, dataset)
    part_paths: list[Path] = []
    for part in manifest["archive"]["parts"]:
        name = str(part["name"])
        asset = assets.get(name)
        if asset is None:
            raise DatasetError(f"Release 中缺少数据分卷：{name}")
        target = cache / name
        expected_size = int(part["bytes"])
        expected_hash = str(part["sha256"]).lower()
        valid_cache = (
            target.is_file()
            and target.stat().st_size == expected_size
            and sha256_file(target) == expected_hash
        )
        if valid_cache:
            print(f"复用缓存：{target.relative_to(PROJECT_ROOT)}")
        else:
            target.unlink(missing_ok=True)
            print(f"正在下载 {name}：{format_size(expected_size)}")
            download_file(
                str(asset["browser_download_url"]),
                target,
                expected_size,
                repo=repo,
                tag=tag,
                asset_name_value=name,
                asset_identity=str(
                    asset.get("digest") or asset.get("updated_at") or asset.get("id")
                ),
            )
            if sha256_file(target) != expected_hash:
                target.unlink(missing_ok=True)
                raise DatasetError(f"下载分卷 SHA-256 校验失败：{name}")
        if checksums.get(name) != expected_hash:
            raise DatasetError(f"SHA-256 清单与 manifest 不一致：{name}")
        part_paths.append(target)
    return manifest, part_paths


def safe_archive_path(name: str, expected_root: str) -> tuple[PurePosixPath, str | None]:
    if "\\" in name:
        raise DatasetError(f"归档包含非标准路径分隔符：{name}")
    pure = PurePosixPath(name.rstrip("/"))
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise DatasetError(f"归档包含不安全路径：{name}")
    if re.match(r"^[A-Za-z]:", pure.parts[0]):
        raise DatasetError(f"归档包含盘符路径：{name}")
    if pure.parts[0] != expected_root:
        raise DatasetError(f"归档内容不属于 {expected_root}/：{name}")
    relative = PurePosixPath(*pure.parts[1:]).as_posix() if len(pure.parts) > 1 else None
    return pure, relative


def manifest_file_map(manifest: dict) -> dict[str, dict]:
    expected: dict[str, dict] = {}
    folded: set[str] = set()
    for raw in manifest["files"]:
        if not isinstance(raw, dict):
            raise DatasetError("manifest 文件记录无效")
        path = str(raw.get("path", ""))
        pure = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or pure.is_absolute()
            or ".." in pure.parts
            or PurePosixPath(*pure.parts).as_posix() != path
        ):
            raise DatasetError(f"manifest 包含不安全路径：{path}")
        key = path.casefold()
        if key in folded:
            raise DatasetError(f"manifest 包含大小写冲突路径：{path}")
        folded.add(key)
        size = raw.get("bytes")
        digest = str(raw.get("sha256", ""))
        if not isinstance(size, int) or size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DatasetError(f"manifest 文件记录无效：{path}")
        expected[path] = raw
    return expected


def verify_dataset_tree(
    manifest: dict,
    output_root: Path,
    *,
    deep: bool,
    quiet: bool = False,
) -> tuple[int, int]:
    dataset = str(manifest["dataset"])
    target = output_root / dataset
    if not target.is_dir():
        raise DatasetError(f"数据集目录不存在：{target}")
    expected = manifest_file_map(manifest)
    actual_records = scan_source(target)
    actual = {record.relative_path: record for record in actual_records}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        details = []
        if missing:
            details.append(f"缺少 {len(missing)} 个文件（例如 {missing[0]}）")
        if extra:
            details.append(f"多出 {len(extra)} 个文件（例如 {extra[0]}）")
        raise DatasetError("，".join(details))
    total = len(expected)
    total_bytes = 0
    for index, (path, raw) in enumerate(expected.items(), start=1):
        record = actual[path]
        if record.size != int(raw["bytes"]):
            raise DatasetError(f"文件大小不匹配：{path}")
        if deep and sha256_file(record.path) != raw["sha256"]:
            raise DatasetError(f"文件 SHA-256 不匹配：{path}")
        total_bytes += record.size
        if not quiet and deep:
            print_file_progress("校验进度", index, total)
    return total, total_bytes


def install_bundle(
    manifest: dict,
    part_paths: Sequence[Path],
    output_root: Path,
    *,
    force: bool = False,
) -> Path:
    dataset = str(manifest["dataset"])
    archive_root = str(manifest["archive"]["root"])
    if archive_root != dataset:
        raise DatasetError("manifest 的 archive root 与数据集名不一致")
    expected = manifest_file_map(manifest)
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / dataset
    marker_dir = output_root / ".cmr_dataset_manifests"
    marker_path = marker_dir / f"{dataset}.json"
    if target.exists() and marker_path.is_file() and not force:
        current = load_manifest(marker_path, dataset)
        if current["archive"]["bundle_sha256"] == manifest["archive"]["bundle_sha256"]:
            verify_dataset_tree(manifest, output_root, deep=False, quiet=True)
            print(f"本地已安装相同数据集，结构与大小校验通过：{target}")
            return target
    if target.exists() and not force:
        raise DatasetError(f"目标目录已存在，默认不覆盖：{target}。确认后使用 --force。")

    staging = Path(tempfile.mkdtemp(prefix=f".{dataset}.install-", dir=output_root))
    seen: set[str] = set()
    try:
        for part_path in part_paths:
            try:
                archive = zipfile.ZipFile(part_path, "r")
            except zipfile.BadZipFile as exc:
                raise DatasetError(f"ZIP 文件损坏：{part_path.name}") from exc
            with archive:
                for info in archive.infolist():
                    _, relative = safe_archive_path(info.filename, archive_root)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise DatasetError(f"ZIP 包含符号链接：{info.filename}")
                    if info.flag_bits & 0x1:
                        raise DatasetError(f"ZIP 包含加密文件：{info.filename}")
                    if info.is_dir():
                        destination = staging / Path(*PurePosixPath(info.filename.rstrip("/")).parts)
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if relative is None:
                        raise DatasetError(f"ZIP 包含不支持的条目：{info.filename}")
                    raw = expected.get(relative)
                    if raw is None:
                        raise DatasetError(f"ZIP 包含 manifest 未登记的文件：{info.filename}")
                    if relative in seen:
                        raise DatasetError(f"多个 ZIP 包含重复文件：{info.filename}")
                    if info.file_size != int(raw["bytes"]):
                        raise DatasetError(f"ZIP 文件大小与 manifest 不一致：{info.filename}")
                    destination = staging / Path(*PurePosixPath(info.filename).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with archive.open(info, "r") as source, destination.open("wb") as output:
                        while chunk := source.read(4 * 1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                    if digest.hexdigest() != raw["sha256"]:
                        raise DatasetError(f"解压文件 SHA-256 不匹配：{info.filename}")
                    seen.add(relative)
                    count = len(seen)
                    print_file_progress("安装进度", count, len(expected))
        missing = sorted(set(expected) - seen)
        if missing:
            raise DatasetError(f"归档缺少 {len(missing)} 个文件，例如：{missing[0]}")

        staging_target = staging / dataset
        if not staging_target.is_dir():
            raise DatasetError(f"归档没有生成预期目录：{dataset}")
        backup: Path | None = None
        if target.exists():
            backup = output_root / f".{dataset}.backup-{time.strftime('%Y%m%d-%H%M%S')}"
            if backup.exists():
                raise DatasetError(f"备份目录已存在：{backup}")
            os.replace(target, backup)
        try:
            os.replace(staging_target, target)
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        marker_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            marker_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        print(f"安装完成：{target}")
        if backup is not None:
            print(f"原目录已保留为备份：{backup}")
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# 中文交互菜单与命令行入口


def confirm(message: str) -> bool:
    try:
        return input(f"{message}\n输入 1 确认，直接回车不执行：").strip() == "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def choose_menu_option(
    prompt: str,
    valid: set[str],
    *,
    default: str | None = None,
    zero_action: str = "返回",
) -> str | None:
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw and default is not None:
            return default
        if raw == "0":
            return None
        if raw in valid:
            return raw
        choices = "、".join(sorted(valid))
        print(f"请输入 {choices} 中的一个编号，或输入 0 {zero_action}。")


def choose_dataset(
    specs: dict[str, DatasetSpec],
    *,
    show_upload_status: bool = False,
) -> DatasetSpec | None:
    values = list(specs.values())
    if not values:
        raise DatasetError("当前没有可选择的数据集")
    print("请选择数据集：")
    for index, spec in enumerate(values, start=1):
        suffix = ""
        if show_upload_status:
            status = (
                "可制作、可上传 GitHub"
                if spec.publish_enabled
                else "可制作；GitHub 上传暂未开放"
            )
            suffix = f"（{status}）"
        print(f"  {index}. {spec.name}{suffix}")
        print(f"     {spec.description}")
    print("  0. 返回主菜单")
    choice = choose_menu_option(
        "请输入编号：",
        {str(index) for index in range(1, len(values) + 1)},
    )
    return values[int(choice) - 1] if choice is not None else None


def prompt_path(message: str, default: Path) -> Path | None:
    try:
        raw = input(
            f"{message}（默认 {default}，直接回车使用默认，输入 0 返回主菜单）："
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw == "0":
        return None
    return Path(raw) if raw else default


def detected_source_path(spec: DatasetSpec) -> Path | None:
    """优先寻找仓库附近的同名原始文件夹或 ZIP，不写死具体磁盘。"""
    candidates = [
        DEFAULT_RAW_DIR / f"{spec.name}.zip",
        DEFAULT_RAW_DIR / spec.name,
        PROJECT_ROOT.parent / f"{spec.name}.zip",
        PROJECT_ROOT.parent / spec.name,
        PROJECT_ROOT / f"{spec.name}.zip",
        PROJECT_ROOT / spec.name,
        PROJECT_ROOT / f"{asset_base(spec.name)}.zip",
    ]
    return next((path for path in candidates if path.exists()), None)


def default_source_path(spec: DatasetSpec) -> Path:
    return detected_source_path(spec) or (DEFAULT_RAW_DIR / spec.name)


def configured_part_size(settings: dict, override_mib: int | None = None) -> int:
    size_mib = int(settings.get("part_size_mib", 1900)) if override_mib is None else override_mib
    if size_mib < 1 or size_mib > 1900:
        raise DatasetError("part-size-mib 必须在 1 到 1900 之间")
    return size_mib * 1024 * 1024


def local_bundle_summary(dataset: str) -> str:
    paths = dataset_asset_paths(dataset)
    if not paths:
        return "未生成"
    try:
        manifest = load_manifest(ASSET_DIR / manifest_name(dataset), dataset)
        expected = {
            *(str(part["name"]) for part in manifest["archive"]["parts"]),
            manifest_name(dataset),
            checksum_name(dataset),
        }
        actual = {path.name for path in paths}
        if actual != expected:
            return f"不完整（当前 {len(actual)} 个文件）"
        part_count = len(manifest["archive"]["parts"])
        total = sum(
            path.stat().st_size
            for path in paths
            if path.suffix.casefold() == ".zip"
        )
        kind = "小数据单 ZIP" if part_count == 1 else f"大数据 {part_count} 个独立 ZIP"
        return f"{kind}，{format_size(total)}"
    except (DatasetError, OSError, KeyError, TypeError, ValueError):
        return f"不完整（当前 {len(paths)} 个文件）"


def remote_bundle_summary(dataset: str, assets: dict[str, dict] | None) -> str:
    if assets is None:
        return "未查询"
    names = set(assets)
    if manifest_name(dataset) not in names or checksum_name(dataset) not in names:
        return "未发布"
    zip_count = sum(
        1
        for name in names
        if dataset_asset_pattern(dataset).fullmatch(name) and name.endswith(".zip")
    )
    return f"已发布（{zip_count} 个 ZIP）" if zip_count else "资产不完整"


def print_dataset_status(
    settings: dict,
    specs: dict[str, DatasetSpec],
    *,
    check_remote: bool = False,
) -> None:
    remote_assets: dict[str, dict] | None = None
    if check_remote:
        repo = str(settings["repository"])
        tag = str(settings["release_tag"])
        try:
            remote_assets = remote_asset_map(github_release_json(repo, tag))
        except DatasetError as exc:
            print(f"远程状态读取失败：{exc}")
    print("数据集状态：")
    print(
        "说明：上传状态来自 datasets.json，只控制本工具能否上传到 GitHub，"
        "不影响本机制作、转换或使用数据集。"
    )
    for spec in specs.values():
        source = detected_source_path(spec)
        if source is None:
            source_text = "未在默认位置找到"
        elif source.is_file():
            source_text = f"ZIP，{format_size(source.stat().st_size)}，{source}"
        else:
            source_text = f"文件夹，{source}"
        permission = (
            "已开放"
            if spec.publish_enabled
            else "暂未开放（仍可在本机制作和使用）"
        )
        print(f"\n{spec.name}  |  {spec.description}")
        print(f"  来源文件：{source_text}")
        print(f"  本机数据包：{local_bundle_summary(spec.name)}")
        print(f"  GitHub：{remote_bundle_summary(spec.name, remote_assets)}")
        print(f"  上传状态：{permission}")
    print(
        f"\nRelease：https://github.com/{settings['repository']}/releases/tag/"
        f"{settings['release_tag']}"
    )


def published_dataset_specs(
    settings: dict,
    specs: dict[str, DatasetSpec],
) -> dict[str, DatasetSpec]:
    release = github_release_json(
        str(settings["repository"]),
        str(settings["release_tag"]),
    )
    assets = remote_asset_map(release)
    return {
        name: spec
        for name, spec in specs.items()
        if remote_bundle_summary(name, assets).startswith("已发布")
    }


def local_bundle_specs(specs: dict[str, DatasetSpec]) -> dict[str, DatasetSpec]:
    available: dict[str, DatasetSpec] = {}
    for name, spec in specs.items():
        summary = local_bundle_summary(name)
        if summary != "未生成" and not summary.startswith("不完整"):
            available[name] = spec
    return available


def interactive_install(settings: dict, specs: dict[str, DatasetSpec]) -> None:
    print("安装来源：")
    print("  1. 从 GitHub 下载（推荐）")
    print("  2. 使用本机已整理的发布包（发布者测试用）")
    print("  0. 返回主菜单")
    source_choice = choose_menu_option(
        "请输入编号（直接回车选择 1）：",
        {"1", "2"},
        default="1",
    )
    if source_choice is None:
        return

    available = (
        published_dataset_specs(settings, specs)
        if source_choice == "1"
        else local_bundle_specs(specs)
    )
    if not available:
        raise DatasetError("当前来源中没有可安装的数据集")
    spec = choose_dataset(available)
    if spec is None:
        return
    output = prompt_path("安装到哪个根目录", DEFAULT_INSTALL_DIR)
    if output is None:
        return
    target_exists = (output / spec.name).exists()
    force = target_exists and confirm("目标已存在，是否保留备份后更新？")
    if target_exists and not force:
        return
    if source_choice == "1":
        manifest, parts = prepare_remote_bundle(
            spec.name,
            str(settings["repository"]),
            str(settings["release_tag"]),
        )
    else:
        manifest, parts = validate_local_bundle(spec.name)
    install_bundle(manifest, parts, output, force=force)


def interactive_upload(settings: dict, spec: DatasetSpec, *, ask_start: bool = True) -> None:
    if not spec.publish_enabled:
        raise DatasetError(f"{spec.name} 尚未完成再分发许可复核，不能上传")
    if ask_start and not confirm(f"是否把 {spec.name} 发布到 GitHub？"):
        return
    replace = confirm("如果远程已有旧版本，是否允许用本地版本替换？")
    print(
        upload_dataset(
            spec,
            settings,
            str(settings["repository"]),
            replace=replace,
        )
    )


def interactive_prepare(settings: dict, spec: DatasetSpec) -> None:
    existing = dataset_asset_paths(spec.name)
    print(f"\n已选择：{spec.name}")
    print(f"  本机数据包：{local_bundle_summary(spec.name)}")
    if spec.publish_enabled:
        print("  GitHub 上传：已开放")
    else:
        print("  GitHub 上传：暂未开放；仍可正常制作本机数据包")

    if existing:
        print("请选择下一步：")
        if spec.publish_enabled:
            print("  1. 上传现有数据包")
            print("  2. 从原始数据重新制作")
            valid_actions = {"1", "2"}
            remake_action = "2"
        else:
            print("  1. 从原始数据重新制作并替换现有数据包")
            valid_actions = {"1"}
            remake_action = "1"
        print("  0. 返回主菜单")
        action = choose_menu_option("请输入编号：", valid_actions)
        if spec.publish_enabled and action == "1":
            interactive_upload(settings, spec)
            return
        if action != remake_action:
            return

    source = prompt_path("原始文件夹或 ZIP 在哪里", default_source_path(spec))
    if source is None:
        return
    source = source.expanduser().resolve()
    print("\n本次将执行：")
    print(f"  读取：{source}")
    print(f"  保存：{ASSET_DIR}")
    print("  打包：自动选择一个 ZIP 或多个小 ZIP，并生成校验文件")
    if existing:
        print("  替换：现有同名数据包会先备份，新包成功后再安全替换")
    if spec.publish_enabled:
        print("  上传：制作完成后再询问是否上传 GitHub")
    else:
        print("  上传：不会上传；该数据集的 GitHub 上传功能暂未开放")
    print("  1. 开始制作")
    print("  0. 返回主菜单")
    if choose_menu_option("请输入编号：", {"1"}) != "1":
        return
    pack_dataset(
        spec,
        source,
        configured_part_size(settings),
        force=bool(existing),
    )
    if not spec.publish_enabled:
        print(f"制作完成：数据包已保存到 {ASSET_DIR}")
        print("GitHub 上传暂未开放，本次操作已完成，没有上传任何文件。")
        return
    if confirm("整理完成，是否现在上传？"):
        interactive_upload(settings, spec, ask_start=False)


def interactive_convert(settings: dict, spec: DatasetSpec) -> None:
    print("来源可以是原目录、单 ZIP、发布包目录、manifest 或任意 part ZIP。")
    source = prompt_path("要转换的内容在哪里", default_source_path(spec))
    if source is None:
        return
    print("转换成：")
    print("  1. 普通文件夹")
    print("  2. 一个完整 ZIP（本地保存）")
    print("  3. 多个小 ZIP（适合 GitHub 发布）")
    print("  0. 返回主菜单")
    target_formats = {"1": "directory", "2": "single-zip", "3": "split-zip"}
    target_choice = choose_menu_option("请输入编号：", set(target_formats))
    if target_choice is None:
        return
    target_format = target_formats[target_choice]
    conversion_root = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}_converted"
    defaults = {
        "directory": conversion_root,
        "single-zip": conversion_root / f"{spec.name}.zip",
        "split-zip": conversion_root / spec.name,
    }
    output = prompt_path("保存到哪里", defaults[target_format])
    if output is None:
        return
    output = output.expanduser().resolve()
    if target_format == "directory":
        target_exists = (output / spec.name).exists()
    elif target_format == "single-zip":
        target_exists = output.exists()
    else:
        target_exists = bool(dataset_asset_paths(spec.name, output))
    force = target_exists and confirm("目标已有同名内容，是否安全替换？")
    if target_exists and not force:
        return
    if not confirm(f"确认开始转换并保存到 {output}？"):
        return
    convert_dataset(
        spec,
        source,
        target_format,
        output,
        configured_part_size(settings),
        force=force,
    )


def interactive_verify(spec: DatasetSpec) -> None:
    root = prompt_path("数据集安装在哪个根目录", DEFAULT_INSTALL_DIR)
    if root is None:
        return
    marker = root / ".cmr_dataset_manifests" / f"{spec.name}.json"
    manifest = load_manifest(marker, spec.name)
    deep = confirm("是否执行较慢但更完整的逐文件 SHA-256 校验？")
    count, size = verify_dataset_tree(manifest, root.resolve(), deep=deep)
    print(f"验证通过：{count} 个文件，{format_size(size)}")


def interactive_check(settings: dict, specs: dict[str, DatasetSpec]) -> None:
    print("检查内容：")
    print("  1. 查看原始文件、本机数据包和 GitHub 发布状态")
    print("  2. 验证已安装的数据目录是否完整")
    print("  0. 返回主菜单")
    action = choose_menu_option("请输入编号：", {"1", "2"})
    if action == "1":
        print("正在查询本机和 GitHub 状态……")
        print_dataset_status(settings, specs, check_remote=True)
        return
    if action == "2":
        spec = choose_dataset(specs)
        if spec is not None:
            interactive_verify(spec)


def interactive() -> None:
    settings, specs = load_settings()
    while True:
        print("\nCMR 原始数据集工具")
        print("请选择你想完成的事情：")
        print("  1. 下载并安装数据集（普通使用者选这个）")
        print("  2. 制作或上传数据包（发布者使用）")
        print("  3. 转换文件夹或 ZIP")
        print("  4. 数据集状态与完整性检查")
        print("  0. 退出")
        choice = choose_menu_option(
            "请输入编号：",
            {"1", "2", "3", "4"},
            zero_action="退出",
        )
        if choice is None:
            print("已退出。")
            return
        try:
            if choice == "1":
                interactive_install(settings, specs)
                continue
            if choice == "4":
                interactive_check(settings, specs)
                continue
            spec = choose_dataset(specs, show_upload_status=(choice == "2"))
            if spec is None:
                continue
            if choice == "2":
                interactive_prepare(settings, spec)
            else:
                interactive_convert(settings, spec)
        except DatasetError as exc:
            print(f"操作未完成：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser(
        "status",
        help="查看原始数据、本地发布包和远程状态",
    )
    status_parser.add_argument("--remote", action="store_true", help="同时联网查询 Release")
    list_parser = subparsers.add_parser("list", help="兼容旧命令：查看本地状态")
    list_parser.add_argument("--remote", action="store_true", help="同时联网查询 Release")

    inspect_parser = subparsers.add_parser("inspect", help="只读检查原始文件夹或 ZIP")
    inspect_parser.add_argument("dataset")
    inspect_parser.add_argument("--source", type=Path, default=None)

    pack_parser = subparsers.add_parser("pack", help="自动生成单 ZIP 或多个独立 ZIP")
    pack_parser.add_argument("dataset")
    pack_parser.add_argument("--source", type=Path, default=None)
    pack_parser.add_argument("--force", action="store_true", help="替换本地同名发布资产")
    pack_parser.add_argument(
        "--part-size-mib",
        type=int,
        default=None,
        help="测试或特殊场景使用的分卷上限；正式发布默认 1900 MiB",
    )

    convert_parser = subparsers.add_parser(
        "convert",
        help="在原目录、单个大 ZIP 和多个独立小 ZIP 之间转换",
    )
    convert_parser.add_argument("dataset")
    convert_parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="文件夹、单 ZIP、发布包目录、manifest 或任意 part ZIP",
    )
    convert_parser.add_argument(
        "--to",
        choices=("directory", "single-zip", "split-zip"),
        required=True,
    )
    convert_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="目录目标填写安装根目录；大 ZIP 填写 .zip；多 ZIP 填写资产目录",
    )
    convert_parser.add_argument("--force", action="store_true", help="安全替换同名目标")
    convert_parser.add_argument(
        "--part-size-mib",
        type=int,
        default=None,
        help="目标为 split-zip 时的单包上限，默认 1900 MiB",
    )

    upload_parser = subparsers.add_parser("upload", help="上传已校验的本地资产")
    upload_parser.add_argument("dataset")
    upload_parser.add_argument("--repo", default=None)
    upload_parser.add_argument("--replace", action="store_true", help="替换远程同名资产")
    upload_parser.add_argument("--yes", action="store_true", help="跳过上传确认")

    install_parser = subparsers.add_parser("install", help="下载、校验并安全安装数据集")
    install_parser.add_argument("dataset")
    install_parser.add_argument("--output", type=Path, required=True)
    install_parser.add_argument("--repo", default=None)
    install_parser.add_argument(
        "--local-assets",
        type=Path,
        default=None,
        help="从本地发布目录安装，用于发布前回归测试",
    )
    install_parser.add_argument("--force", action="store_true", help="保留备份后替换已有目录")

    verify_parser = subparsers.add_parser("verify", help="验证已安装的数据集")
    verify_parser.add_argument("dataset")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--deep", action="store_true", help="逐文件计算 SHA-256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        interactive()
        return 0
    settings, specs = load_settings()
    if args.command in {"status", "list"}:
        print_dataset_status(settings, specs, check_remote=args.remote)
        return 0
    if args.dataset not in specs:
        raise DatasetError(
            f"未知数据集：{args.dataset}。可选值：{', '.join(specs)}"
        )
    spec = specs[args.dataset]
    if args.command == "inspect":
        part_size = configured_part_size(settings)
        inspect_dataset_source(
            spec,
            args.source or default_source_path(spec),
            part_size,
        )
    elif args.command == "pack":
        pack_dataset(
            spec,
            args.source or default_source_path(spec),
            configured_part_size(settings, args.part_size_mib),
            force=args.force,
        )
    elif args.command == "convert":
        convert_dataset(
            spec,
            args.source or default_source_path(spec),
            args.to,
            args.output,
            configured_part_size(settings, args.part_size_mib),
            force=args.force,
        )
    elif args.command == "upload":
        repo = args.repo or str(settings["repository"])
        if not args.yes and not confirm(
            f"即将把 {spec.name} 上传到 {repo} 的 {settings['release_tag']} Release，是否继续？"
        ):
            print("已取消。")
            return 0
        print(upload_dataset(spec, settings, repo, replace=args.replace))
    elif args.command == "install":
        if args.local_assets is not None:
            manifest, parts = validate_local_bundle(spec.name, args.local_assets.resolve())
        else:
            repo = args.repo or str(settings["repository"])
            manifest, parts = prepare_remote_bundle(
                spec.name,
                repo,
                str(settings["release_tag"]),
            )
        install_bundle(manifest, parts, args.output, force=args.force)
    elif args.command == "verify":
        marker = args.root.resolve() / ".cmr_dataset_manifests" / f"{spec.name}.json"
        manifest = load_manifest(marker, spec.name)
        count, size = verify_dataset_tree(
            manifest,
            args.root.resolve(),
            deep=args.deep,
        )
        mode = "SHA-256 深度" if args.deep else "结构与大小"
        print(f"{spec.name} {mode}验证通过：{count} 个文件，{format_size(size)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DatasetError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
