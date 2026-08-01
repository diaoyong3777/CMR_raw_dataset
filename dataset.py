#!/usr/bin/env python3
"""CMR_raw_dataset 的检查、打包、发布、下载和安装工具。

直接运行 ``python dataset.py`` 使用交互菜单；也可以使用子命令自动化。
脚本只读取原始数据目录，所有中间文件都写在本仓库的忽略目录中。
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
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "datasets.json"
ASSET_DIR = PROJECT_ROOT / "release_assets"
CACHE_DIR = PROJECT_ROOT / ".dataset_cache"
DEFAULT_RAW_DIR = PROJECT_ROOT / "raw_dataset"
ASSET_PREFIX = "CMR_raw_dataset-"
MANIFEST_SCHEMA = 1
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class DatasetError(RuntimeError):
    """可以直接向使用者展示的预期错误。"""


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    description: str
    archive_root: str
    publish_enabled: bool
    upstream: str


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    size: int


@dataclass(frozen=True)
class TempPart:
    path: Path
    size: int
    sha256: str


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


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
        archive_root = str(raw.get("archive_root", ""))
        if archive_root != name:
            raise DatasetError(f"{name} 的 archive_root 必须与数据集名一致")
        specs[name] = DatasetSpec(
            name=name,
            description=str(raw.get("description", "")),
            archive_root=archive_root,
            publish_enabled=bool(raw.get("publish_enabled", False)),
            upstream=str(raw.get("upstream", "")),
        )
    return settings, specs


def require_dataset(name: str) -> tuple[dict, DatasetSpec]:
    settings, specs = load_settings()
    if name not in specs:
        raise DatasetError(
            f"未知数据集：{name}。可选值：{', '.join(specs)}"
        )
    return settings, specs[name]


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


def print_source_report(dataset: str, source: Path, records: Sequence[SourceFile]) -> None:
    total_size = sum(record.size for record in records)
    suffixes: dict[str, int] = {}
    for record in records:
        suffix = Path(record.relative_path).suffix.lower() or "[无扩展名]"
        suffixes[suffix] = suffixes.get(suffix, 0) + 1
    common = sorted(suffixes.items(), key=lambda item: (-item[1], item[0]))[:8]
    types = "，".join(f"{suffix}={count}" for suffix, count in common)
    print(f"{dataset} 检查通过")
    print(f"  来源：{source.expanduser().resolve()}")
    print(f"  文件：{len(records)}")
    print(f"  大小：{format_size(total_size)}")
    print(f"  类型：{types}")


def asset_base(dataset: str) -> str:
    return f"{ASSET_PREFIX}{dataset}"


def manifest_name(dataset: str) -> str:
    return f"{asset_base(dataset)}.manifest.json"


def checksum_name(dataset: str) -> str:
    return f"{asset_base(dataset)}.sha256"


def dataset_asset_paths(dataset: str) -> list[Path]:
    if not ASSET_DIR.is_dir():
        return []
    base = re.escape(asset_base(dataset))
    pattern = re.compile(
        rf"^{base}(?:\.zip|\.part\d{{3}}\.zip|\.manifest\.json|\.sha256)$"
    )
    return sorted(
        (path for path in ASSET_DIR.iterdir() if path.is_file() and pattern.fullmatch(path.name)),
        key=lambda path: path.name,
    )


def split_file_groups(
    records: Sequence[SourceFile], part_size: int, archive_root: str
) -> list[list[SourceFile]]:
    """按未压缩大小和 ZIP 元数据开销预分组，每组都会生成独立可打开的 ZIP。"""
    groups: list[list[SourceFile]] = []
    current: list[SourceFile] = []
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


def pack_dataset(
    spec: DatasetSpec,
    source: Path,
    part_size: int,
    *,
    force: bool = False,
) -> dict:
    source = source.expanduser().resolve()
    records = scan_source(source)
    print_source_report(spec.name, source, records)
    existing = dataset_asset_paths(spec.name)
    if existing and not force:
        raise DatasetError(
            "本地已有同名发布文件，默认不覆盖："
            + ", ".join(path.name for path in existing)
            + "。确认后使用 --force。"
        )

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    base = asset_base(spec.name)
    file_manifest: list[dict[str, str | int]] = []
    temp_paths: list[Path] = []
    started = time.monotonic()
    try:
        groups = split_file_groups(records, part_size, spec.archive_root)
        temp_parts: list[TempPart] = []
        processed = 0
        total = len(records)
        token = uuid.uuid4().hex
        for part_index, group in enumerate(groups, start=1):
            temp_path = ASSET_DIR / f".{base}.{token}.part{part_index:03d}.tmp"
            temp_paths.append(temp_path)
            with zipfile.ZipFile(
                temp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=1,
                allowZip64=True,
            ) as archive:
                for record in group:
                    archive_path = f"{spec.archive_root}/{record.relative_path}"
                    digest = hashlib.sha256()
                    bytes_read = 0
                    with record.path.open("rb") as source_file, archive.open(
                        zip_info_for(archive_path), "w", force_zip64=True
                    ) as output:
                        while chunk := source_file.read(4 * 1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                            bytes_read += len(chunk)
                    if bytes_read != record.size:
                        raise DatasetError(
                            f"打包期间文件大小发生变化：{record.relative_path}"
                        )
                    file_manifest.append(
                        {
                            "path": record.relative_path,
                            "bytes": record.size,
                            "sha256": digest.hexdigest(),
                        }
                    )
                    processed += 1
                    if processed == 1 or processed == total or processed % 500 == 0:
                        print(f"  [{processed}/{total}] {record.relative_path}")
            actual_size = temp_path.stat().st_size
            if actual_size > part_size:
                raise DatasetError(
                    "ZIP 元数据使分包超过设定上限，请减小分包目标后重试："
                    f"{format_size(actual_size)} > {format_size(part_size)}"
                )
            if actual_size >= 2 * 1024 * 1024 * 1024:
                raise DatasetError(
                    f"ZIP 分包超过 GitHub 2 GiB 硬限制：{format_size(actual_size)}"
                )
            temp_parts.append(
                TempPart(temp_path, actual_size, sha256_file(temp_path))
            )

        if len(temp_parts) == 1:
            final_part_names = [f"{base}.zip"]
        else:
            final_part_names = [
                f"{base}.part{index:03d}.zip"
                for index in range(1, len(temp_parts) + 1)
            ]
        parts = [
            {"name": name, "bytes": part.size, "sha256": part.sha256}
            for name, part in zip(final_part_names, temp_parts)
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
                "file_count": len(records),
                "bytes": sum(record.size for record in records),
            },
            "files": file_manifest,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        manifest_temp = ASSET_DIR / f".{manifest_name(spec.name)}.{uuid.uuid4().hex}.tmp"
        manifest_temp.write_bytes(manifest_bytes)
        temp_paths.append(manifest_temp)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        checksum_lines = [
            f"{part['sha256']}  {part['name']}" for part in parts
        ]
        checksum_lines.append(f"{manifest_hash}  {manifest_name(spec.name)}")
        checksum_bytes = ("\n".join(checksum_lines) + "\n").encode("utf-8")
        checksum_temp = ASSET_DIR / f".{checksum_name(spec.name)}.{uuid.uuid4().hex}.tmp"
        checksum_temp.write_bytes(checksum_bytes)
        temp_paths.append(checksum_temp)

        # 所有新文件成功写完后才替换旧资产，避免中途失败破坏已有发布包。
        for path in existing:
            path.unlink()
        final_paths: list[Path] = []
        for temp_part, final_name in zip(temp_parts, final_part_names):
            final_path = ASSET_DIR / final_name
            os.replace(temp_part.path, final_path)
            final_paths.append(final_path)
            temp_paths.remove(temp_part.path)
        final_manifest = ASSET_DIR / manifest_name(spec.name)
        os.replace(manifest_temp, final_manifest)
        temp_paths.remove(manifest_temp)
        final_checksum = ASSET_DIR / checksum_name(spec.name)
        os.replace(checksum_temp, final_checksum)
        temp_paths.remove(checksum_temp)

        elapsed = max(time.monotonic() - started, 0.001)
        print(f"已生成 {len(final_paths)} 个数据分卷：")
        for path in final_paths:
            print(f"  {path.name}  {format_size(path.stat().st_size)}")
        print(f"  {final_manifest.name}")
        print(f"  {final_checksum.name}")
        print(
            f"ZIP 总大小：{format_size(sum(part.size for part in temp_parts))}，"
            f"耗时 {elapsed:.1f} 秒，bundle SHA-256={bundle_hash}"
        )
        return manifest
    except Exception:
        for path in temp_paths:
            path.unlink(missing_ok=True)
        raise


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
    if not isinstance(archive.get("parts"), list) or not archive["parts"]:
        raise DatasetError(f"manifest 没有数据分卷：{path.name}")
    if not isinstance(files, list) or not files:
        raise DatasetError(f"manifest 没有文件清单：{path.name}")
    return manifest


def validate_local_bundle(dataset: str, directory: Path = ASSET_DIR) -> tuple[dict, list[Path]]:
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
        if checksums.get(name) != expected or sha256_file(path) != expected:
            raise DatasetError(f"数据分卷 SHA-256 校验失败：{name}")
        part_paths.append(path)
    return manifest, part_paths


def find_gh() -> str | None:
    executable = shutil.which("gh")
    if executable:
        return executable
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "GitHub CLI"
            / "gh.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "GitHub CLI"
            / "gh.exe",
        ]
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
        "本 Release 为 CMR_Bench 提供原始图片和标注。统一 `.pkl` 仍由 CMR_Bench 仓库管理。",
        "请运行 `python dataset.py install <DATASET> --output <DIR>` 下载、校验并安全解压。",
        "",
        "> 页面底部的 Source code 由 GitHub 自动生成，不是数据集；请下载本 Release 中以 `CMR_raw_dataset-` 开头的资产。",
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
            "CMR_raw_dataset-<DATASET>.zip",
            "CMR_raw_dataset-<DATASET>.partNNN.zip",
            "CMR_raw_dataset-<DATASET>.manifest.json",
            "CMR_raw_dataset-<DATASET>.sha256",
            "```",
            "",
            "大数据集会拆成多个相互独立、可在 Windows 直接打开的 ZIP。下载脚本会按 manifest 顺序完整安装。",
        ]
    )
    return "\n".join(lines) + "\n"


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
    manifest_path = ASSET_DIR / manifest_name(spec.name)
    checksum_path = ASSET_DIR / checksum_name(spec.name)
    upload_paths = [*part_paths, manifest_path, checksum_path]
    executable = require_gh()
    tag = str(settings.get("release_tag", "datasets"))
    title = str(settings.get("release_title", "CMR Raw Datasets"))
    remote = gh_release_view(executable, repo, tag)
    remote_names = {
        str(asset.get("name")) for asset in (remote or {}).get("assets", [])
    }
    conflicts = sorted(remote_names & {path.name for path in upload_paths})
    if conflicts and not replace:
        raise DatasetError(
            "远程已存在同名资产："
            + ", ".join(conflicts)
            + "。确认替换后使用 --replace。"
        )

    _, specs = load_settings()
    published = {
        name
        for name in specs
        if manifest_name(name) in remote_names
    }
    published.add(spec.name)
    notes = release_notes(specs, published)
    notes_path = ASSET_DIR / "release_notes.md"
    atomic_write_bytes(notes_path, notes.encode("utf-8"))

    if remote is None:
        command = [
            executable,
            "release",
            "create",
            tag,
            *map(str, upload_paths),
            "--repo",
            repo,
            "--target",
            "main",
            "--title",
            title,
            "--notes-file",
            str(notes_path),
        ]
    else:
        command = [
            executable,
            "release",
            "upload",
            tag,
            *map(str, upload_paths),
            "--repo",
            repo,
        ]
        if conflicts:
            command.append("--clobber")
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise DatasetError("GitHub Release 上传失败")
    if remote is not None:
        result = subprocess.run(
            [
                executable,
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
            cwd=PROJECT_ROOT,
            check=False,
        )
        if result.returncode != 0:
            raise DatasetError("资产已上传，但 Release 说明更新失败")
    # 让返回值可由测试和调用方直接复用。
    return f"https://github.com/{repo}/releases/tag/{tag}"


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
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CMR_raw_dataset/dataset.py"},
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temp_path.open("wb") as output:
                total = int(response.headers.get("Content-Length") or expected_size or 0)
                downloaded = 0
                started = time.monotonic()
                while chunk := response.read(4 * 1024 * 1024):
                    output.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.monotonic() - started, 0.001)
                    percent = f"{downloaded / total * 100:5.1f}%" if total else "  ---%"
                    print(
                        f"\r  {percent}  {format_size(downloaded):>10}  "
                        f"{format_size(int(downloaded / elapsed))}/s",
                        end="",
                        flush=True,
                    )
            print()
            if expected_size and temp_path.stat().st_size != expected_size:
                raise DatasetError(f"下载大小不匹配：{asset_name_value}")
            os.replace(temp_path, target)
            return
        except Exception as exc:  # 网络异常需要保留现场并有限重试
            print()
            temp_path.unlink(missing_ok=True)
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
        if not quiet and deep and (index == 1 or index == total or index % 500 == 0):
            print(f"  [{index}/{total}] {path}")
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
                    if count == 1 or count == len(expected) or count % 500 == 0:
                        print(f"  [{count}/{len(expected)}] {relative}")
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


def confirm(message: str) -> bool:
    try:
        return input(f"{message}\n输入 1 确认，直接回车取消：").strip() == "1"
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return False


def choose_dataset(specs: dict[str, DatasetSpec]) -> DatasetSpec | None:
    values = list(specs.values())
    print("请选择数据集：")
    for index, spec in enumerate(values, start=1):
        status = "可发布" if spec.publish_enabled else "待许可复核"
        print(f"  {index}. {spec.name}（{status}）")
    print("直接回车取消")
    raw = input("请输入编号：").strip()
    if not raw:
        return None
    try:
        index = int(raw)
    except ValueError as exc:
        raise DatasetError("数据集编号格式无效") from exc
    if index < 1 or index > len(values):
        raise DatasetError("数据集编号超出范围")
    return values[index - 1]


def prompt_path(message: str, default: Path) -> Path | None:
    raw = input(f"{message}（默认 {default}，直接回车使用默认，输入 0 取消）：").strip()
    if raw == "0":
        return None
    return Path(raw) if raw else default


def interactive() -> None:
    settings, specs = load_settings()
    print("CMR 原始数据集管理")
    print("  1. 检查、打包并可选上传")
    print("  2. 从 GitHub 下载并安装")
    print("  3. 验证已安装数据集")
    print("  4. 查看数据集状态")
    print("直接回车取消")
    choice = input("请选择功能：").strip()
    if not choice:
        print("已取消。")
        return
    if choice == "4":
        print_dataset_list(specs)
        return
    if choice not in {"1", "2", "3"}:
        raise DatasetError("功能编号无效")
    spec = choose_dataset(specs)
    if spec is None:
        print("已取消。")
        return

    if choice == "1":
        source = prompt_path("请输入原始数据目录", DEFAULT_RAW_DIR / spec.name)
        if source is None:
            print("已取消。")
            return
        records = scan_source(source)
        print_source_report(spec.name, source, records)
        if not confirm("是否生成发布分卷、manifest 和 SHA-256？"):
            return
        force = bool(dataset_asset_paths(spec.name)) and confirm("本地同名资产可能存在，是否允许替换？")
        part_size = int(settings.get("part_size_mib", 1900)) * 1024 * 1024
        pack_dataset(spec, source, part_size, force=force)
        if not spec.publish_enabled:
            print("本地打包完成；该数据集尚未完成许可复核，本次不会上传。")
            return
        if confirm("是否上传到 GitHub Release？"):
            repo = str(settings["repository"])
            replace = False
            executable = require_gh()
            remote = gh_release_view(executable, repo, str(settings["release_tag"]))
            if remote is not None:
                remote_names = {str(asset.get("name")) for asset in remote.get("assets", [])}
                if remote_names & {path.name for path in dataset_asset_paths(spec.name)}:
                    replace = confirm("远程已有同名资产，是否替换？")
                    if not replace:
                        print("已取消上传。")
                        return
            print(upload_dataset(spec, settings, repo, replace=replace))
        return

    if choice == "2":
        output = prompt_path("请输入安装根目录", DEFAULT_RAW_DIR)
        if output is None:
            print("已取消。")
            return
        repo = str(settings["repository"])
        tag = str(settings["release_tag"])
        manifest, parts = prepare_remote_bundle(spec.name, repo, tag)
        force = (output / spec.name).exists() and confirm("目标目录已存在，是否保留备份后替换？")
        install_bundle(manifest, parts, output, force=force)
        return

    root = prompt_path("请输入数据集安装根目录", DEFAULT_RAW_DIR)
    if root is None:
        print("已取消。")
        return
    marker = root / ".cmr_dataset_manifests" / f"{spec.name}.json"
    manifest = load_manifest(marker, spec.name)
    deep = confirm("是否执行逐文件 SHA-256 深度校验？")
    count, size = verify_dataset_tree(manifest, root.resolve(), deep=deep)
    print(f"验证通过：{count} 个文件，{format_size(size)}")


def print_dataset_list(specs: dict[str, DatasetSpec]) -> None:
    print("数据集配置：")
    for spec in specs.values():
        local = "已打包" if dataset_asset_paths(spec.name) else "未打包"
        publish = "允许上传" if spec.publish_enabled else "待许可复核"
        print(f"  {spec.name:<18} {local:<6} {publish}  {spec.description}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="查看数据集配置和本地打包状态")

    inspect_parser = subparsers.add_parser("inspect", help="只读检查原始数据目录")
    inspect_parser.add_argument("dataset")
    inspect_parser.add_argument("--source", type=Path, required=True)

    pack_parser = subparsers.add_parser("pack", help="流式压缩、分卷并生成校验文件")
    pack_parser.add_argument("dataset")
    pack_parser.add_argument("--source", type=Path, required=True)
    pack_parser.add_argument("--force", action="store_true", help="替换本地同名发布资产")
    pack_parser.add_argument(
        "--part-size-mib",
        type=int,
        default=None,
        help="测试或特殊场景使用的分卷上限；正式发布默认 1900 MiB",
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
    if args.command == "list":
        print_dataset_list(specs)
        return 0
    if args.dataset not in specs:
        raise DatasetError(
            f"未知数据集：{args.dataset}。可选值：{', '.join(specs)}"
        )
    spec = specs[args.dataset]
    if args.command == "inspect":
        records = scan_source(args.source)
        print_source_report(spec.name, args.source, records)
    elif args.command == "pack":
        part_size_mib = (
            int(settings.get("part_size_mib", 1900))
            if args.part_size_mib is None
            else args.part_size_mib
        )
        if part_size_mib < 1 or part_size_mib > 1900:
            raise DatasetError("part-size-mib 必须在 1 到 1900 之间")
        pack_dataset(
            spec,
            args.source,
            part_size_mib * 1024 * 1024,
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
