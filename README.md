# dTRINITY Daily Scraper

Scrapes on-chain data and DeFi protocol positions for the dTRINITY protocol across multiple chains, then writes the values into a single Google Sheet (the dTRINITY balance + dLEND stats workbook).

Runs once per day, automatically, via the `dTRINITY Daily Scrape` GitHub Action (`.github/workflows/daily.yml`, cron `0 8 * * *` = 08:00 UTC). It can also be triggered by hand from the Actions tab, or run locally — see [Running](#running).

> **If the daily column stops appearing, check whether the workflow is still enabled.** GitHub disables scheduled workflows in public repos after 60 days with no repository activity, silently — no failure, no email. This happened once already (last commit 2026-06-29 → last run 2026-08-28, exactly +60 days). The workflow now pushes a keepalive commit when the branch has been quiet for 45 days, which prevents it recurring, but a workflow that is *already* disabled has to be re-enabled by hand: **Actions → dTRINITY Daily Scrape → Enable workflow**.

---

## What it writes

The script writes one column per day into each of these tabs in spreadsheet `1ZXV1U2q_Y6c9NjAJxOJHqaYAQhOJPHQCfGBwFcTDX3E`:

| Tab | What it tracks |
|---|---|
| `dUSD Balance Sheet (Fraxtal)` | Wallet `0x624E…FeBC` token holdings + Curve/Convex LP positions on wallet `0xdb10…5431` + dUSD supply on Fraxtal |
| `dUSD Balance Sheet (Katana)` | Wallet `0xA5f9…6A49` token holdings + dUSD supply on Katana |
| `dUSD Balance Sheet (Ethereum)` | Wallet `0x84c5…53f4` token holdings + Frax/Sky protocol positions + AMO Curve LP from wallet `0x3826…f3db` + dUSD supply on Ethereum |
| `dLEND Stats (Fraxtal)` | dLEND lending pool reserve supplies, dUSD debt, sdUSD supply on Fraxtal |
| `dLEND Stats (Ethereum)` | dLEND lending pool reserve supplies, dUSD debt, sdUSD supply on Ethereum |

Each daily column also writes the relevant subtotal/ratio formulas (Total Assets, Total Liabilities, CR Ratio, etc.) and copies cell formatting (borders, $/% number formats) from a template column so the new column matches the historical visual style.

---

## Data sources

- **Wallet token holdings:** [DeBank](https://debank.com) — scraped via headless Playwright (Chromium)
- **Protocol positions** (Curve, Convex, Frax Staked, Sky Yield): same DeBank page
- **Token supplies on-chain:** Etherscan v2 API (`api.etherscan.io/v2/api`) — supports Fraxtal (chain 252), Ethereum (chain 1), and other chains via the same endpoint
- **dUSD supply on Katana:** Etherscan v2 API, chain 747474 (was a `katanascan.com` page scrape until 2026-06-29 — it flaked constantly and blanked row 9)
- **Token prices:** [CoinGecko free API](https://api.coingecko.com/api/v3/simple/price)
- **FXB bond prices:** scraped from `facts.frax.finance/fxb` table for YTM, then computed `Price = 1 / (1 + YTM)^t` (zero-coupon bond formula)

---

## Setup

```bash
cd "/Users/scott/Documents/Claude/dTRINITY/Daily tasks using Claude"
python3 -m venv .venv
.venv/bin/pip install requests playwright gspread google-auth
.venv/bin/playwright install chromium
```

Credentials live in `service_account.json` (Google service account with edit access to the spreadsheet). **This file should never be committed to git or shared** — see the `CLAUDE.md` note about key rotation.

---

## Running

```bash
cd "/Users/scott/Documents/Claude/dTRINITY/Daily tasks using Claude"
.venv/bin/python scraper.py
```

Optional shell alias for daily use:
```bash
echo 'alias dtrinity='\''cd "/Users/scott/Documents/Claude/dTRINITY/Daily tasks using Claude" && .venv/bin/python scraper.py'\''' >> ~/.zshrc
source ~/.zshrc
# Then: dtrinity
```

The scrape itself takes roughly 2–4 minutes (most of it is Playwright spinning up headless Chromium for the DeBank scrapes — six Chromium launches across the five sheets).

Wall-clock time for a CI run is usually much longer: if the verify pass finds a blank in an expected row, `main()` sleeps `RETRY_DELAY_SECONDS` (default 3600) before re-running that sheet, up to `MAX_VERIFY_ATTEMPTS` times. In practice most scheduled runs land around 62 minutes, i.e. one retry pass. Set `RETRY_DELAY_SECONDS=0` for local debugging.

---

## Token contract reference

### Fraxtal (chain ID 252)
| Symbol | Contract | Decimals | Notes |
|---|---|---|---|
| dUSD (supply) | `0x788D96f655735f52c676A133f4dFC53cEC614d4A` | 6 | Used by Fraxtal balance sheet row 16 |
| dLEND aToken: dUSD | `0x29d0256fe397F6e442464982C4Cba7670646059b` | 6 | dLEND Fraxtal row 4 |
| dLEND debt: dUSD | `0x6B937da34fb213763458a3b7672B950df1F560dE` | 6 | dLEND Fraxtal row 19 (debt outstanding) |
| dLEND aToken: frxETH | `0x29155d25B11EE91FEC887b09DA8ef86951799Ee0` | 18 | dLEND Fraxtal row 6 |
| dLEND aToken: sfrxUSD | `0x8315047C1fdfb27656C2893B432324919F7448DE` | 18 | row 5 |
| dLEND aToken: sfrxETH | `0x1F075573E3eB0D7B2D10266bA8c2c2449Fa862F7` | 18 | row 7 |
| dLEND aToken: sUSDe | `0x12ED58F0744dE71C39118143dCc26977Cb99cDef` | 18 | row 8 |
| dLEND aToken: scrvUSD | `0xc569B9e1A9144E365b60CBE8a16B37bA4a764BC9` | 18 | row 9 |
| dLEND aToken: sDAI | `0xDba7B882B61b7B86f3BA897F84C36a15CaEF3345` | 18 | row 10 |
| dLEND aToken: USDe | `0x6AE1450D550e44Bb014D4c8CD98592863edB0706` | 18 | row 11 |
| dLEND aToken: FXB 2025-12-31 | `0x5037aE643839CEdD678368d3614F03eD1179c5D4` | 18 | row 12 (matured) |
| dLEND aToken: FXB 2026-12-31 | `0x2D8AE7d18D61Dd02eBF5367bb62bbd485736a0ab` | 18 | row 13 |
| dLEND aToken: FXB 2029-12-31 | `0xE919136c67493046fc26bF04E86A82C747eE2EDf` | 18 | row 14 |
| dLEND aToken: FXB 2055-12-31 | `0xF1082f0323E6a35c93A05160E0e3054B62BF4C0e` | 18 | row 15 |
| dLEND aToken: wFRAX | `0x64188DE66adD8B3d813F2Dc157dFeDaf74F10ede` | 18 | row 16 |
| dLEND aToken: sdUSD | `0x58AcC2600835211Dcb5847c5Fa422791Fd492409` | 6 | row 20 |

### Ethereum (chain ID 1)
| Symbol | Contract | Decimals | Notes |
|---|---|---|---|
| dUSD (supply) | `0x07fFf99e1664d9B116fbC158c0E99785F81cA236` | 18 | Ethereum balance sheet row 11 — different decimals from Fraxtal! |
| dLEND aToken: dUSD | `0x5CC741931D01Cb1ADdE193222Dfb1ad75930fd60` | 18 | dLEND Ethereum row 3 |
| dLEND debt: dUSD | `0x9477297FeacD988bE2E8bC42dFB0edf44bbfb59B` | 18 | dLEND Ethereum row 19 |
| dLEND aToken: sfrxUSD | `0x979fb79D36c0D3006cDe38e992d9f51768efaAd8` | 18 | row 4 |
| dLEND aToken: ETH (WETH) | `0xab035F35f3e9891f5756f54bc26DD4a51cD02989` | 18 | row 5 |
| dLEND aToken: wstETH | `0xDFAEe67e4EF9009A728dae88453275c616A5877f` | 18 | row 6 |
| dLEND aToken: sfrxETH | `0x3De01b66b97EAF98603920E9e850c6d7b2411dDF` | 18 | row 7 |
| dLEND aToken: rETH | `0x7F90988393D1db8ef33cC9f4294A7dDA389D7cF1` | 18 | row 8 |
| dLEND aToken: sUSDe | `0x2B820Fd4911876160C3988E57A10D8A5B85dFf35` | 18 | row 9 |
| dLEND aToken: sUSDS | `0xB33276a11CaBe6e1cD0252C4E1770FfD30a8029c` | 18 | row 10 |
| dLEND aToken: SyrupUSDC | `0xa5535fC58Fd1be43a37367f4b66669f691A26eae` | 6 | row 11 |
| dLEND aToken: SyrupUSDT | `0xA17571a95bd22dc1a6F54d7f6E396D2398DFe493` | 6 | row 12 |
| dLEND aToken: LBTC | `0xc247736EAaa1B45D21ae1668D13965B4b50e9011` | 8 | row 13 |
| dLEND aToken: WBTC | `0x88A4EeD28A1d7bCee95228721678662421A1C748` | 8 | row 14 |
| dLEND aToken: cbBTC | `0x504D0Eacbf9ea5645A8A9da1b15f3708A5483AcC` | 8 | row 15 — currently forced to $0 |
| dLEND aToken: PAXG | `0x8A9384b094D34db0110988D497E96B17F3B9C930` | 18 | row 16 |
| dLEND aToken: sdUSD | `0x7CB20517776636eD76b68EdB3D99DCce356ABf02` | 18 | row 20 |

### Katana
| Symbol | Source | Notes |
|---|---|---|
| dUSD (supply) | `0xcA52d08737E6Af8763a2bF6034B3B03868f24DDA` via Etherscan v2 (chain ID 747474), 18 decimals | Katana balance sheet row 9 |

---

## CoinGecko ID reference

URL slug is sometimes ≠ API ID. Verified IDs:

| Token | API ID |
|---|---|
| frxETH | `frax-ether` |
| sfrxETH | `staked-frax-ether` |
| sfrxUSD | `staked-frax-usd` (note: NOT `frax-staked-frxusd` despite the URL slug) |
| sUSDe | `ethena-staked-usde` |
| USDe | `ethena-usde` |
| sDAI | `savings-dai` |
| sUSDS | `susds` |
| scrvUSD | `savings-crvusd` |
| WETH | `weth` |
| wstETH | `wrapped-steth` |
| rETH | `rocket-pool-eth` |
| WBTC | `wrapped-bitcoin` |
| cbBTC | `coinbase-wrapped-btc` (currently overridden to `ZERO` for row 15) |
| LBTC | `lombard-staked-btc` |
| PAXG | `pax-gold` |
| SyrupUSDC | `syrupusdc` (note: NOT `syrup-usdc`) |
| SyrupUSDT | `syrupusdt` |
| wFRAX | `frax` |
| sdUSD | `dtrinity-staked-dusd` (covers both Fraxtal and Ethereum sdUSD) |
