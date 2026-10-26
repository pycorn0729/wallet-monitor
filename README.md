# τ Wallet Monitor

A FastAPI/uvicorn web app that monitors Bittensor wallets for fund movements: TAO transfers and alpha (stake) transfers.

## Features

- **Restricted access.** Anyone can register at `/register`, but a new account can't sign in until an admin approves it on the **Users** page. Admins see a badge with the number of pending registrations.
- **Roles.**
  - **Admin:** manages users and can edit any wallet.
  - **Approver:** signs off on transfers.
  - **User:** manages their own wallets.

  A user can be an admin, an approver, both, or neither.
- **Wallets.** Each wallet has a name, a coldkey address, an owner and a usage purpose. Owners can add, edit and remove their own wallets.
- **Dashboard.**
  - Total balance of all registered wallets (free + staked, with stake valued in TAO at current subnet prices).
  - Totals per owner.
  - The largest wallets.
  - Recent transfers.
  - Unapproved counts.
- **Wallet list filters.** Filter by owner, balance range (`> 1`, `> 100`, …) and text search. The page shows totals for the filtered wallets.
- **Network-wide transfer index (background tasks).**
  - The **chain watcher** follows the finalized head and stores every `Balances.Transfer` and `SubtensorModule.StakeTransferred` event on the network worth at least `WM_MIN_TRANSFER_TAO` (default 0.2 τ). It stores block, extrinsic ID, extrinsic hash and timestamp. For stake transfers it also stores the hotkey and the origin and destination subnets, plus the alpha amount when the transfer was a direct `transfer_stake` call.
  - The **history backfill** scans the past `WM_BACKFILL_DAYS` (default 7, about 50,400 blocks) once, from an archive node, newest first. It takes about an hour at the default settings, resumes after a restart, and shows its progress on the dashboard and graph page.
  - Transfer history, approvals, comments and unapproved counts cover only transfers that involve a registered wallet. All other transfers feed the connection graph.
- **Balance refresher (background task).** Reads each wallet's free balance and stake positions from the chain every `WM_BALANCE_REFRESH_SECONDS`, and immediately after a wallet is added.
- **Transfer history.**
  - Filter by type, approval status, owner, wallet or address.
  - Owners of the wallets involved can comment on what a transfer was for.
  - Approvers can approve or revoke a transfer, one at a time or in bulk.
  - Unapproved counts appear in the nav bar, on the dashboard, in the wallet list, on wallet pages and in the graph.
- **Connection graph (`/graph`).** An interactive diagram of TAO and alpha transfers, built only from the app's own index (no API key needed).
  - With no address it shows transfers involving registered wallets; with an address, that address's transfers.
  - Registered wallets are shown by name and owner, admin-labeled addresses in teal, everything else as a short address.
  - Edges carry totals and transfer counts, and are highlighted with the number of unapproved transfers.
  - Double-click a node to expand its connections.
- **Address labels (`/labels`, admins).** Admins can name any address (an exchange, a partner, a team member). Labels aren't monitored wallets; they only make transfer lists and the graph easier to read. Admins can also label an address straight from the graph's side panel.
- **History import (optional).** With a Taostats API key, "Import history" on a wallet page loads that wallet's transfers from before the indexed range. Records the app already has are de-duplicated.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # set WM_SECRET_KEY, optionally WM_TAOSTATS_API_KEY

# create the first admin (also an approver here)
.venv/bin/python -m app.cli create-user admin --admin --approver

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Sign in, then add wallets on the **Wallets** page. Other people register at `/register`; approve them under **Users**.

Run a single uvicorn worker. The background tasks run inside the app process, so several workers would each run their own watcher.

## Configuration (`.env`, prefix `WM_`)

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | dev value | Session cookie signing key. Change it. |
| `DATABASE_URL` | `sqlite:///./wallet_monitor.db` | Any SQLAlchemy URL. |
| `SUBTENSOR_URL` | finney entrypoint | Subtensor websocket endpoint. A local archive or lite node is faster. |
| `ARCHIVE_URL` | opentensor archive | Archive node used by the history backfill. |
| `MIN_TRANSFER_TAO` | `0.2` | Transfers worth less than this are not stored or shown. Raising it hides smaller stored transfers; lowering it only affects new ones. |
| `BACKFILL_DAYS` | `7` | How far back the one-off backfill scans. Raising it later extends the scan. |
| `BACKFILL_CONCURRENCY` | `20` | Blocks fetched in parallel by the backfill. |
| `TAOSTATS_API_KEY` | empty | Optional; only enables "Import history" on wallet pages. |
| `WATCHER_START_BLOCK` | empty | Block to start scanning from on first run. Empty means the current finalized head. After the first run, progress is kept in the DB. |
| `WATCHER_ENABLED` | `true` | Turns off both background tasks, for example for a read-only replica. |
| `WATCHER_BATCH_SIZE` | `20` | Number of blocks fetched concurrently while catching up. |
| `BALANCE_REFRESH_SECONDS` | `300` | Balance refresh interval. |

## Notes

- **Stake transfer amounts.** The amount in `StakeTransferred` is the TAO value moved. The alpha amount comes from the `transfer_stake` call. If the call was wrapped in a batch or proxy, the alpha amount is left empty.
- **Staked balance** is the sum of each position's alpha × the current subnet alpha price. This is a spot value; the TAO you would get from unstaking can be lower because of slippage.
- **Database size.** About 3 transfers per block pass the 0.2 τ default, roughly 150k rows a week. That's fine for SQLite.
- **Taostats API.** The optional history import uses the new Taostats v1 API (`/v1/transfers` and `/v1/subnets/stake-events?is_transfer=true`). The legacy `/api/...` endpoints stop working on 2026-11-09. Responses are cached for `WM_TAOSTATS_CACHE_SECONDS` (default 300) to stay within rate limits.
- **Sessions and CSRF.** Sessions are signed cookies with `SameSite=Lax`. Serve the app over HTTPS behind a reverse proxy in production.
