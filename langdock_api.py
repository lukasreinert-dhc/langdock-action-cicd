"""Small HTTP client for the Langdock Integrations API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class LangdockApiError(RuntimeError):
    """Raised when the Langdock API returns an error."""


def error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)
        if isinstance(error, str):
            return error
        return str(
            payload.get("message")
            or payload.get("errorMessage")
            or payload.get("errorCode")
            or payload
        )
    return str(payload or "Unknown API error")


class LangdockClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def get_integration(self, integration_id: str) -> dict[str, Any]:
        path = f"/integrations/v1/{quote(integration_id, safe='')}"
        payload = self._request_json("GET", path)
        integration = payload.get("integration", payload)
        if not isinstance(integration, dict):
            raise LangdockApiError(
                "The API response contains no integration object."
            )
        return integration

    def create_action(
        self,
        integration_id: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        path = (
            f"/integrations/v1/{quote(integration_id, safe='')}"
            "/actions/create"
        )
        payload = self._request_json("POST", path, action)
        result = payload.get("action", payload)
        if not isinstance(result, dict):
            raise LangdockApiError("The API response contains no action object.")
        return result

    def update_action(
        self,
        integration_id: str,
        action_id: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        path = (
            f"/integrations/v1/{quote(integration_id, safe='')}"
            f"/actions/{quote(action_id, safe='')}"
        )
        payload = self._request_json("PUT", path, action)
        result = payload.get("action", payload)
        if not isinstance(result, dict):
            raise LangdockApiError("The API response contains no action object.")
        return result

    def _request_json(
        self,
        method: str,
        path: str,
        body: Any | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        request = Request(
            url=self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )

        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else None
                if response.status < 200 or response.status >= 300:
                    raise LangdockApiError(
                        f"HTTP {response.status}: {error_message(payload)}"
                    )
                return payload
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw
            raise LangdockApiError(
                f"HTTP {exc.code}: {error_message(payload)}"
            ) from exc
        except URLError as exc:
            raise LangdockApiError(f"Request failed: {exc.reason}") from exc
