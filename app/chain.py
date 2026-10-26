"""Background tasks talking to the Bittensor (subtensor) chain.

* ChainWatcher scans every finalized block for Balances.Transfer and
  SubtensorModule.StakeTransferred events and stores those that touch a
  registered wallet.
* BalanceRefresher periodically reads free balance and stake positions
  (valued in TAO at the current subnet alpha price) for every wallet.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from async_substrate_interface import AsyncSubstrateInterface
from sqlalchemy import select

from . import ss58
from .config import Settings
from .db import SessionLocal
from .models import KIND_STAKE_TRANSFER, KIND_TRANSFER, RAO_PER_TAO, Wallet, utcnow
from .services import kv_get, kv_set, store_transfers, tracked_addresses

log = logging.getLogger("wallet_monitor.chain")

LAST_BLOCK_KEY = "watcher.last_block"


def _value(obj):
    return getattr(obj, "value", obj)


def _event_parts(ev: dict) -> tuple[str, str, object]:
    inner = ev.get("event") or {}
    module = ev.get("module_id") or inner.get("module_id")
    name = ev.get("event_id") or inner.get("event_id")
    attrs = ev.get("attributes", inner.get("attributes"))
    return module, name, attrs


def parse_event(ev: dict) -> dict | None:
    """Return a partial Transfer row for the events we care about, else None."""
    module, name, attrs = _event_parts(ev)
    if module == "Balances" and name == "Transfer":
        if isinstance(attrs, dict):
            frm, to, amount = attrs.get("from"), attrs.get("to"), attrs.get("amount")
        else:
            frm, to, amount = attrs[0], attrs[1], attrs[2]
        return {
            "kind": KIND_TRANSFER,
            "from_address": ss58.normalize(frm),
            "to_address": ss58.normalize(to),
            "amount_rao": int(amount),
        }
    if module == "SubtensorModule" and name == "StakeTransferred":
        # (origin_coldkey, destination_coldkey, hotkey, origin_netuid, destination_netuid, tao_amount)
        if isinstance(attrs, dict):
            attrs = list(attrs.values())
        frm, to, hotkey, o_net, d_net, amount = attrs[:6]
        return {
            "kind": KIND_STAKE_TRANSFER,
            "from_address": ss58.normalize(frm),
            "to_address": ss58.normalize(to),
            "hotkey": ss58.normalize(hotkey),
            "origin_netuid": int(o_net),
            "destination_netuid": int(d_net),
            "amount_rao": int(amount),
        }
    return None


def _call_alpha_amount(call: dict) -> int | None:
    """alpha_amount argument of a direct transfer_stake call (None when wrapped in batch/proxy)."""
    if not isinstance(call, dict) or call.get("call_function") != "transfer_stake":
        return None
    for arg in call.get("call_args", []):
        if arg.get("name") == "alpha_amount":
            return int(arg["value"])
    return None


class ChainWatcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status: dict = {"state": "starting", "last_block": None, "head": None, "error": None}

    async def run(self):
        while True:
            try:
                async with AsyncSubstrateInterface(self.settings.subtensor_url, ss58_format=42) as substrate:
                    self.status.update(state="connected", error=None)
                    while True:
                        await self._sync(substrate)
                        await asyncio.sleep(self.settings.watcher_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reconnect on any failure
                log.exception("chain watcher error")
                self.status.update(state="error", error=str(e))
                await asyncio.sleep(15)

    async def _sync(self, substrate: AsyncSubstrateInterface):
        finalized = await substrate.get_chain_finalised_head()
        head = await substrate.get_block_number(finalized)
        self.status["head"] = head

        with SessionLocal() as db:
            stored = kv_get(db, LAST_BLOCK_KEY)
        if stored is not None:
            last = int(stored)
        elif self.settings.watcher_start_block:
            last = self.settings.watcher_start_block - 1
        else:
            last = head - 1

        while last < head:
            with SessionLocal() as db:
                addresses = tracked_addresses(db)
            end = min(head, last + self.settings.watcher_batch_size)
            results = await asyncio.gather(
                *(self._scan_block(substrate, n, addresses) for n in range(last + 1, end + 1))
            )
            rows = [row for block_rows in results for row in block_rows]
            with SessionLocal() as db:
                added = store_transfers(db, rows) if rows else 0
                kv_set(db, LAST_BLOCK_KEY, end)
                db.commit()
            if added:
                log.info("stored %d new transfer(s) up to block %d", added, end)
            last = end
            self.status.update(last_block=last, state="syncing" if last < head else "live")

    async def _scan_block(self, substrate, number: int, addresses: set[str]) -> list[dict]:
        if not addresses:
            return []
        block_hash = await substrate.get_block_hash(number)
        events = await substrate.get_events(block_hash)
        hits = []
        for position, ev in enumerate(events):
            try:
                row = parse_event(ev)
            except Exception:
                log.warning("could not parse event in block %d: %r", number, ev, exc_info=True)
                continue
            if not row or (row["from_address"] not in addresses and row["to_address"] not in addresses):
                continue
            idx = ev.get("event_index")
            row["event_index"] = idx if isinstance(idx, int) else position
            row["extrinsic_index"] = ev.get("extrinsic_idx")
            hits.append(row)
        if not hits:
            return []

        block = await substrate.get_block(block_hash)
        extrinsics = block.get("extrinsics", []) if block else []
        timestamp = None
        try:
            ms = _value(await substrate.query("Timestamp", "Now", block_hash=block_hash))
            timestamp = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            log.warning("no timestamp for block %d", number, exc_info=True)

        for row in hits:
            row.update(block_number=number, block_hash=block_hash, timestamp=timestamp, source="chain")
            xi = row["extrinsic_index"]
            if xi is None:
                row["extrinsic_id"] = None
                continue
            row["extrinsic_id"] = f"{number}-{xi:04d}"
            if xi < len(extrinsics):
                ext = _value(extrinsics[xi])
                if isinstance(ext, dict):
                    row["extrinsic_hash"] = ext.get("extrinsic_hash")
                    if row["kind"] == KIND_STAKE_TRANSFER:
                        row["alpha_rao"] = _call_alpha_amount(ext.get("call"))
        return hits


class BalanceRefresher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status: dict = {"state": "starting", "last_run": None, "error": None}
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def request_refresh(self):
        """Thread-safe: ask for an immediate refresh (e.g. after a wallet was added)."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        while True:
            try:
                async with AsyncSubstrateInterface(self.settings.subtensor_url, ss58_format=42) as substrate:
                    while True:
                        await self.refresh_all(substrate)
                        self.status.update(state="ok", last_run=utcnow(), error=None)
                        try:
                            await asyncio.wait_for(self._wake.wait(), self.settings.balance_refresh_seconds)
                        except asyncio.TimeoutError:
                            pass
                        self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("balance refresher error")
                self.status.update(state="error", error=str(e))
                await asyncio.sleep(30)

    async def refresh_all(self, substrate):
        with SessionLocal() as db:
            addresses = list(db.scalars(select(Wallet.address)))
        prices: dict[int, int] = {}
        for address in addresses:
            try:
                free, staked, positions = await self._read_wallet(substrate, address, prices)
            except Exception:
                log.exception("failed to read balance of %s", address)
                continue
            with SessionLocal() as db:
                wallet = db.scalars(select(Wallet).where(Wallet.address == address)).first()
                if wallet:
                    wallet.free_rao = free
                    wallet.staked_rao = staked
                    wallet.stake_detail = json.dumps(positions)
                    wallet.balance_updated_at = utcnow()
                    db.commit()

    async def _alpha_price(self, substrate, netuid: int, cache: dict[int, int]) -> int:
        """RAO of TAO per 1 alpha (root subnet is 1:1)."""
        if netuid == 0:
            return RAO_PER_TAO
        if netuid not in cache:
            cache[netuid] = int(_value(await substrate.runtime_call("SwapRuntimeApi", "current_alpha_price", [netuid])))
        return cache[netuid]

    async def _read_wallet(self, substrate, address: str, prices: dict[int, int]):
        account = _value(await substrate.query("System", "Account", [address]))
        free = int(account["data"]["free"])

        infos = _value(
            await substrate.runtime_call("StakeInfoRuntimeApi", "get_stake_info_for_coldkey", [address])
        ) or []
        positions = []
        staked = 0
        for info in infos:
            alpha = int(info["stake"])
            if alpha <= 0:
                continue
            netuid = int(info["netuid"])
            price = await self._alpha_price(substrate, netuid, prices)
            tao_value = alpha * price // RAO_PER_TAO
            staked += tao_value
            positions.append(
                {
                    "netuid": netuid,
                    "hotkey": ss58.normalize(info["hotkey"]),
                    "alpha": alpha / RAO_PER_TAO,
                    "tao": tao_value / RAO_PER_TAO,
                }
            )
        positions.sort(key=lambda p: -p["tao"])
        return free, staked, positions
