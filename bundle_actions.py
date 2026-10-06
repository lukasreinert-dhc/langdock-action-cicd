"""Create and import single-file ZIP bundles for AI-assisted action editing."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from import_actions import read_json, strip_action_ids, write_json

BUNDLE_FORMAT = "langdock-actions-bundle"
BUNDLE_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_path(path: str) -> PurePosixPath:
    if not path or path.startswith("/"):
        raise RuntimeError(f"Unsafe archive path: {path!r}")
    parsed = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in parsed.parts):
        raise RuntimeError(f"Unsafe archive path: {path!r}")
    return parsed


def _safe_directory_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Bundle integration directory must be a non-empty string.")
    parsed = _safe_archive_path(value)
    if len(parsed.parts) != 1:
        raise RuntimeError(f"Bundle integration directory is not a single name: {value!r}")
    return parsed.name


def _integration_metadata(
    source_dir: Path,
    directory: str,
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    strip_action_ids(source_dir)
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Integration manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Integration manifest is not an object: {manifest_path}")

    files: list[tuple[Path, str]] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise RuntimeError(f"Symlinks are not supported in bundles: {path}")
        relative = path.relative_to(source_dir).as_posix()
        archive_path = f"integrations/{directory}/{relative}"
        _safe_archive_path(archive_path)
        files.append((path, archive_path))

    if not files:
        raise RuntimeError(f"Integration directory contains no files: {source_dir}")

    file_metadata = [
        {
            "path": archive_path,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path, archive_path in files
    ]
    metadata = {
        "directory": directory,
        "id": manifest.get("id"),
        "name": manifest.get("name") or directory,
        "files": file_metadata,
    }
    return metadata, files


def export_bundle(
    source_root: Path,
    bundle_path: Path,
    targets: list[tuple[str, Path]],
    complete: bool,
) -> None:
    if not targets:
        raise RuntimeError("No integrations selected for bundle export.")

    integration_metadata: list[dict[str, Any]] = []
    files_to_write: list[tuple[Path, str]] = []
    directories: set[str] = set()
    for name, source_dir in targets:
        directory = source_dir.name
        if directory in directories:
            raise RuntimeError(f"Duplicate bundle integration directory: {directory}")
        directories.add(directory)
        metadata, files = _integration_metadata(source_dir, directory)
        metadata["name"] = name or metadata["name"]
        integration_metadata.append(metadata)
        files_to_write.extend(files)

    bundle_manifest = {
        "format": BUNDLE_FORMAT,
        "formatVersion": BUNDLE_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "integrations": integration_metadata,
    }

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        bundle_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "bundle.json",
            json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for source_path, archive_path in files_to_write:
            archive.write(source_path, archive_path)

    print(
        f"Bundle export complete: {bundle_path} with "
        f"{len(integration_metadata)} integration(s) and "
        f"{len(files_to_write)} file(s)"
    )


def _read_and_validate_bundle(
    bundle_path: Path,
) -> tuple[dict[str, Any], dict[str, list[tuple[str, bytes]]]]:
    if not bundle_path.is_file():
        raise RuntimeError(f"Bundle file not found: {bundle_path}")

    try:
        archive = zipfile.ZipFile(bundle_path, mode="r")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Invalid ZIP bundle: {bundle_path}") from exc

    with archive:
        names = archive.namelist()
        file_names = {name for name in names if not name.endswith("/")}
        if "bundle.json" not in file_names:
            raise RuntimeError("Bundle does not contain bundle.json.")
        if len(names) != len(set(names)):
            raise RuntimeError("Bundle contains duplicate paths.")

        for name in names:
            _safe_archive_path(name)

        try:
            manifest = json.loads(archive.read("bundle.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("bundle.json is not valid UTF-8 JSON.") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("bundle.json must contain a JSON object.")
        if manifest.get("format") != BUNDLE_FORMAT:
            raise RuntimeError("Unsupported bundle format.")
        if manifest.get("formatVersion") != BUNDLE_VERSION:
            raise RuntimeError(
                f"Unsupported bundle format version: {manifest.get('formatVersion')!r}"
            )

        integrations = manifest.get("integrations")
        if not isinstance(integrations, list) or not integrations:
            raise RuntimeError("Bundle contains no integrations.")

        directories: set[str] = set()
        declared_by_path: dict[str, dict[str, Any]] = {}
        for integration in integrations:
            if not isinstance(integration, dict):
                raise RuntimeError("Bundle integration metadata must be an object.")
            directory = _safe_directory_name(integration.get("directory"))
            if directory in directories:
                raise RuntimeError(f"Duplicate integration in bundle: {directory}")
            directories.add(directory)
            files = integration.get("files", [])
            if not isinstance(files, list):
                print(
                    f"WARNING: Invalid file list in bundle metadata for {directory}; "
                    "using ZIP folder contents.",
                )
                continue
            for file_metadata in files:
                if not isinstance(file_metadata, dict):
                    print(
                        f"WARNING: Invalid file metadata in bundle for {directory}; "
                        "using ZIP folder contents.",
                    )
                    continue
                path = file_metadata.get("path")
                if not isinstance(path, str):
                    print(
                        f"WARNING: Invalid bundle file path in {directory}; "
                        "using ZIP folder contents.",
                    )
                    continue
                expected_prefix = f"integrations/{directory}/"
                if not path.startswith(expected_prefix):
                    raise RuntimeError(f"Bundle file is outside its integration: {path}")
                _safe_archive_path(path)
                declared_by_path[path] = file_metadata

        prefixes = {
            directory: f"integrations/{directory}/"
            for directory in directories
        }
        extracted: dict[str, list[tuple[str, bytes]]] = {
            directory: [] for directory in directories
        }
        actual_paths: set[str] = set()
        for path in sorted(file_names - {"bundle.json"}):
            matching = [
                directory
                for directory, prefix in prefixes.items()
                if path.startswith(prefix)
            ]
            if len(matching) != 1:
                raise RuntimeError(f"Bundle file is outside a declared integration: {path}")
            directory = matching[0]
            actual_paths.add(path)
            content = archive.read(path)
            file_metadata = declared_by_path.get(path)
            if file_metadata is None:
                print(
                    f"WARNING: Bundle file is not declared in bundle.json: {path}; "
                    "accepting it because it is inside a declared integration folder."
                )
            else:
                expected_hash = file_metadata.get("sha256")
                actual_hash = hashlib.sha256(content).hexdigest()
                if expected_hash and expected_hash != actual_hash:
                    print(
                        f"WARNING: Checksum changed for bundle file: {path}; "
                        "accepting the AI-modified content."
                    )
                expected_size = file_metadata.get("size")
                if isinstance(expected_size, int) and expected_size != len(content):
                    print(
                        f"WARNING: Size changed for bundle file: {path}; "
                        "accepting the AI-modified content."
                    )
            extracted[directory].append((path, content))

        for path in sorted(set(declared_by_path) - actual_paths):
            print(
                f"WARNING: Declared bundle file is missing from ZIP: {path}; "
                "treating it as deleted by the AI."
            )

        for directory, files in extracted.items():
            if not files:
                raise RuntimeError(f"Bundle integration contains no files: {directory}")
        return manifest, extracted


def bundle_integration_options(bundle_path: Path) -> list[dict[str, str]]:
    if not bundle_path.is_file():
        raise RuntimeError(f"Bundle file not found: {bundle_path}")
    try:
        with zipfile.ZipFile(bundle_path, mode="r") as archive:
            manifest = json.loads(archive.read("bundle.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid bundle metadata: {bundle_path}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("bundle.json must contain a JSON object.")
    if manifest.get("format") != BUNDLE_FORMAT:
        raise RuntimeError("Unsupported bundle format.")
    if manifest.get("formatVersion") != BUNDLE_VERSION:
        raise RuntimeError(
            f"Unsupported bundle format version: {manifest.get('formatVersion')!r}"
        )
    integrations = manifest.get("integrations")
    if not isinstance(integrations, list) or not integrations:
        raise RuntimeError("Bundle contains no integrations.")

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for integration in integrations:
        if not isinstance(integration, dict):
            raise RuntimeError("Bundle integration metadata must be an object.")
        directory = _safe_directory_name(integration.get("directory"))
        if directory in seen:
            raise RuntimeError(f"Duplicate integration in bundle: {directory}")
        seen.add(directory)
        options.append({
            "name": str(integration.get("name") or directory),
            "directory": directory,
        })
    return options


def bundle_integration_names(bundle_path: Path) -> list[str]:
    return [
        option["name"]
        for option in bundle_integration_options(bundle_path)
    ]


def import_bundle(
    bundle_path: Path,
    destination_root: Path,
    selected_names: list[str] | None,
) -> None:
    manifest, extracted = _read_and_validate_bundle(bundle_path)
    metadata_by_directory = {
        _safe_directory_name(item["directory"]): item
        for item in manifest["integrations"]
    }

    selected_directories = set(extracted)
    if selected_names:
        wanted = {name.casefold() for name in selected_names}
        selected_directories = {
            directory
            for directory, metadata in metadata_by_directory.items()
            if str(metadata.get("name", directory)).casefold() in wanted
            or directory.casefold() in wanted
        }
        missing = [
            name
            for name in selected_names
            if not any(
                str(metadata.get("name", directory)).casefold() == name.casefold()
                or directory.casefold() == name.casefold()
                for directory, metadata in metadata_by_directory.items()
            )
        ]
        if missing:
            raise RuntimeError(
                "Selected integration(s) are not present in bundle: "
                + ", ".join(missing)
            )

    if not selected_directories:
        raise RuntimeError("No integrations selected from bundle.")

    with tempfile.TemporaryDirectory(prefix="langdock-bundle-") as temporary:
        staging_root = Path(temporary) / "integrations"
        staging_root.mkdir(parents=True)
        for directory in selected_directories:
            staged_dir = staging_root / directory
            for archive_path, content in extracted[directory]:
                relative = PurePosixPath(archive_path).relative_to(
                    PurePosixPath(f"integrations/{directory}")
                )
                target = staged_dir.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            strip_action_ids(staged_dir)

        destination_root.mkdir(parents=True, exist_ok=True)
        if selected_names is None:
            for child in destination_root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
        else:
            for directory in selected_directories:
                target = destination_root / directory
                if target.exists() and not target.is_symlink():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()

        for directory in selected_directories:
            staged_dir = staging_root / directory
            target = destination_root / directory
            shutil.copytree(staged_dir, target)

    print(
        f"Bundle import complete: {bundle_path} with "
        f"{len(selected_directories)} integration(s) into {destination_root}"
    )
