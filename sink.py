"""Rendering the request body and POSTing a material order to starship.

Nothing here ever writes without an explicit keypress.
"""

from __future__ import annotations

import json
import shlex
from typing import Optional

import httpx

from . import config


def render_body(dto) -> str:
    return json.dumps(dto.request_body(), indent=2, ensure_ascii=False)


def curl_for(settings: config.Settings, dto) -> str:
    url = settings.order_url
    body = json.dumps(dto.request_body(), ensure_ascii=False)
    return (
        f"curl -X POST {shlex.quote(url)} \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H {shlex.quote('X-USER-ID: ' + settings.user_id)} \\\n"
        f"  -d {shlex.quote(body)}"
    )


class StarshipClient:
    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "Content-Type": "application/json",
                "X-USER-ID": settings.user_id,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create(self, dto) -> tuple[Optional[int], str]:
        """POST the order. Returns (status_code, rendered body or error)."""
        url = self._settings.order_url
        try:
            response = await self._http.post(url, json=dto.request_body())
        except httpx.HTTPError as exc:
            return None, (
                f"request to {url} failed: {exc}\n"
                f"  is starship running? (go build -o starship . && ./starship)"
            )
        try:
            rendered = json.dumps(response.json(), indent=2, ensure_ascii=False)
        except ValueError:
            rendered = response.text
        return response.status_code, rendered


__all__ = [
    "StarshipClient",
    "render_body",
    "curl_for",
]
