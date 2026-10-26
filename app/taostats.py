"""Taostats API client (new v1 API, https://api.taostats.io/v1).

Used for (a) the connection graph, which needs transfers between arbitrary
addresses, and (b) importing past history of a newly registered wallet.
"""

import asyncio
import logging
import time
from datetime import datetime

import httpx

from . import ss58
from .config import Settings
from .models import KIND_STAKE_TRANSFER, KIND_TRANSFER

log = logging.getLogger("wallet_monitor.taostats")


class TaostatsError(Exception):
    pass


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _addr(value) -> str | None:
    if isinstance(value, dict):  # tolerate {"ss58": ..., "hex": ...}
        value = value.get("ss58") or value.get("hex")
    try:
        return ss58.normalize(value)
    except ValueError:
        return None


def _event_index(item_id: str | None, fallback: int) -> int:
    """Taostats ids look like "{block}-{event_idx}"; keep the event part when present."""
    try:
        return int(str(item_id).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return fallback


class TaostatsClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[tuple, tuple[float, list]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.taostats_api_key)

    async def _get_all(self, path: str, params: dict, max_items: int) -> list[dict]:
        if not self.enabled:
            raise TaostatsError("Taostats API key is not configured (WM_TAOSTATS_API_KEY).")
        key = (path, tuple(sorted(params.items())), max_items)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.settings.taostats_cache_seconds:
            return cached[1]

        items: list[dict] = []
        page = 1
        limit = min(200, max_items)
        headers = {"Authorization": self.settings.taostats_api_key, "Accept": "application/json"}
        async with self._lock, httpx.AsyncClient(base_url=self.settings.taostats_base_url, timeout=30) as client:
            while len(items) < max_items:
                for attempt in range(3):
                    resp = await client.get(path, params={**params, "page": page, "limit": limit}, headers=headers)
                    if resp.status_code != 429:
                        break
                    await asyncio.sleep(float(resp.headers.get("retry-after", 5 * (attempt + 1))))
                if resp.status_code != 200:
                    try:
                        message = resp.json().get("message", resp.text)
                    except ValueError:
                        message = resp.text
                    raise TaostatsError(f"Taostats {path} returned {resp.status_code}: {message}")
                body = resp.json()
                items.extend(body.get("data") or [])
                next_page = (body.get("pagination") or {}).get("next_page")
                if not next_page:
                    break
                page = next_page
        items = items[:max_items]
        self._cache[key] = (time.monotonic(), items)
        return items

    async def transfers(self, address: str, max_items: int = 200) -> list[dict]:
        data = await self._get_all("/v1/transfers", {"address": address}, max_items)
        rows = []
        for i, item in enumerate(data):
            frm, to = _addr(item.get("from")), _addr(item.get("to"))
            if not frm or not to:
                continue
            rows.append(
                {
                    "kind": KIND_TRANSFER,
                    "from_address": frm,
                    "to_address": to,
                    "amount_rao": int(item.get("amount") or 0),
                    "alpha_rao": None,
                    "hotkey": None,
                    "origin_netuid": None,
                    "destination_netuid": None,
                    "block_number": int(item.get("block_number") or 0),
                    "event_index": _event_index(item.get("id"), -1 - i),
                    "extrinsic_id": item.get("extrinsic_id"),
                    "extrinsic_hash": item.get("transaction_hash"),
                    "timestamp": _parse_ts(item.get("timestamp")),
                }
            )
        return rows

    async def stake_transfers(self, address: str, max_items: int = 200) -> list[dict]:
        """Each stake transfer is indexed as an `unstake` leg on the sender's coldkey and
        a `stake` leg on the receiver's; querying one coldkey yields both directions."""
        data = await self._get_all(
            "/v1/subnets/stake-events", {"coldkey": address, "is_transfer": "true"}, max_items
        )
        rows = []
        for i, item in enumerate(data):
            coldkey, other = _addr(item.get("coldkey")), _addr(item.get("transfer_address"))
            if not coldkey or not other:
                continue
            outgoing = str(item.get("action", "")).lower() == "unstake"
            netuid = item.get("netuid")
            alpha = item.get("alpha")
            rows.append(
                {
                    "kind": KIND_STAKE_TRANSFER,
                    "from_address": coldkey if outgoing else other,
                    "to_address": other if outgoing else coldkey,
                    "amount_rao": int(item.get("amount") or 0),
                    "alpha_rao": int(alpha) if alpha is not None else None,
                    "hotkey": _addr(item.get("hotkey")),
                    "origin_netuid": netuid if outgoing else None,
                    "destination_netuid": None if outgoing else netuid,
                    "block_number": int(item.get("block_number") or 0),
                    "event_index": _event_index(item.get("id"), -1 - i),
                    "extrinsic_id": item.get("extrinsic_id"),
                    "extrinsic_hash": None,
                    "timestamp": _parse_ts(item.get("timestamp")),
                }
            )
        return rows

    async def history(self, address: str, kinds: set[str], max_items: int = 200) -> list[dict]:
        rows: list[dict] = []
        if KIND_TRANSFER in kinds:
            rows += await self.transfers(address, max_items)
        if KIND_STAKE_TRANSFER in kinds:
            rows += await self.stake_transfers(address, max_items)
        return rows
