# Instructions for Claude

This file is read automatically by Claude Code at the start of every session in this folder. It captures rules, gotchas, and decisions that aren't obvious from reading the code.

Read `README.md` for what the project does and how to run it. This file is for **constraints and don't-do-this notes** specifically.

---

## Hard rules

1. **Never modify historical data in any sheet.** The script only appends today's column. Past columns, orphan-date columns (date in row 2/3 but no data), and existing cell values in those columns must not be touched. If a refactor or fix would alter historical values, surface the change to the user before making it.

2. **Never bypass the `app.dtrinity.org` disclaimer modal.** The dLEND markets page is IP-locked to non-restricted regions. The disclaimer requires the user to accept they aren't a resident of a restricted region — that is an action the user must take, not Claude. If you need data from that page, raise it with the user; don't auto-click the agreement.

3. **Never commit `service_account.json` or print its private key contents.** It's a Google service account with edit access to the spreadsheet. The current key was already exposed in a prior conversation transcript and is flagged for rotation (see TODOs below). Do not make this worse.

4. **Don't refactor without asking.** The script has accumulated chain-specific quirks (different decimals, different price sources, force-zero overrides). Cleanup is welcome but always confirm with the user before consolidating, renaming, or extracting helpers — some apparent duplication is intentional.

---

## Non-obvious decisions to remember

### cbBTC (Ethereum dLEND row 15) is forced to $0 on purpose
The token has real supply (~7.9 cbBTC ≈ $620K at market price), but the user explicitly asked to default this row to $0 until they revisit the underlying issue (which they did not explain). The map entry is `15: ("cbBTC", "0x504D...", 8, "ZERO")` — the `"ZERO"` sentinel triggers the price = 0 branch in `fetch_dlend_ethereum`. **Do not "fix" this back to live pricing without the user explicitly asking.**

### FXB bond prices are YTM-derived approximations
`fetch_fxb_prices_via_ytm()` scrapes `facts.frax.finance/fxb` for each bond's YTM and computes price as `1 / (1 + YTM)^t`. This is the standard zero-coupon bond formula and matches the chart prices on facts.frax.finance to within roughly 3–5%. The user accepted this approximation. If higher accuracy is ever required, the alternative is scraping the canvas-rendered price chart, which is much more invasive.

### Decimals differ across chains for the same token
- **dUSD:** 6 decimals on Fraxtal, 18 decimals on Ethereum. Easy to get wrong.
- **sdUSD:** 6 decimals on Fraxtal, 18 decimals on Ethereum.
- BTC-pegged tokens (WBTC, cbBTC, LBTC) are typically 8 decimals everywhere.
- USDC/USDT-pegged tokens (SyrupUSDC, SyrupUSDT) are typically 6 decimals.

The codebase passes decimals explicitly per token instead of auto-detecting, because a previous magnitude-detection attempt picked the wrong scale for low-supply tokens (frxETH on Fraxtal has <1 token total, which broke the heuristic).

### CoinGecko URL slug ≠ API ID, sometimes
Verified examples:
- `staked-frax-usd` works; `frax-staked-frxusd` (the URL slug) does not.
- `syrupusdc` works; `syrup-usdc` (the URL slug) does not.

When adding new tokens, always verify the API ID directly via `https://api.coingecko.com/api/v3/simple/price?ids={candidate}&vs_currencies=usd`. The `coins/{id}` and search endpoints can also help disambiguate.

### Date row position differs across sheets
- Fraxtal balance sheet: row **3**
- Katana balance sheet: row **2**
- Ethereum balance sheet: row **3**
- dLEND Fraxtal: row **3**
- dLEND Ethereum: row **2**

Constants at the top of `scraper.py` capture this per sheet.

### Append-after-last-date logic
The script searches for today's date in the date row. If found, it overwrites that column. If not, it appends at `len(date_row) + 1` — i.e., past any orphan future-dated columns. The Fraxtal balance sheet and dLEND Fraxtal sheet have ~9 months of pre-populated future dates; today gets written into the matching pre-populated column. The Katana sheet was previously trimmed by the user to remove orphan dates.

