# AWS Cost Estimator Agent (fast local DB, credential-free)

A conversational AWS cost estimator that runs inside **Claude Code**. Users ask
plain-English questions and get exact, validated hourly / monthly / yearly
On-Demand estimates — **instantly**.

- **Fast** — the agent reads a local price database; every answer is sub-second.
- **Accurate** — prices are the real AWS public prices (not estimates).
- **Fresh** — a background job refreshes the database on a schedule.
- **No AWS credentials** — the refresh pulls AWS's PUBLIC price files.

## How it works

```
   (background, weekly)                         (per question, instant)
 refresh_prices.py ──► prices.db ◄────────────── pricing_server.py ──► Claude Code
 pulls PUBLIC AWS      local cache               reads the DB, validates,
 price files                                     computes hourly/monthly/yearly
```

The slow part (downloading AWS's large price files) happens in the background on
a schedule. User questions only ever touch the fast local database.

## What it prices

Fixed-size (On-Demand, Shared tenancy, Used capacity only):
EC2 (all OS families, license models, SQL options), RDS, EBS, S3.

Usage-metered (the agent asks for the usage numbers):
Lambda, ALB, CloudFront, Route 53, WAF.

Excluded by design for accuracy: Reserved Instances, Savings Plans, Dedicated
Hosts/Instances, capacity reservations, and BYOL unless explicitly requested.

## Setup

Install **Claude Code** and **uv**:

```bash
npm install -g @anthropic-ai/claude-code
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Build the price database (first time)

```bash
cd aws-cost-agent
uv run --with ijson python refresh_prices.py
```

This downloads the public price files and writes `prices.db` (takes a couple of
minutes — the EC2 file is large). You only do this manually once; the scheduler
below keeps it updated.

### 2. Schedule automatic refreshes (macOS)

```bash
bash setup_refresh_schedule.sh
```

This installs a weekly launchd job (Sundays 3am) that re-runs the refresh in the
background, and runs it once immediately. Logs go to `refresh.log`.

> Not on macOS? Add this line to `crontab -e` instead:
> `0 3 * * 0 cd /path/to/aws-cost-agent && ~/.local/bin/uv run --with ijson python refresh_prices.py`

### 3. Use it

```bash
cd aws-cost-agent
claude
```

Approve the `aws-pricing-live` MCP server when prompted, then ask away:

```
Price a Linux t3.medium in Mumbai, hourly monthly and yearly.
How much for a Windows server + MySQL RDS + 100 GB gp3 in us-east-1?
Lambda: 5 million requests/month, 512 MB, 300 ms. And a WAF with 10 rules.
```

Every answer is instant, and each includes how old the price data is.

## Files

| File | Purpose |
|------|---------|
| `refresh_prices.py` | Downloads public AWS prices → `prices.db` (scheduled/background). |
| `pricing_server.py` | Fast MCP server; reads `prices.db`, validates, computes. |
| `setup_refresh_schedule.sh` | Installs the weekly macOS refresh job. |
| `.mcp.json` | Registers the server with Claude Code. |
| `CLAUDE.md` | Standing instructions that shape the agent's behavior. |

## Freshness

AWS list prices change infrequently (roughly monthly), so a weekly refresh keeps
estimates accurate. Every tool result reports `data.days_old`; if the DB hasn't
been refreshed in over 14 days, the agent flags the estimate as stale. To refresh
on demand any time: `uv run --with ijson python refresh_prices.py`.

## Handing it to users

Ship the folder **with a freshly-built `prices.db`** so users are instant on day
one. If they run `setup_refresh_schedule.sh`, their copy self-updates; otherwise
re-send the folder periodically, or have them re-run the refresh command.

## Limits

Planning estimates only — excludes taxes, data transfer (except CloudFront),
support plans, and free-tier discounts. Region-specific; the agent states which
region it used.
