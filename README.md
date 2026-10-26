# τ Wallet Monitor

A FastAPI/uvicorn web app that monitors Bittensor wallets for fund movements: TAO transfers and alpha (stake) transfers.

## Features

- **Restricted access.** Anyone can register at `/register`, but a new account can't sign in until an admin approves it on the **Users** page. Admins see a badge with the number of pending registrations. Admins can also create accounts directly.
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
- **Chain watcher (background task).**
  - Scans every finalized block for `Balances.Transfer` and `SubtensorModule.StakeTransferred` events.
  - Stores the events that touch a registered wallet, with block, extrinsic ID, extrinsic hash and timestamp.
  - For stake transfers it also stores the hotkey, the origin and destination subnets, and the alpha amount.
- **Balance refresher (background task).** Reads each wallet's free balance and stake positions from the chain every `WM_BALANCE_REFRESH_SECONDS`, and immediately after a wallet is added.
- **Transfer history.**
  - Filter by type, approval status, owner, wallet or address.
  - Owners of the wallets involved can comment on what a transfer was for.
  - Approvers can approve or revoke a transfer, one at a time or in bulk.
  - Unapproved counts appear in the nav bar, on the dashboard, in the wallet list, on wallet pages and in the graph.
- **Connection graph (`/graph`).** An interactive diagram of TAO and alpha transfers.
  - With a Taostats API key it loads the full history of any address, including counterparties that aren't registered.
  - Without one it uses the transfers stored by the app.
  - Registered wallets are shown by name and owner.
  - Edges carry totals and transfer counts, and are highlighted with the number of unapproved transfers.
  - Double-click a node to expand its connections.
- **History import.** "Import history" on a wallet page backfills that wallet's past transfers from Taostats, because the watcher only sees new blocks. Records the app already has are de-duplicated.

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
| `TAOSTATS_API_KEY` | empty | Enables the Taostats graph source and history import. |
| `WATCHER_START_BLOCK` | empty | Block to start scanning from on first run. Empty means the current finalized head. After the first run, progress is kept in the DB. |
| `WATCHER_ENABLED` | `true` | Turns off both background tasks, for example for a read-only replica. |
| `WATCHER_BATCH_SIZE` | `20` | Number of blocks fetched concurrently while catching up. |
| `BALANCE_REFRESH_SECONDS` | `300` | Balance refresh interval. |

## Notes

- **Stake transfer amounts.** The amount in `StakeTransferred` is the TAO value moved. The alpha amount comes from the `transfer_stake` call. If the call was wrapped in a batch or proxy, the alpha amount is left empty.
- **Staked balance** is the sum of each position's alpha × the current subnet alpha price. This is a spot value; the TAO you would get from unstaking can be lower because of slippage.
- **Taostats API.** The app uses the new Taostats v1 API (`/v1/transfers` and `/v1/subnets/stake-events?is_transfer=true`). The legacy `/api/...` endpoints stop working on 2026-11-09. Responses are cached for `WM_TAOSTATS_CACHE_SECONDS` (default 300) to stay within rate limits.
- **Sessions and CSRF.** Sessions are signed cookies with `SameSite=Lax`. Serve the app over HTTPS behind a reverse proxy in production.
