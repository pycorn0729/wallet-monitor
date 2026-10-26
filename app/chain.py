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
import time
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
            amount = int(arg["value"])
            # u64::MAX means "move all my stake": not the amount actually moved (and too big
            # for an SQLite integer). The event's TAO amount still records what moved.
            return None if amount >= 2**63 else amount
    return None


def _read_compact(data: bytes, i: int) -> tuple[int, int]:
    """SCALE compact integer at data[i]; returns (value, next offset)."""
    mode = data[i] & 3
    if mode == 0:
        return data[i] >> 2, i + 1
    if mode == 1:
        return int.from_bytes(data[i:i + 2], "little") >> 2, i + 2
    if mode == 2:
        return int.from_bytes(data[i:i + 4], "little") >> 2, i + 4
    n = (data[i] >> 2) + 4
    return int.from_bytes(data[i + 1:i + 1 + n], "little"), i + 1 + n


def _inherent_timestamp(raw_extrinsic: str) -> datetime | None:
    """Block time from the Timestamp.set inherent (always the block's first extrinsic),
    read straight from its bytes instead of querying storage. None if it doesn't look right."""
    try:
        data = bytes.fromhex(raw_extrinsic.removeprefix("0x"))
        _, i = _read_compact(data, 0)  # length prefix
        if data[i] & 0x80:  # signed => not an inherent
            return None
        ms, _ = _read_compact(data, i + 3)  # skip version byte + 2-byte call index
    except (ValueError, IndexError):
        return None
    if not 1_500_000_000_000 < ms < 4_000_000_000_000:  # sanity: 2017..2096 in ms
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


async def _block_hashes(substrate, numbers: list[int]) -> list[str]:
    """All hashes in one RPC (chain_getBlockHash accepts a list of numbers)."""
    hashes = (await substrate.rpc_request("chain_getBlockHash", [numbers]))["result"]
    if not isinstance(hashes, list) or len(hashes) != len(numbers) or not all(hashes):
        raise RuntimeError(f"node returned no hash for some of blocks {numbers[0]}..{numbers[-1]}")
    return hashes


async def _runtimes(substrate, hashes: list[str]) -> list:
    """Runtime (metadata) for each block. Resolving it costs two RPCs per block, so check only
    the range ends: the version can only go up, so equal ends mean the whole range shares it."""
    first = await substrate.init_runtime(block_hash=hashes[0])
    last = await substrate.init_runtime(block_hash=hashes[-1]) if len(hashes) > 1 else first
    if first.runtime_version == last.runtime_version:
        return [first] * len(hashes)
    log.info("runtime upgrade %s -> %s inside scanned range", first.runtime_version, last.runtime_version)
    return [await substrate.init_runtime(block_hash=h) for h in hashes]


async def scan_block(substrate, number: int, block_hash: str, runtime, min_rao: int) -> list[dict]:
    """All qualifying transfers in one block, as Transfer row dicts.
    Costs one RPC, plus one more (the raw block) only when the block has a qualifying transfer."""
    events = _value(await substrate.query("System", "Events", block_hash=block_hash, runtime=runtime))
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

    # One fetch of the raw block gives extrinsic hashes (blake2b-256 of the encoded
    # extrinsic), the timestamp (first inherent) and, decoded locally, alpha amounts.
    raw_extrinsics = (await substrate.rpc_request("chain_getBlock", [block_hash]))["result"]["block"]["extrinsics"]
    timestamp = _inherent_timestamp(raw_extrinsics[0]) if raw_extrinsics else None
    if timestamp is None:
        try:
            ms = _value(await substrate.query("Timestamp", "Now", block_hash=block_hash, runtime=runtime))
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
        if xi >= len(raw_extrinsics):
            continue
        data = bytes.fromhex(raw_extrinsics[xi].removeprefix("0x"))
        row["extrinsic_hash"] = "0x" + hashlib.blake2b(data, digest_size=32).hexdigest()
        if row["kind"] == KIND_STAKE_TRANSFER:
            try:
                ext = _value(await substrate.decode_scale("Extrinsic", raw_extrinsics[xi], runtime=runtime))
                row["alpha_rao"] = _call_alpha_amount(ext.get("call")) if isinstance(ext, dict) else None
            except Exception:
                log.debug("could not decode extrinsic %s", row["extrinsic_id"], exc_info=True)
    return hits


