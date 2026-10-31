"""Small client for the everlights app's HTTP API (not the bridge itself).

The app does the scheduling, confirming and retrying; Home Assistant just asks it
to play a pattern or turn off, the same way the app's own buttons do. A change
made here holds until the app's next scheduled change.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class EverLightsError(Exception):
    """The app didn't answer, or answered with an error."""


class EverLightsAuthError(EverLightsError):
    """The app wants a token (the remote entrance) and ours is missing or wrong."""


class EverLightsApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None = None) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._token = token or None

    async def _request(self, method: str, path: str, json: Any = None, text: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with self._session.request(
                method, self.url + path, json=json, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status in (401, 403):
                    raise EverLightsAuthError(f"{resp.status} from {path}")
                body = await (resp.text() if text else resp.json(content_type=None))
                if resp.status >= 400:
                    msg = body if text else (body or {}).get("error", body)
                    raise EverLightsError(f"{resp.status} from {path}: {msg}")
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EverLightsError(f"{path}: {err}") from err

    async def state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/state")

    async def patterns(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/patterns")

    async def play(self, sequence_id: str) -> bool:
        return (await self._request("POST", "/api/play", {"sequenceId": sequence_id})).get("ok", False)

    async def play_colors(self, colors: list[str], label: str) -> bool:
        body = {"pattern": colors, "effects": [], "label": label}
        return (await self._request("POST", "/api/play", body)).get("ok", False)

    async def turn_on(self) -> str:
        """The app's own "on": tonight's scheduled pattern, else the last one played."""
        return await self._request("POST", "/go/on", text=True)

    async def turn_off(self) -> bool:
        return (await self._request("POST", "/api/off", {})).get("ok", False)