### Ethereum dUSD balance sheet has multiple data sources
The Ethereum balance sheet (rows 4–10) draws from three places:
- **Raw wallet tokens** (frxUSD, USDS, USDC, USDT) — main wallet `0x84c5…53f4` DeBank token list
- **Frax Staked sfrxUSD position** (row 5) and **Sky Yield Savings USDS position** (row 7) — same wallet, but in DeBank's protocol sections
- **Unallocated AMO dUSD** (row 10) — *separate* wallet `0x3826…f3db` Curve LP USD value

If the user changes any of these wallet addresses, both the constant and the corresponding scrape function need updating.

### Verify-and-retry pass at the end of `main()`
After the parallel scrape, `main()` runs `verify_today()` to read today's column on each sheet and compare against `EXPECTED_SHEET_ROWS`. Any sheet with a blank in an expected row gets re-run (up to `MAX_VERIFY_ATTEMPTS = 2` retry passes). A cell counting `0`/`$0.00` is treated as **present** (intentional zeros like cbBTC). A truly empty cell is treated as **missing**. If anything is still missing after the retries, `main()` exits non-zero so the GitHub Action fails visibly.

There is a `RETRY_DELAY_SECONDS` sleep (default 3600 = 1 hour) **before** each retry, so transient rate-limits / DeBank flakes have time to clear. Override via env var for local debugging: `RETRY_DELAY_SECONDS=0 .venv/bin/python scraper.py`. The GitHub Action's `timeout-minutes` was bumped to 180 to accommodate worst-case 2hr 15min runs (scrape + 1hr + retry + 1hr + retry).

When adding a new data row to any sheet, also add its row number to that sheet's entry in `EXPECTED_SHEET_ROWS` — otherwise the verification pass won't notice when it goes missing.

### Sheet column count vs. row_values length
`sheet.row_values(N)` returns up to `sheet.col_count` cells (with trailing empties), not just up to the last non-empty cell. This caused an early bug where `len(row) + 1` over-shot the grid limit. The script now calls `sheet.add_cols(...)` if the target column exceeds `sheet.col_count`.

---

## Open TODOs

- [ ] **Rotate the Google service account key** in `service_account.json`. The current key (`9828eddbbd0f1ad96a3bc30d0fd71cd658953653`) was leaked in a prior conversation transcript. Revoke it in Google Cloud Console → IAM → Service Accounts → `dtrinity-scraper@dtrinity-scraper.iam.gserviceaccount.com` → Keys, then create + download a new one.
- [ ] **dLEND APY rows** (Fraxtal rows 22/23/24 and Ethereum rows 22/23/24): blocked behind the `app.dtrinity.org` disclaimer modal. Will require either a VPN-routed Playwright scrape (with user permission to accept the disclaimer) or querying the dLEND lending pool contract directly via Etherscan v2 read-contract calls. Currently those rows are blank and the row-25 "Borrow APY (Net)" formula evaluates to 0%.
- [ ] **cbBTC pricing fix** (Ethereum dLEND row 15): user wants $0 for now, plans to revisit.
- [ ] **FXB exact pricing** (vs YTM-approximated): see "Non-obvious decisions" above. Only worth pursuing if the user requests higher precision.
- [ ] **Automation / cron**: currently runs manually. The plan is to schedule daily once the user is confident in the data. Etherscan API key (`FD8P4KY6Z9HMHBWXCRN9MX9U8JTDJSA214`) is hardcoded — should move to env var before any deployment.

---

## Useful one-liners

Run only one sheet's flow (for testing in isolation):
```bash
.venv/bin/python -c "from scraper import fetch_dlend_ethereum, write_to_dlend_ethereum_sheet; write_to_dlend_ethereum_sheet(fetch_dlend_ethereum())"
```

Verify a CoinGecko ID quickly:
```bash
.venv/bin/python -c "import requests; print(requests.get('https://api.coingecko.com/api/v3/simple/price?ids=ID_HERE&vs_currencies=usd').json())"
```

Inspect a column on a sheet:
```bash
.venv/bin/python -c "
import gspread; from google.oauth2.service_account import Credentials
creds = Credentials.from_service_account_file('service_account.json', scopes=['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive'])
sheet = gspread.authorize(creds).open_by_key('1ZXV1U2q_Y6c9NjAJxOJHqaYAQhOJPHQCfGBwFcTDX3E').worksheet('TAB_NAME')
print(sheet.spreadsheet.values_get('TAB_NAME!COLLETTERROW1:COLLETTERROW2', params={'valueRenderOption':'FORMATTED_VALUE'}).get('values'))
"
```
