"""Local configuration, secrets, and integration-map loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{description} file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {description} file: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"The {description} file must contain a JSON object.")

    return data


def load_secrets(path: Path) -> dict[str, Any]:
    data = load_json_object(path, "Secrets")

    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError("The secrets file must contain a non-empty api_key.")

    return data


def load_integrations(path: Path) -> dict[str, str]:
    data = load_json_object(path, "Integrations map")
    integrations: dict[str, str] = {}

    for name, integration_id in data.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("Integration-map names must be non-empty strings.")
        if not isinstance(integration_id, str) or not integration_id.strip():
            raise RuntimeError(
                f"Integration ID for {name!r} must be a non-empty string."
            )
        integrations[name] = integration_id.strip()

    if not integrations:
        raise RuntimeError(f"The integrations map is empty: {path}")

    return integrations
