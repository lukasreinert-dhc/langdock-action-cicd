"""Local action file helpers and remote-to-local import support."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from langdock_api import LangdockClient


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required local file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def portable_input_field(
    field: dict[str, Any],
    action_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = dict(field)
    context_action_id = result.pop("contextActionId", None)

    if context_action_id:
        target_action = action_by_id.get(context_action_id)
        if target_action and target_action.get("slug"):
            result["contextActionSlug"] = target_action["slug"]
        else:
            print(
                "WARNING: Could not resolve contextActionId "
                f"{context_action_id} for field {field.get('slug')}",
                file=sys.stderr,
            )
            result["contextActionId"] = context_action_id

    return result


def portable_action(
    action: dict[str, Any],
    action_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    slug = action.get("slug")
    if not slug:
        raise RuntimeError("An action returned by Langdock has no slug.")

    manifest: dict[str, Any] = {}
    manifest_keys = (
        "slug",
        "name",
        "description",
        "nativeActionType",
        "actionType",
        "order",
        "requiresConfirmation",
        "kind",
        "activeLabel",
        "doneLabel",
        "failedLabel",
    )

    for key in manifest_keys:
        if key in action:
            manifest[key] = action[key]

    manifest["inputFields"] = [
        portable_input_field(field, action_by_id)
        for field in action.get("inputFields", [])
    ]

    code = action.get("code") or ""
    return manifest, str(code)


def _summary_by_slug(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    summaries = manifest.get("actions", [])
    if not isinstance(summaries, list):
        return {}
    return {
        item["slug"]: item
        for item in summaries
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    }


def strip_action_ids(output_dir: Path) -> None:
    """Remove obsolete actionId fields from local manifest files."""
    manifest_paths: list[Path] = []
    integration_manifest_path = output_dir / "manifest.json"
    if integration_manifest_path.is_file():
        manifest_paths.append(integration_manifest_path)
    actions_dir = output_dir / "actions"
    if actions_dir.is_dir():
        manifest_paths.extend(actions_dir.rglob("action_manifest.json"))

    for manifest_path in manifest_paths:
        value = read_json(manifest_path)
        if not isinstance(value, dict):
            continue
        changed = False
        if "actionId" in value:
            value.pop("actionId", None)
            changed = True
        actions = value.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and "actionId" in action:
                    action.pop("actionId", None)
                    changed = True
        if changed:
            write_json(manifest_path, value)


def load_local_actions(
    output_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    strip_action_ids(output_dir)
    integration_manifest: dict[str, Any] = {}
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        value = read_json(manifest_path)
        if not isinstance(value, dict):
            raise RuntimeError(f"Local integration manifest is not an object: {manifest_path}")
        integration_manifest = value

    summaries = _summary_by_slug(integration_manifest)
    actions_dir = output_dir / "actions"
    records: dict[str, dict[str, Any]] = {}
    if not actions_dir.is_dir():
        return records, integration_manifest

    for action_folder in sorted(actions_dir.iterdir()):
        if not action_folder.is_dir():
            continue
        latest_dir = action_folder / "latest"
        manifest_path = latest_dir / "action_manifest.json"
        code_path = latest_dir / "code.js"
        if not manifest_path.is_file() and not code_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise RuntimeError(f"Local action manifest is not an object: {manifest_path}")
        slug = manifest.get("slug") or action_folder.name
        if not isinstance(slug, str) or not slug:
            raise RuntimeError(f"Local action has no usable slug: {manifest_path}")
        manifest["slug"] = slug
        try:
            code = code_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(f"Required local file not found: {code_path}") from exc
        records[slug] = {
            "manifest": manifest,
            "code": code,
            "path": latest_dir,
            "action_id": summaries.get(slug, {}).get("actionId"),
        }

    return records, integration_manifest


def write_action_record(
    output_dir: Path,
    manifest: dict[str, Any],
    code: str,
    action_path: Path | None = None,
) -> Path:
    slug = manifest.get("slug")
    if not isinstance(slug, str) or not slug:
        raise RuntimeError("Cannot write a local action without a slug.")
    manifest = dict(manifest)
    manifest.pop("actionId", None)
    target_dir = action_path or output_dir / "actions" / slug / "latest"
    target_dir.mkdir(parents=True, exist_ok=True)
    write_json(target_dir / "action_manifest.json", manifest)
    (target_dir / "code.js").write_text(code, encoding="utf-8")
    return target_dir


def write_integration_manifest(
    output_dir: Path,
    integration: dict[str, Any],
    local_actions: dict[str, dict[str, Any]],
    remote_actions: dict[str, dict[str, Any]],
    include_remote_only: bool,
) -> None:
    slugs = set(local_actions)
    if include_remote_only:
        slugs.update(remote_actions)

    def sort_key(slug: str) -> tuple[int, str]:
        remote_manifest = remote_actions.get(slug, {}).get("manifest", {})
        local_manifest = local_actions.get(slug, {}).get("manifest", {})
        order = remote_manifest.get("order", local_manifest.get("order", 999999))
        return (order if isinstance(order, int) else 999999, slug)

    summaries: list[dict[str, Any]] = []
    for slug in sorted(slugs, key=sort_key):
        remote = remote_actions.get(slug)
        local = local_actions.get(slug)
        source = remote or local
        if not source:
            continue
        summary: dict[str, Any] = {
            "slug": slug,
            "name": source["manifest"].get("name"),
        }
        summaries.append(summary)

    write_json(
        output_dir / "manifest.json",
        {
            "id": integration.get("id"),
            "name": integration.get("name"),
            "description": integration.get("description"),
            "authType": integration.get("authType"),
            "actions": summaries,
        },
    )


def import_actions(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    overwrite: bool,
) -> None:
    """Legacy direct importer that downloads all remote actions."""
    integration = client.get_integration(integration_id)
    actions = integration.get("actions")
    if not isinstance(actions, list):
        raise RuntimeError("The integration response contains no actions array.")

    output_dir.mkdir(parents=True, exist_ok=True)
    action_by_id = {
        action["id"]: action
        for action in actions
        if isinstance(action, dict) and action.get("id")
    }
    for action in sorted(
        (item for item in actions if isinstance(item, dict)),
        key=lambda item: (item.get("order", 999999), item.get("slug", "")),
    ):
        manifest, code = portable_action(action, action_by_id)
        action_path = None
        if not overwrite:
            candidate = output_dir / "actions" / manifest["slug"] / "latest"
            if candidate.exists():
                continue
        write_action_record(output_dir, manifest, code, action_path)

    local_actions, _ = load_local_actions(output_dir)
    remote_actions = {
        portable_action(action, action_by_id)[0]["slug"]: {
            "manifest": portable_action(action, action_by_id)[0],
            "code": str(action.get("code") or ""),
            "action": action,
        }
        for action in actions
        if isinstance(action, dict) and action.get("slug")
    }
    write_integration_manifest(
        output_dir,
        integration,
        local_actions,
        remote_actions,
        include_remote_only=True,
    )
    print(f"Downloaded {len(remote_actions)} actions into {output_dir}")
