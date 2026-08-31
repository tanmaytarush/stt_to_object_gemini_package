"""Rendering the request body, probing for duplicates, and POSTing to starship.

Nothing here ever writes without an explicit keypress. The duplicate probe is
best-effort: the API has no uniqueness constraint on client or dealer names, so
a voice flow will happily create the same contact three times if nobody looks.
"""

from __future__ import annotations

import json
import shlex
from typing import Optional

import httpx

from . import config
from .schemas import ClientDto, DealerDto


def render_body(dto) -> str:
    return json.dumps(dto.request_body(), indent=2, ensure_ascii=False)


def parse_filter_rows(envelope) -> tuple[list[dict], Optional[str]]:
    """Pull the row list out of a Utils.ApiPaginatedResult envelope.

    Shape (Utils/ApiResponse.go PaginatedResultBody):
        {"result": {"response": {...}, "data": [...], "offset": 0,
                    "pageSize": 10, "totalPages": 1, "totalRecords": 2}}

    An empty page serializes `data` as null. That is a valid "no matches"
    answer and must not be reported as a parse failure.
    """
    if isinstance(envelope, dict):
        result = envelope.get("result")
        if isinstance(result, dict) and "data" in result:
            rows = result["data"]
            if rows is None:
                return [], None
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)], None
    return [], "duplicate check: unrecognised response envelope"


def curl_for(settings: config.Settings, dto) -> str:
    url = settings.client_url if isinstance(dto, ClientDto) else settings.dealer_url
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
                # Read by RecoveryMiddleware and used by the GORM audit callbacks
                # to populate created_by / updated_by.
                "X-USER-ID": settings.user_id,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def find_similar(self, dto) -> tuple[list[dict], Optional[str]]:
        """Best-effort lookup of existing records with a similar name.

        Both repositories filter with `LIKE '%name%'` (ClientRepository.go:46,
        DealerRepository.go:49), so this returns substring matches, not just
        exact ones — "Ramesh" surfaces an existing "Ramesh Kumar", which is
        exactly what you want before creating a near-duplicate contact.

        Returns (matches, error). A failed probe is never fatal — it downgrades
        to "could not check" rather than blocking the write.
        """
        if isinstance(dto, ClientDto):
            url = f"{self._settings.client_url}/filter"
            payload = {
                "contractorId": dto.contractorId,
                "clientName": (dto.clientName or "").strip(),
                "offset": 0,
                "pageSize": 10,
            }
        else:
            url = f"{self._settings.dealer_url}/filter"
            # DealerFilterRequestDto's offset/pageSize are non-pointer ints, so
            # they must be sent explicitly; pageSize 0 would be clamped to 20.
            payload = {
                "contractorId": dto.contractorId,
                "dealerName": (dto.dealerName or "").strip(),
                "offset": 0,
                "pageSize": 10,
            }

        try:
            response = await self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            return [], f"could not check for duplicates: {exc}"

        if response.status_code != 200:
            return [], f"duplicate check returned {response.status_code}"

        try:
            envelope = response.json()
        except ValueError:
            return [], "duplicate check returned non-JSON"

        return parse_filter_rows(envelope)

    async def create(self, dto) -> tuple[Optional[int], str]:
        """POST the record. Returns (status_code, rendered body or error)."""
        url = self._settings.client_url if isinstance(dto, ClientDto) else self._settings.dealer_url
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


def describe_match(match: dict, wanted_name: str = "") -> str:
    """One line summarizing an existing record found by the similarity probe.

    Field names follow ResponseDtos.ClientResponseDto / DealerResponseDto.
    """
    name = match.get("clientName") or match.get("dealerName") or "?"
    bits = [f"id={match.get('id')}", str(name), str(match.get("phoneNumber") or "—")]
    if match.get("projectType"):
        bits.append(str(match["projectType"]))
    if match.get("isActive") is False:
        bits.append("inactive")
    if wanted_name and str(name).strip().casefold() == wanted_name.strip().casefold():
        bits.append("← exact match")
    return "  ".join(bits)


__all__ = [
    "StarshipClient",
    "render_body",
    "parse_filter_rows",
    "curl_for",
    "describe_match",
    "ClientDto",
    "DealerDto",
]