async def scan_range(substrate, numbers, min_rao: int, parallel: int) -> list[dict]:
    """Scan consecutive blocks with at most `parallel` blocks in flight at once."""
    numbers = list(numbers)
    if not numbers:
        return []
    hashes = await _block_hashes(substrate, numbers)
    runtimes = await _runtimes(substrate, hashes)
    gate = asyncio.Semaphore(parallel)

    async def one(n, h, rt):
        async with gate:
            return await scan_block(substrate, n, h, rt, min_rao)

    results = await asyncio.gather(*(one(n, h, rt) for n, h, rt in zip(numbers, hashes, runtimes)))
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
            rows = await scan_range(
                substrate, range(last + 1, end + 1), self.settings.min_transfer_rao, self.settings.watcher_concurrency
            )
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
        self._since: tuple[float, int] | None = None

    @property
    def progress(self) -> float | None:
        s = self.status
        if s["top"] is None or s["stop"] is None or s["next"] is None:
            return None
        total = s["top"] - s["stop"]
        return 1.0 if total <= 0 else min(1.0, (s["top"] - 1 - s["next"]) / total)

    @property
    def blocks_per_second(self) -> float | None:
        """Average scan rate since this run started (throttling included)."""
        since = self._since
        if not since or self.status["next"] is None:
            return None
        elapsed, done = time.monotonic() - since[0], since[1] - self.status["next"]
        return done / elapsed if elapsed > 60 and done > 0 else None

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
        self._since = (time.monotonic(), nxt)  # for the scan rate shown on the dashboard
        log.info("history backfill: blocks %d..%d (next %d)", stop, top - 1, nxt)

        batch = self.settings.backfill_batch_size
        max_parallel = parallel = self.settings.backfill_concurrency
        calm = 0
        while nxt >= stop:
            low = max(stop, nxt - batch + 1)
            try:
                rows = await scan_range(substrate, range(low, nxt + 1), self.settings.min_transfer_rao, parallel)
            except SubstrateRequestException as e:
                if "rate limit" not in str(e).lower():
                    raise
                # Archive nodes throttle historical queries: back off, then retry the same range slower.
                parallel, calm = max(1, parallel // 2), 0
                self.status["throttled"] = parallel
                log.warning("archive node rate limit; retrying with %d parallel blocks", parallel)
                await asyncio.sleep(20)
                continue
            calm += 1
            if parallel < max_parallel and calm >= 5:
                parallel, calm = min(max_parallel, parallel * 2), 0
                self.status["throttled"] = parallel if parallel < max_parallel else None
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


def sync_summary(watcher: ChainWatcher, backfill: HistoryBackfill) -> dict:
    """How far the database has caught up with the chain, for the dashboard."""
    w, b = watcher.status, backfill.status
    head, last = w["head"], w["last_block"]
    behind = max(0, head - last) if head is not None and last is not None else None

    total = done = left = eta = None
    if b["top"] is not None and b["stop"] is not None and b["next"] is not None:
        total = max(0, b["top"] - b["stop"])
        left = max(0, b["next"] - b["stop"] + 1)
        done = total - left
        rate = backfill.blocks_per_second
        if rate and left:
            eta = left / rate

    # Indexed range: from the backfill's lowest scanned block up to the live cursor.
    low = b["next"] + 1 if b["next"] is not None else (b["top"] if b["top"] is not None else None)
    high = last
    covered = max(0, high - low + 1) if low is not None and high is not None else None
    # Target: the whole backfill window up to the current finalized head.
    target = head - b["stop"] + 1 if head is not None and b["stop"] is not None else None
    return {
        "live": {**w, "behind": behind},
        "backfill": {**b, "progress": backfill.progress, "total": total, "done": done, "left": left, "eta": eta},
        "coverage": {"low": low, "high": high, "blocks": covered,
                     "days": covered / BLOCKS_PER_DAY if covered is not None else None,
                     "progress": min(1.0, covered / target) if covered is not None and target else None},
    }


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
        """Read every wallet against one pinned block with bulk calls: a handful of RPCs per
        cycle, whatever the number of wallets (one extra per subnet with stake, for its price)."""
        with SessionLocal() as db:
            addresses = list(db.scalars(select(Wallet.address)))
        if not addresses:
            return
        block_hash = await substrate.get_chain_finalised_head()
        runtime = await substrate.init_runtime(block_hash=block_hash)

        keys = [
            await substrate.create_storage_key("System", "Account", [a], block_hash=block_hash) for a in addresses
        ]
        free = {a: 0 for a in addresses}
        for key, account in await substrate.query_multi(keys, block_hash=block_hash, runtime=runtime):
            account = _value(account)
            if account:
                free[ss58.normalize(key.params[0])] = int(account["data"]["free"])

        stake_lists = _value(
            await substrate.runtime_call(
                "StakeInfoRuntimeApi", "get_stake_info_for_coldkeys", [addresses], block_hash=block_hash
            )
        ) or []
        stakes = {ss58.normalize(coldkey): infos or [] for coldkey, infos in stake_lists}

        netuids = {int(i["netuid"]) for infos in stakes.values() for i in infos if int(i["stake"]) > 0}
        prices = await self._alpha_prices(substrate, netuids, block_hash)

        updates = {}
        for address in addresses:
            positions, staked = [], 0
            for info in stakes.get(address, []):
                alpha = int(info["stake"])
                if alpha <= 0:
                    continue
                netuid = int(info["netuid"])
                tao_value = alpha * prices[netuid] // RAO_PER_TAO
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
            updates[address] = (free[address], staked, positions)

        with SessionLocal() as db:
            for wallet in db.scalars(select(Wallet).where(Wallet.address.in_(list(updates)))):
                wallet.free_rao, wallet.staked_rao, positions = updates[wallet.address]
                wallet.stake_detail = json.dumps(positions)
                wallet.balance_updated_at = utcnow()
            db.commit()

    async def _alpha_prices(self, substrate, netuids: set[int], block_hash: str) -> dict[int, int]:
        """RAO of TAO per 1 alpha for each subnet (root subnet is 1:1), at the pinned block."""
        prices = {0: RAO_PER_TAO}
        todo = sorted(n for n in netuids if n != 0)
        results = await asyncio.gather(
            *(substrate.runtime_call("SwapRuntimeApi", "current_alpha_price", [n], block_hash=block_hash) for n in todo)
        )
        prices.update({n: int(_value(r)) for n, r in zip(todo, results)})
        return prices
