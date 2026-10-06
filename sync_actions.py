"""Non-destructive download, upload, and synchronization of actions."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from import_actions import (
    load_local_actions,
    portable_action,
    read_json,
    write_action_record,
    write_integration_manifest,
)
from langdock_api import LangdockClient

INPUT_FIELD_KEYS = (
    "label",
    "type",
    "description",
    "placeholder",
    "required",
    "options",
    "allowMultiSelect",
    "jsonSchema",
)


class UserAborted(RuntimeError):
    """Raised when the user aborts an interactive operation."""


def remote_action_records(
    integration: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    actions = integration.get("actions")
    if not isinstance(actions, list):
        raise RuntimeError("The integration response contains no actions array.")
    action_by_id = {
        action["id"]: action
        for action in actions
        if isinstance(action, dict) and action.get("id")
    }
    records: dict[str, dict[str, Any]] = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        manifest, code = portable_action(action, action_by_id)
        records[manifest["slug"]] = {
            "manifest": manifest,
            "code": code,
            "action": action,
        }
    return records


def _canonical_field(field: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: field[key]
        for key in INPUT_FIELD_KEYS
        if key in field
    }
    if "contextActionSlug" in field:
        result["contextActionSlug"] = field["contextActionSlug"]
    elif "contextActionId" in field:
        result["contextActionId"] = field["contextActionId"]
    return result


def action_digest(manifest: dict[str, Any], code: str) -> str:
    canonical = {
        "name": manifest.get("name"),
        "description": manifest.get("description"),
        "requiresConfirmation": manifest.get("requiresConfirmation", True),
        "inputFields": [
            _canonical_field(field)
            for field in manifest.get("inputFields", [])
            if isinstance(field, dict)
        ],
        "code": code,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compare_actions(
    local_actions: dict[str, dict[str, Any]],
    remote_actions: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    local_only = sorted(set(local_actions) - set(remote_actions))
    remote_only = sorted(set(remote_actions) - set(local_actions))
    mismatched = sorted(
        slug
        for slug in set(local_actions) & set(remote_actions)
        if action_digest(
            local_actions[slug]["manifest"],
            local_actions[slug]["code"],
        )
        != action_digest(
            remote_actions[slug]["manifest"],
            remote_actions[slug]["code"],
        )
    )
    return local_only, remote_only, mismatched


def _print_mismatch_summary(
    integration: dict[str, Any],
    mismatched: list[str],
    direction: str,
) -> None:
    print(
        f"Found {len(mismatched)} mismatching action(s) in "
        f"{integration.get('name') or integration.get('id')}"
    )
    for slug in mismatched:
        if direction == "download":
            print(f"  {slug}: local differs from remote; remote can replace local")
        elif direction == "upload":
            print(f"  {slug}: remote differs from local; local can replace remote")
        else:
            print(f"  {slug}: local and remote differ")


def _policy_decisions(
    integration: dict[str, Any],
    mismatched: list[str],
    direction: str,
    policy: str,
) -> dict[str, str]:
    if not mismatched:
        return {}
    _print_mismatch_summary(integration, mismatched, direction)

    if policy == "fail":
        raise RuntimeError("Mismatches found and --on-mismatch=fail was selected.")
    if policy == "skip":
        return {slug: "skip" for slug in mismatched}
    if policy == "local":
        return {
            slug: "local" if direction in ("upload", "sync") else "skip"
            for slug in mismatched
        }
    if policy == "remote":
        return {
            slug: "remote" if direction in ("download", "sync") else "skip"
            for slug in mismatched
        }
    if policy != "ask":
        raise RuntimeError(f"Unsupported mismatch policy: {policy}")
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Mismatches require a terminal. Use --on-mismatch=fail, "
            "--on-mismatch=skip, --on-mismatch=local, or "
            "--on-mismatch=remote."
        )

    decisions: dict[str, str] = {}
    for slug in mismatched:
        while True:
            if direction == "download":
                prompt = f"{slug}: [d]ownload remote, [s]kip, [a]bort: "
                allowed = {"d": "remote", "s": "skip", "a": "abort"}
            elif direction == "upload":
                prompt = f"{slug}: [u]pload local, [s]kip, [a]bort: "
                allowed = {"u": "local", "s": "skip", "a": "abort"}
            else:
                prompt = (
                    f"{slug}: [u]pload local, [d]ownload remote, "
                    "[s]kip, [a]bort: "
                )
                allowed = {
                    "u": "local",
                    "d": "remote",
                    "s": "skip",
                    "a": "abort",
                }
            answer = input(prompt).strip().lower()
            choice = allowed.get(answer)
            if choice == "abort":
                raise UserAborted("Operation aborted by user.")
            if choice:
                decisions[slug] = choice
                break
            print("Please choose one of the displayed options.")
    return decisions


def _remote_by_id(remote_actions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        record["action"]["id"]: record["action"]
        for record in remote_actions.values()
        if record.get("action", {}).get("id")
    }


def _local_dependencies(record: dict[str, Any], aliases: dict[str, str]) -> set[str]:
    dependencies: set[str] = set()
    for field in record["manifest"].get("inputFields", []):
        if not isinstance(field, dict):
            continue
        slug = field.get("contextActionSlug")
        if isinstance(slug, str) and slug:
            dependencies.add(aliases.get(slug, slug))
    return dependencies


def _local_payload(
    record: dict[str, Any],
    remote_actions: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, Any]:
    manifest = record["manifest"]
    name = manifest.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("A local action has no usable name.")

    payload: dict[str, Any] = {
        "name": name,
        "code": record["code"],
        "inputFields": [],
        "requiresConfirmation": manifest.get("requiresConfirmation", True),
    }
    if "description" in manifest:
        payload["description"] = manifest.get("description")

    for field in manifest.get("inputFields", []):
        if not isinstance(field, dict):
            continue
        output = {
            key: field[key]
            for key in INPUT_FIELD_KEYS
            if key in field
        }
        context_slug = field.get("contextActionSlug")
        if isinstance(context_slug, str) and context_slug:
            resolved_slug = aliases.get(context_slug, context_slug)
            target = remote_actions.get(resolved_slug)
            if not target or not target.get("action", {}).get("id"):
                raise RuntimeError(
                    f"Context action {context_slug!r} is not available remotely."
                )
            output["contextActionId"] = target["action"]["id"]
        elif field.get("contextActionId"):
            output["contextActionId"] = field["contextActionId"]
        output.pop("contextActionSlug", None)
        payload["inputFields"].append(output)

    return payload


def _register_remote_action(
    remote_actions: dict[str, dict[str, Any]],
    action: dict[str, Any],
) -> dict[str, Any]:
    action_by_id = _remote_by_id(remote_actions)
    if action.get("id"):
        action_by_id[action["id"]] = action
    manifest, code = portable_action(action, action_by_id)
    record = {"manifest": manifest, "code": code, "action": action}
    remote_actions[manifest["slug"]] = record
    return record


def _write_remote_record(
    output_dir: Path,
    local_actions: dict[str, dict[str, Any]],
    slug: str,
    remote_record: dict[str, Any],
    existing_path: Path | None = None,
) -> str:
    manifest = remote_record["manifest"]
    path = existing_path
    if path is None and slug in local_actions:
        path = local_actions[slug].get("path")
    path = write_action_record(
        output_dir,
        manifest,
        remote_record["code"],
        path,
    )
    return path.as_posix()


def _create_local_only(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    local_actions: dict[str, dict[str, Any]],
    remote_actions: dict[str, dict[str, Any]],
) -> dict[str, str]:
    pending = {
        slug: record
        for slug, record in local_actions.items()
        if slug not in remote_actions
    }
    aliases: dict[str, str] = {}

    while pending:
        created = False
        for local_slug, record in list(pending.items()):
            dependencies = _local_dependencies(record, aliases)
            if any(dependency not in remote_actions for dependency in dependencies):
                continue
            payload = _local_payload(record, remote_actions, aliases)
            action = client.create_action(integration_id, payload)
            if not action.get("slug") or not action.get("id"):
                raise RuntimeError("Created action response has no slug or ID.")
            remote_record = _register_remote_action(remote_actions, action)
            new_slug = remote_record["manifest"]["slug"]
            old_path = record.get("path")
            new_path = write_action_record(
                output_dir,
                remote_record["manifest"],
                remote_record["code"],
                old_path,
            )
            new_record = {
                "manifest": remote_record["manifest"],
                "code": remote_record["code"],
                "path": new_path,
                "action_id": action["id"],
            }
            local_actions.pop(local_slug, None)
            local_actions[new_slug] = new_record
            if local_slug != new_slug:
                aliases[local_slug] = new_slug
            pending.pop(local_slug)
            created = True
            print(f"Created remote action: {new_slug}")

        if not created:
            unresolved = ", ".join(sorted(pending))
            raise RuntimeError(
                "Could not create local actions because their context-action "
                f"dependencies cannot be resolved: {unresolved}"
            )

    return aliases


def _upload_selected(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    local_actions: dict[str, dict[str, Any]],
    remote_actions: dict[str, dict[str, Any]],
    slugs: list[str],
    aliases: dict[str, str] | None = None,
) -> None:
    if aliases is None:
        aliases = {}
    for slug in slugs:
        local_record = local_actions[slug]
        remote_record = remote_actions.get(slug)
        payload = _local_payload(local_record, remote_actions, aliases)
        if remote_record:
            action_id = remote_record.get("action", {}).get("id")
            if not action_id:
                raise RuntimeError(f"Remote action {slug} has no ID.")
            action = client.update_action(integration_id, action_id, payload)
            print(f"Updated remote action: {slug}")
        else:
            action = client.create_action(integration_id, payload)
            print(f"Created remote action: {slug}")
        if not action.get("slug") or not action.get("id"):
            raise RuntimeError("Remote action response has no slug or ID.")
        old_path = local_record.get("path")
        new_record = _register_remote_action(remote_actions, action)
        new_slug = new_record["manifest"]["slug"]
        if new_slug != slug:
            remote_actions.pop(slug, None)
        new_path = write_action_record(
            output_dir,
            new_record["manifest"],
            new_record["code"],
            old_path,
        )
        local_actions.pop(slug, None)
        local_actions[new_record["manifest"]["slug"]] = {
            "manifest": new_record["manifest"],
            "code": new_record["code"],
            "path": new_path,
            "action_id": action["id"],
        }


def _download_selected(
    output_dir: Path,
    local_actions: dict[str, dict[str, Any]],
    remote_actions: dict[str, dict[str, Any]],
    slugs: list[str],
) -> None:
    for slug in slugs:
        remote_record = remote_actions[slug]
        existing = local_actions.get(slug)
        path = existing.get("path") if existing else None
        new_path = write_action_record(
            output_dir,
            remote_record["manifest"],
            remote_record["code"],
            path,
        )
        local_actions[slug] = {
            "manifest": remote_record["manifest"],
            "code": remote_record["code"],
            "path": new_path,
            "action_id": remote_record["action"].get("id"),
        }
        print(f"Downloaded action: {slug}")


def download_integration(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    on_mismatch: str,
) -> None:
    integration = client.get_integration(integration_id)
    remote_actions = remote_action_records(integration)
    local_actions, _ = load_local_actions(output_dir)
    _, _, mismatched = compare_actions(local_actions, remote_actions)
    decisions = _policy_decisions(integration, mismatched, "download", on_mismatch)

    _download_selected(
        output_dir,
        local_actions,
        remote_actions,
        sorted(set(remote_actions) - set(local_actions)),
    )
    _download_selected(
        output_dir,
        local_actions,
        remote_actions,
        [slug for slug, choice in decisions.items() if choice == "remote"],
    )
    write_integration_manifest(
        output_dir,
        integration,
        local_actions,
        remote_actions,
        include_remote_only=True,
    )
    print(f"Download complete: {output_dir}")


def upload_integration(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    on_mismatch: str,
) -> None:
    integration = client.get_integration(integration_id)
    remote_actions = remote_action_records(integration)
    local_actions, _ = load_local_actions(output_dir)
    _, _, mismatched = compare_actions(local_actions, remote_actions)
    decisions = _policy_decisions(integration, mismatched, "upload", on_mismatch)

    aliases = _create_local_only(
        client,
        integration_id,
        output_dir,
        local_actions,
        remote_actions,
    )
    _upload_selected(
        client,
        integration_id,
        output_dir,
        local_actions,
        remote_actions,
        [slug for slug, choice in decisions.items() if choice == "local"],
        aliases,
    )
    write_integration_manifest(
        output_dir,
        integration,
        local_actions,
        remote_actions,
        include_remote_only=False,
    )
    print(f"Upload complete: {output_dir}")


def sync_integration(
    client: LangdockClient,
    integration_id: str,
    output_dir: Path,
    on_mismatch: str,
) -> None:
    integration = client.get_integration(integration_id)
    remote_actions = remote_action_records(integration)
    local_actions, _ = load_local_actions(output_dir)
    local_only, remote_only, mismatched = compare_actions(
        local_actions,
        remote_actions,
    )
    decisions = _policy_decisions(integration, mismatched, "sync", on_mismatch)

    aliases = _create_local_only(
        client,
        integration_id,
        output_dir,
        local_actions,
        remote_actions,
    )
    _download_selected(output_dir, local_actions, remote_actions, remote_only)
    _upload_selected(
        client,
        integration_id,
        output_dir,
        local_actions,
        remote_actions,
        [slug for slug, choice in decisions.items() if choice == "local"],
        aliases,
    )
    _download_selected(
        output_dir,
        local_actions,
        remote_actions,
        [slug for slug, choice in decisions.items() if choice == "remote"],
    )
    write_integration_manifest(
        output_dir,
        integration,
        local_actions,
        remote_actions,
        include_remote_only=True,
    )
    print(f"Sync complete: {output_dir}")
