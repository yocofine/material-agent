"""安全的归档解压工具。

所有来自用户或外部系统的归档都必须经过这里，避免 Zip Slip、Tar traversal
以及通过归档成员创建链接逃逸出工作目录。
"""
from __future__ import annotations

import tarfile
import zipfile
from typing import BinaryIO
from pathlib import Path, PurePosixPath, PureWindowsPath


class ArchiveSizeLimitExceeded(ValueError):
    """Raised when extracted archive content exceeds the configured budget."""


class ArchiveFileCountExceeded(ValueError):
    """Raised when an archive contains too many extracted files."""


def _copy_limited(
    source: BinaryIO,
    output: BinaryIO,
    total_written: int,
    max_total_bytes: int | None,
) -> int:
    while chunk := source.read(1024 * 1024):
        total_written += len(chunk)
        if max_total_bytes is not None and total_written > max_total_bytes:
            raise ArchiveSizeLimitExceeded(
                f"压缩包解压后总大小超过 {max_total_bytes // (1024 * 1024)} MB"
            )
        output.write(chunk)
    return total_written


def repair_archive_name(name: str) -> str:
    """Repair common UTF-8-as-CP437 ZIP filename mojibake.

    Some Windows upload/ZIP paths are decoded with the wrong legacy code page,
    producing names such as ``╤▌╜▓╕σ`` instead of ``演讲稿``. Only accept the repair when it produces
    CJK text and the original contains obvious box/block mojibake markers.
    """
    def repair_component(component: str) -> str:
        if not component or not any(
            0x2500 <= ord(char) <= 0x259F or 0x0370 <= ord(char) <= 0x03FF
            for char in component
        ):
            return component
        stem, dot, extension = component.rpartition(".")
        if not dot:
            stem, extension = component, ""
        try:
            # This is the inverse of the Windows GBK/CP437 mojibake observed
            # in uploaded multipart and ZIP filenames.
            repaired_stem = stem.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return component
        if any("\u3400" <= char <= "\u9fff" for char in repaired_stem):
            return repaired_stem + (("." + extension) if dot else "")
        return component

    if not name:
        return name
    separator = "/" if "/" in name else "\\" if "\\" in name else ""
    if not separator:
        return repair_component(name)
    return separator.join(repair_component(part) for part in name.replace("\\", "/").split("/"))


def safe_member_path(root: Path, member_name: str) -> Path:
    """Resolve an archive member and ensure it stays below ``root``."""
    normalized = member_name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or ".." in parts
        or ":" in parts[0]
    ):
        raise ValueError(f"不安全的归档路径：{member_name!r}")
    target_root = root.resolve()
    target = (target_root / Path(*parts)).resolve()
    if target != target_root and target_root not in target.parents:
        raise ValueError(f"不安全的归档路径：{member_name!r}")
    return target


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def extract_zip_recursive(
    zip_path: Path,
    dest: Path,
    max_total_bytes: int | None = None,
    max_files: int | None = None,
) -> list[Path]:
    """Extract a ZIP and nested ZIPs into ``dest`` using safe member paths."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    queue = [(zip_path.resolve(), dest)]
    root_zip = zip_path.resolve()
    extracted: list[Path] = []
    total_written = 0
    file_count = 0
    while queue:
        current, current_dest = queue.pop(0)
        with zipfile.ZipFile(current) as zf:
            for info in zf.infolist():
                name = repair_archive_name(info.filename)
                parts = PurePosixPath(name.replace("\\", "/")).parts
                if info.is_dir() or "__MACOSX" in parts:
                    continue
                base = PurePosixPath(name.replace("\\", "/")).name
                if base == ".DS_Store" or base.startswith("._"):
                    continue
                if _is_zip_symlink(info):
                    raise ValueError(f"归档包含不支持的符号链接：{name}")
                target = safe_member_path(current_dest, name)
                is_nested_zip = target.suffix.lower() == ".zip"
                if not is_nested_zip:
                    file_count += 1
                    if max_files is not None and file_count > max_files:
                        raise ArchiveFileCountExceeded(
                            f"压缩包解压后文件数不能超过 {max_files} 个"
                        )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("wb") as output:
                    total_written = _copy_limited(
                        source, output, total_written, max_total_bytes
                    )
                extracted.append(target)
                if is_nested_zip:
                    nested_dest = target.with_suffix("")
                    nested_dest.mkdir(parents=True, exist_ok=True)
                    queue.append((target, nested_dest))
        if current != root_zip:
            current.unlink(missing_ok=True)
    return extracted


def extract_tar_safe(
    archive_path: Path,
    dest: Path,
    max_total_bytes: int | None = None,
    max_files: int | None = None,
) -> list[Path]:
    """Extract regular files from a TAR archive after validating every member."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_written = 0
    file_count = 0
    with tarfile.open(archive_path) as tf:
        for member in tf.getmembers():
            member.name = repair_archive_name(member.name)
            if member.isdir():
                safe_member_path(dest, member.name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"归档包含不支持的链接或特殊文件：{member.name}")
            file_count += 1
            if max_files is not None and file_count > max_files:
                raise ArchiveFileCountExceeded(
                    f"压缩包解压后文件数不能超过 {max_files} 个"
                )
            target = safe_member_path(dest, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise ValueError(f"无法读取归档成员：{member.name}")
            with source, target.open("wb") as output:
                total_written = _copy_limited(
                    source, output, total_written, max_total_bytes
                )
            extracted.append(target)
    return extracted


def extract_rar_safe(
    archive_path: Path,
    dest: Path,
    max_total_bytes: int | None = None,
    max_files: int | None = None,
) -> list[Path]:
    """Extract regular RAR files after validating member paths."""
    import rarfile  # type: ignore

    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_written = 0
    file_count = 0
    with rarfile.RarFile(archive_path) as rf:
        for info in rf.infolist():
            member_name = repair_archive_name(info.filename)
            if info.isdir():
                safe_member_path(dest, member_name).mkdir(parents=True, exist_ok=True)
                continue
            file_count += 1
            if max_files is not None and file_count > max_files:
                raise ArchiveFileCountExceeded(
                    f"压缩包解压后文件数不能超过 {max_files} 个"
                )
            target = safe_member_path(dest, member_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with rf.open(info) as source, target.open("wb") as output:
                total_written = _copy_limited(
                    source, output, total_written, max_total_bytes
                )
            extracted.append(target)
    return extracted


def safe_filename(name: str, fallback: str = "file.bin") -> str:
    """Keep only a basename for user-controlled uploaded filenames."""
    name = repair_archive_name(name)
    normalized = name.replace("\\", "/")
    basename = PurePosixPath(normalized).name
    if basename in {"", ".", ".."}:
        return fallback
    cleaned = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in basename)
    return cleaned.strip().strip(".") or fallback
