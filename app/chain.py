"""Background tasks talking to the Bittensor (subtensor) chain.

* ChainWatcher follows the finalized head and stores every Balances.Transfer
  and SubtensorModule.StakeTransferred event at or above the minimum amount.
* HistoryBackfill scans the past `backfill_days` once, from an archive node,
  walking backwards from where live syncing started.
* BalanceRefresher periodically reads free balance and stake positions
  (valued in TAO at the current subnet alpha price) for every wallet.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from async_substrate_interface import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException
from sqlalchemy import select

from . import ss58
from .config import Settings
from .db import SessionLocal
from .models import KIND_STAKE_TRANSFER, KIND_TRANSFER, RAO_PER_TAO, Wallet, utcnow
from .services import kv_get, kv_set, store_transfers

log = logging.getLogger("wallet_monitor.chain")

LAST_BLOCK_KEY = "watcher.last_block"
BACKFILL_TOP_KEY = "backfill.top"  # block the backfill started below
BACKFILL_NEXT_KEY = "backfill.next"  # next block to scan (walking down)
BLOCKS_PER_DAY = 7200  # 12 s blocks


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


async def scan_block(substrate, number: int, min_rao: int) -> list[dict]:
    """All qualifying transfers in one block, as Transfer row dicts."""
    block_hash = await substrate.get_block_hash(number)
    events = await substrate.get_events(block_hash)
    hits = []
    for position, ev in enumerate(events):
        try:
            row = parse_event(ev)
        except Exception:
            log.warning("could not parse event in block %d: %r", number, ev, exc_info=True)
            continue
        if not row or row["amount_rao"] < min_rao:
            continue
        # Staking/unstaking shows up as transfers to/from pallet accounts; those aren't user transfers.
        if ss58.is_module_account(row["from_address"]) or ss58.is_module_account(row["to_address"]):
            continue
        # Position in the block's event list. (The event's own "event_index" field is the
        # pallet's event-variant number, identical for every transfer of a kind.)
        row["event_index"] = position
        row["extrinsic_index"] = ev.get("extrinsic_idx")
        hits.append(row)
    if not hits:
        return []

    # Extrinsic hash = blake2b-256 of the encoded extrinsic; cheaper than decoding the block.
    raw = await substrate.rpc_request("chain_getBlock", [block_hash])
    raw_extrinsics = raw["result"]["block"]["extrinsics"]
    timestamp = None
    try:
        ms = _value(await substrate.query("Timestamp", "Now", block_hash=block_hash))
        timestamp = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        log.warning("no timestamp for block %d", number, exc_info=True)

    decoded = None
    if any(r["kind"] == KIND_STAKE_TRANSFER for r in hits):
        block = await substrate.get_block(block_hash)  # decoded, for the alpha amount
        decoded = block.get("extrinsics", []) if block else []

    for row in hits:
        row.update(block_number=number, block_hash=block_hash, timestamp=timestamp, source="chain")
        xi = row["extrinsic_index"]
        if xi is None:
            row["extrinsic_id"] = None
            continue
        row["extrinsic_id"] = f"{number}-{xi:04d}"
        if xi < len(raw_extrinsics):
            data = bytes.fromhex(raw_extrinsics[xi].removeprefix("0x"))
            row["extrinsic_hash"] = "0x" + hashlib.blake2b(data, digest_size=32).hexdigest()
        if row["kind"] == KIND_STAKE_TRANSFER and decoded and xi < len(decoded):
            ext = _value(decoded[xi])
            if isinstance(ext, dict):
                row["alpha_rao"] = _call_alpha_amount(ext.get("call"))
    return hits


async def scan_range(substrate, numbers, min_rao: int) -> list[dict]:
    results = await asyncio.gather(*(scan_block(substrate, n, min_rao) for n in numbers))
    return [row for block_rows in results for row in block_rows]


def _store(rows: list[dict]) -> int:
    with SessionLocal() as db:
        return store_transfers(db, rows) if rows else 0


async def _finalized_head(substrate) -> int:
    return await substrate.get_block_number(await substrate.get_chain_finalised_head())


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

    async def _sync(self, substrate):
        head = await _finalized_head(substrate)
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
            end = min(head, last + self.settings.watcher_batch_size)
            rows = await scan_range(substrate, range(last + 1, end + 1), self.settings.min_transfer_rao)
            added = await asyncio.to_thread(_store, rows)
            with SessionLocal() as db:
                kv_set(db, LAST_BLOCK_KEY, end)
                db.commit()
            if added:
                log.info("stored %d new transfer(s) up to block %d", added, end)
            last = end
            self.status.update(last_block=last, state="syncing" if last < head else "live")


class HistoryBackfill:
    """One-off scan of the past `backfill_days`, newest first, resumable across restarts."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status: dict = {
            "state": "starting", "next": None, "top": None, "stop": None,
            "error": None, "oldest_time": None, "throttled": None,
        }

    @property
    def progress(self) -> float | None:
        s = self.status
        if s["top"] is None or s["stop"] is None or s["next"] is None:
            return None
        total = s["top"] - s["stop"]
        return 1.0 if total <= 0 else min(1.0, (s["top"] - 1 - s["next"]) / total)

    async def run(self):
        while True:
            try:
                async with AsyncSubstrateInterface(self.settings.archive_url, ss58_format=42) as substrate:
                    await self._backfill(substrate)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("history backfill error")
                self.status.update(state="error", error=str(e))
                await asyncio.sleep(30)

    async def _backfill(self, substrate):
        with SessionLocal() as db:
            top = kv_get(db, BACKFILL_TOP_KEY)
            nxt = kv_get(db, BACKFILL_NEXT_KEY)
            if top is None:
                # Start just above where live syncing is, so the two ranges meet.
                live = kv_get(db, LAST_BLOCK_KEY)
                top = int(live) + 1 if live else await _finalized_head(substrate) + 1
                nxt = top - 1
                kv_set(db, BACKFILL_TOP_KEY, top)
                kv_set(db, BACKFILL_NEXT_KEY, nxt)
                db.commit()
        top, nxt = int(top), int(nxt)
        # Recomputed every start, so raising WM_BACKFILL_DAYS extends the scan.
        stop = max(1, top - self.settings.backfill_days * BLOCKS_PER_DAY)
        self.status.update(state="running", top=top, stop=stop, next=nxt, error=None)
        log.info("history backfill: blocks %d..%d (next %d)", stop, top - 1, nxt)

        max_batch = batch = self.settings.backfill_concurrency
        calm = 0
        while nxt >= stop:
            low = max(stop, nxt - batch + 1)
            try:
                rows = await scan_range(substrate, range(low, nxt + 1), self.settings.min_transfer_rao)
            except SubstrateRequestException as e:
                if "rate limit" not in str(e).lower():
                    raise
                # Archive nodes throttle historical queries: back off, then retry the same range slower.
                batch, calm = max(2, batch // 2), 0
                self.status["throttled"] = batch
                log.warning("archive node rate limit; retrying with %d parallel blocks", batch)
                await asyncio.sleep(20)
                continue
            calm += 1
            if batch < max_batch and calm >= 10:
                batch, calm = min(max_batch, batch * 2), 0
                self.status["throttled"] = batch if batch < max_batch else None
            await asyncio.to_thread(_store, rows)
            nxt = low - 1
            with SessionLocal() as db:
                kv_set(db, BACKFILL_NEXT_KEY, nxt)
                db.commit()
            stamps = [r["timestamp"] for r in rows if r.get("timestamp")]
            if stamps:
                self.status["oldest_time"] = min(stamps)
            self.status["next"] = nxt
        self.status.update(state="done")
        log.info("history backfill finished down to block %d", stop)


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
