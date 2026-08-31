import os, re, time, json, threading, requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials
from eth_abi import decode as _abi_decode

# Etherscan v2 free tier allows 5 req/sec but reports rate-limit errors at 3/sec under load.
# Throttle globally to ~2.5/sec to stay safe across parallel sheets.
_etherscan_lock = threading.Lock()
_etherscan_last_call_t = [0.0]
ETHERSCAN_MIN_INTERVAL = 0.4

def _etherscan_get(params, max_retries=4):
    """Throttled + retrying GET against the Etherscan v2 API."""
    last = None
    for attempt in range(max_retries):
        with _etherscan_lock:
            elapsed = time.time() - _etherscan_last_call_t[0]
            if elapsed < ETHERSCAN_MIN_INTERVAL:
                time.sleep(ETHERSCAN_MIN_INTERVAL - elapsed)
            _etherscan_last_call_t[0] = time.time()
        try:
            r = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=20).json()
            last = r
            if r.get("status") == "1":
                return r
            msg = str(r.get("result", "")) + str(r.get("message", ""))
            if "rate limit" in msg.lower() or "max calls" in msg.lower():
                wait = (attempt + 1) * 1.5
                print(f"  [throttle] rate limited, sleeping {wait:.1f}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            print(f"  [throttle] request error attempt {attempt+1}: {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(2)
    return last

# Load .env from script directory if present (local-run convenience; not used in GH Actions)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
if not ETHERSCAN_API_KEY:
    raise SystemExit("ETHERSCAN_API_KEY not set. Add it to .env (local) or GitHub Secrets (Actions).")

SPREADSHEET_ID = "1ZXV1U2q_Y6c9NjAJxOJHqaYAQhOJPHQCfGBwFcTDX3E"
SHEET_TAB_NAME = "dUSD Balance Sheet (Fraxtal)"
SERVICE_ACCOUNT_FILE = str(Path(__file__).parent / "service_account.json")
DEBANK_URL = "https://debank.com/profile/0x624E12dE7a97B8cFc1AD1F050a1c9263b1f4FeBC"
DEBANK_URL_PROTOCOLS = "https://debank.com/profile/0xdb104e0bb0b2955f69e8e092eb80831913d85431"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
FRAXTAL_CHAIN_ID = 252
DUSD_CONTRACT = "0x788D96f655735f52c676A133f4dFC53cEC614d4A"


def _load_google_credentials():
    """Load Google service account creds from env var (cloud) or local file."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_str:
        return Credentials.from_service_account_info(json.loads(json_str), scopes=scopes)
    if Path(SERVICE_ACCOUNT_FILE).exists():
        return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    raise SystemExit("No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON env var or place service_account.json next to scraper.py.")

TOKEN_ROW_MAP = {
    "frxusd":  4,
    "sfrxusd": 5,
    "dai":     6,
    "sdai":    7,
    "usdc":    8,
    "usdt":    9,
}
CONVEX_ROW = 10
CURVE_ROW = 11
DUSD_ROW = 16

KATANA_SHEET_TAB = "dUSD Balance Sheet (Katana)"
DEBANK_URL_KATANA = "https://debank.com/profile/0xA5f9F6238406B1301D0ED09555a2893dc1A26A49"
KATANA_CHAIN_ID = 747474
KATANA_DUSD_CONTRACT = "0xcA52d08737E6Af8763a2bF6034B3B03868f24DDA"

# Katana sheet token map (sheet labels in column A; vb-prefixed wallet tokens map to unprefixed sheet rows)
KATANA_TOKEN_ROW_MAP = {
    "frxusd":  3,
    "sfrxusd": 4,
    "vbusdc":  5,  # → "USDC (Katana)"
    "vbusdt":  6,  # → "USDT (Katana)"
    "ausd":    7,
    "yusd":    8,
}
KATANA_DUSD_ROW = 9

# --- Katana DeBank scrape reliability knobs -------------------------------
# The Katana wallet scrape returned 0 of 6 tokens on the first pass of EVERY
# scheduled run (verified across a month of Action logs: rows 3-8 missing in
# verify pass 1, recovered on retry). Because an empty scrape did not raise,
# main() reported "5/5 sheets succeeded" and only self-healed via the 1-hour
# RETRY_DELAY_SECONDS sleep — which is why every run took ~62 minutes, and why
# 2026-08-20 and 2026-08-27 burned BOTH retries and came one flake away from
# exiting non-zero with a blank Katana column.
#
# Instead of a blind settle, poll until the expected tokens actually render,
# and retry in-process before falling back to the hourly retry.
KATANA_SCRAPE_ATTEMPTS = 3          # in-process attempts before giving up
KATANA_SCRAPE_BACKOFF_SECONDS = 10  # wait between those attempts
KATANA_RENDER_TIMEOUT_SECONDS = 45  # how long to wait for tokens to paint
KATANA_RENDER_POLL_SECONDS = 2      # gap between reads of the rendered body
# Accept a partial scrape only as a last resort. A wallet token that drops to a
# zero balance can legitimately vanish from DeBank's list, so requiring all six
# would be brittle; 0-of-6 (the observed failure) is far below this floor.
KATANA_MIN_TOKENS = 4

ETHEREUM_SHEET_TAB = "dUSD Balance Sheet (Ethereum)"
DEBANK_URL_ETHEREUM = "https://debank.com/profile/0x84c58066a4408454b7380f168c95f571419253f4"
DEBANK_URL_AMO_ETHEREUM = "https://debank.com/profile/0x38262effcd17cd64f6311ef688b2caa61102f3db"
DUSD_CONTRACT_ETHEREUM = "0x07fFf99e1664d9B116fbC158c0E99785F81cA236"
ETHEREUM_CHAIN_ID = 1

# Ethereum sheet — raw wallet tokens (rows 4, 6, 8, 9) plus protocol positions (rows 5, 7)
ETHEREUM_RAW_TOKEN_ROW_MAP = {
    "frxusd": 4,
    "usds":   6,
    "usdc":   8,
    "usdt":   9,
}
# Protocol positions: (sheet_key, row, header, subtitle)
ETHEREUM_PROTOCOL_POSITIONS = [
    ("sfrxusd", 5, "Frax", "Staked"),
    ("susds",   7, "Sky",  "Yield"),
]
ETHEREUM_AMO_ROW = 10
ETHEREUM_DUSD_ROW = 11

# dLEND Stats (Fraxtal)
DLEND_FRAXTAL_SHEET_TAB = "dLEND Stats (Fraxtal)"
DLEND_DUSD_CONTRACT = "0x29d0256fe397F6e442464982C4Cba7670646059b"   # row 4 (Total dUSD Supply)
DLEND_SDUSD_CONTRACT = "0x6B937da34fb213763458a3b7672B950df1F560dE"  # row 19 (Total dUSD Debt — actually sdUSD per user)
DLEND_FRXETH_CONTRACT = "0x29155d25B11EE91FEC887b09DA8ef86951799Ee0" # row 6, multiplied by frxETH price
COINGECKO_FRXETH_URL = "https://api.coingecko.com/api/v3/simple/price?ids=frax-ether&vs_currencies=usd"

# Additional dLEND token map (rows 5, 7-16); each entry: (symbol, contract, decimals, coingecko_id or None for $1, or "FXB" for bond pricing)
DLEND_EXTRA_TOKENS = {
    5:  ("sfrxUSD",     "0x8315047C1fdfb27656C2893B432324919F7448DE", 18, "staked-frax-usd"),
    7:  ("sfrxETH",     "0x1F075573E3eB0D7B2D10266bA8c2c2449Fa862F7", 18, "staked-frax-ether"),
    8:  ("sUSDe",       "0x12ED58F0744dE71C39118143dCc26977Cb99cDef", 18, "ethena-staked-usde"),
    9:  ("scrvUSD",     "0xc569B9e1A9144E365b60CBE8a16B37bA4a764BC9", 18, "savings-crvusd"),
    10: ("sDAI",        "0xDba7B882B61b7B86f3BA897F84C36a15CaEF3345", 18, "savings-dai"),
    11: ("USDe",        "0x6AE1450D550e44Bb014D4c8CD98592863edB0706", 18, "ethena-usde"),
    12: ("FXB20251231", "0x5037aE643839CEdD678368d3614F03eD1179c5D4", 18, "FXB:2025-12-31"),
    13: ("FXB20261231", "0x2D8AE7d18D61Dd02eBF5367bb62bbd485736a0ab", 18, "FXB:2026-12-31"),
    14: ("FXB20291231", "0xE919136c67493046fc26bF04E86A82C747eE2EDf", 18, "FXB:2029-12-31"),
    15: ("FXB20551231", "0xF1082f0323E6a35c93A05160E0e3054B62BF4C0e", 18, "FXB:2055-12-31"),
    16: ("wFRAX",       "0x64188DE66adD8B3d813F2Dc157dFeDaf74F10ede", 18, "frax"),
    20: ("sdUSD",       "0x58AcC2600835211Dcb5847c5Fa422791Fd492409", 6,  "dtrinity-staked-dusd"),
}
FRAX_FACTS_FXB_URL = "https://facts.frax.finance/fxb"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# dLEND Stats (Ethereum)
DLEND_ETHEREUM_SHEET_TAB = "dLEND Stats (Ethereum)"
DLEND_ETHEREUM_DUSD_CONTRACT = "0x5CC741931D01Cb1ADdE193222Dfb1ad75930fd60"      # row 3, default $1
DLEND_ETHEREUM_DUSD_DEBT_CONTRACT = "0x9477297FeacD988bE2E8bC42dFB0edf44bbfb59B" # row 19, default $1
DLEND_ETHEREUM_DATE_ROW = 2

# dLEND APYs (rows 22-25) — dUSD reserve only.
# Addresses sourced from dtrinity/interface shared/config/markets/{fraxtal,ethereum}.ts.
# Type strings reflect the deployed UiPoolDataProvider / UiIncentiveDataProvider
# ABIs (verified identical across Fraxtal + Ethereum at integration time).
DLEND_APY_ADDRESSES = {
    FRAXTAL_CHAIN_ID: {
        "ui_pool":         "0xE284a74c661AD0ff6fC7C07e180BBbDA8ED3Eabc",
        "ui_incs":         "0x21bD81b33D4B04B94bd30C6f015484E830b68830",
        "addr_prov":       "0xD9C622d64342B5FaCeef4d366B974AEf6dCB338D",
        "dusd_underlying": DUSD_CONTRACT,
    },
    ETHEREUM_CHAIN_ID: {
        "ui_pool":         "0x1C4be7D7f0184Ba6cc458Fc99880198c537867E2",
        "ui_incs":         "0xe3Ee2d4BDe6695Cc1AE4A4cda466Bdc6d5DF479e",
        "addr_prov":       "0xa5CaE880272183d7C8B69F8B0edF395f8E42e751",
        "dusd_underlying": DUSD_CONTRACT_ETHEREUM,
    },
}
_RESERVE_TUPLE = (
    "(address,string,string,uint256,uint256,uint256,uint256,uint256,bool,bool,bool,bool,bool,"
    "uint128,uint128,uint128,uint128,uint128,uint40,address,address,address,address,uint256,"
    "uint256,uint256,uint256,uint256,uint256,address,uint256,uint256,uint256,uint256,uint256,"
    "uint256,uint256,bool,bool,uint128,uint128,uint128,bool,uint256,uint256,uint8,uint256,"
    "uint256,uint16,uint16,uint16,address,string,bool)"
)
_BASE_CURRENCY_TUPLE = "(uint256,int256,int256,uint8)"
_GET_RESERVES_DATA_OUTPUT = [f"{_RESERVE_TUPLE}[]", _BASE_CURRENCY_TUPLE]
_REWARD_TUPLE = "(string,address,address,uint256,uint256,uint256,uint256,int256,uint8,uint8,uint8)"
_INCENTIVE_DATA_TUPLE = f"(address,address,{_REWARD_TUPLE}[])"
_RESERVE_INCS_TUPLE = f"(address,{_INCENTIVE_DATA_TUPLE},{_INCENTIVE_DATA_TUPLE},{_INCENTIVE_DATA_TUPLE})"
_GET_RESERVES_INCENTIVES_OUTPUT = [f"{_RESERVE_INCS_TUPLE}[]"]
# Function selectors (first 4 bytes of keccak256 of signature).
_SEL_GET_RESERVES_DATA = "0xec489c21"        # getReservesData(address)
_SEL_GET_RESERVES_INCENTIVES = "0x976fafc5"  # getReservesIncentivesData(address)
SECONDS_PER_YEAR = 31_536_000
RAY = 10**27

# row → (symbol, contract, decimals, coingecko_id_or_None)
DLEND_ETHEREUM_EXTRA_TOKENS = {
    4:  ("sfrxUSD",   "0x979fb79D36c0D3006cDe38e992d9f51768efaAd8", 18, "staked-frax-usd"),
    5:  ("ETH",       "0xab035F35f3e9891f5756f54bc26DD4a51cD02989", 18, "weth"),
    6:  ("wstETH",    "0xDFAEe67e4EF9009A728dae88453275c616A5877f", 18, "wrapped-steth"),
    7:  ("sfrxETH",   "0x3De01b66b97EAF98603920E9e850c6d7b2411dDF", 18, "staked-frax-ether"),
    8:  ("rETH",      "0x7F90988393D1db8ef33cC9f4294A7dDA389D7cF1", 18, "rocket-pool-eth"),
    9:  ("sUSDe",     "0x2B820Fd4911876160C3988E57A10D8A5B85dFf35", 18, "ethena-staked-usde"),
    10: ("sUSDS",     "0xB33276a11CaBe6e1cD0252C4E1770FfD30a8029c", 18, "susds"),
    11: ("SyrupUSDC", "0xa5535fC58Fd1be43a37367f4b66669f691A26eae", 6,  "syrupusdc"),
    12: ("SyrupUSDT", "0xA17571a95bd22dc1a6F54d7f6E396D2398DFe493", 6,  "syrupusdt"),
    13: ("LBTC",      "0xc247736EAaa1B45D21ae1668D13965B4b50e9011", 8,  "lombard-staked-btc"),
    14: ("WBTC",      "0x88A4EeD28A1d7bCee95228721678662421A1C748", 8,  "wrapped-bitcoin"),
    15: ("cbBTC",     "0x504D0Eacbf9ea5645A8A9da1b15f3708A5483AcC", 8,  "ZERO"),  # forced $0 — user will revisit
    16: ("PAXG",      "0x8A9384b094D34db0110988D497E96B17F3B9C930", 18, "pax-gold"),
    20: ("sdUSD",     "0x7CB20517776636eD76b68EdB3D99DCce356ABf02", 18, "dtrinity-staked-dusd"),
}

def scrape_debank():
    print(f"\nScraping DeBank...")
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(DEBANK_URL, wait_until="networkidle", timeout=60000)
        time.sleep(8)

        lines = page.inner_text("body").split('\n')
        lines = [l.strip() for l in lines if l.strip()]

        # DeBank layout per token row:
        # Token Name
        # $Price
        # Amount (number)
        # $USD Value  <-- this is what we want

        target_tokens = {
            "frxusd":  ["frxusd"],
            "sfrxusd": ["sfrxusd"],
            "dai":     ["dai"],
            "sdai":    ["sdai"],
            "usdc":    ["usdc"],
            "usdt":    ["usdt"],
        }

        for i, line in enumerate(lines):
            ll = line.lower().strip()
            for key, aliases in target_tokens.items():
                if key in results:
                    continue
                if any(ll == a for a in aliases):
                    # DeBank wallet row layout: Token / $Price / Amount / $USD Value
                    # First $ in the next few lines is Price, second $ is USD Value.
                    upcoming = lines[i+1:i+5]
                    usd_values = []
                    for upcoming_line in upcoming:
                        if upcoming_line.startswith('$'):
                            val_str = upcoming_line.replace('$', '').replace(',', '').strip()
                            try:
                                val = float(val_str)
                                if val > 0:
                                    usd_values.append(val)
                            except:
                                pass
                    if len(usd_values) >= 2:
                        results[key] = usd_values[1]
                        print(f"  {key}: ${usd_values[1]:,.2f}")
                    elif usd_values:
                        results[key] = usd_values[0]
                        print(f"  {key}: ${usd_values[0]:,.2f} (only one $ found, may be wrong)")

        browser.close()

    print(f"  Results: {results}")
    return results


def scrape_protocols():
    print(f"\nScraping DeBank protocol positions...")
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(DEBANK_URL_PROTOCOLS, wait_until="networkidle", timeout=60000)
        time.sleep(8)

        lines = [l.strip() for l in page.inner_text("body").split('\n') if l.strip()]
        browser.close()

    # DeBank protocol section layout:
    # ProtocolName / $summary / Subtitle / Pool / Balance / [Rewards] / USD Value / position name / amounts / Withdraw / [rewards] / [Claim] / $detail
    # The detail value is the FIRST $-prefixed line AFTER the subtitle (Farming/Staked).
    targets = [("curve", "Curve", "Farming"), ("convex", "Convex", "Staked")]

    for key, header, subtitle in targets:
        for i, line in enumerate(lines):
            if line == header and i + 2 < len(lines) and lines[i + 2] == subtitle:
                for j in range(i + 3, min(i + 25, len(lines))):
                    if lines[j].startswith('$'):
                        val_str = lines[j].replace('$', '').replace(',', '').strip()
                        try:
                            results[key] = float(val_str)
                            print(f"  {key} ({header} {subtitle}): ${results[key]:,.2f}")
                            break
                        except ValueError:
                            continue
                break
        if key not in results:
            print(f"  Missing: {key} ({header} {subtitle})")

    return results


def fetch_dusd_supply():
    print(f"\nFetching dUSD supply...")
    r = _etherscan_get({
        "chainid": FRAXTAL_CHAIN_ID,
        "module": "stats",
        "action": "tokensupply",
        "contractaddress": DUSD_CONTRACT,
        "apikey": ETHERSCAN_API_KEY,
    })
    if r and r.get("status") == "1":
        s = int(r["result"]) / 1_000_000
        print(f"  dUSD: {s:,.6f}")
        return s
    print(f"  Error: {r}")
    return None


def _parse_katana_tokens(lines, verbose=True):
    """Pull {token: usd_value} out of DeBank's rendered body text.

    Split out of scrape_debank_katana so the parse can run against a partially
    rendered page on every poll, and so it is testable without a browser.
    """
    results = {}
    target_tokens = list(KATANA_TOKEN_ROW_MAP.keys())

    for i, line in enumerate(lines):
        ll = line.lower().strip()
        for key in target_tokens:
            if key in results:
                continue
            if ll == key:
                upcoming = lines[i+1:i+5]
                usd_values = []
                for upcoming_line in upcoming:
                    if upcoming_line.startswith('<$'):
                        # DeBank shows "<$0.01" for sub-cent positions
                        usd_values.append(0.0)
                    elif upcoming_line.startswith('$'):
                        val_str = upcoming_line.replace('$', '').replace(',', '').strip()
                        try:
                            val = float(val_str)
                            if val > 0:
                                usd_values.append(val)
                        except ValueError:
                            pass
                if len(usd_values) >= 2:
                    results[key] = usd_values[1]
                    if verbose:
                        print(f"  {key}: ${usd_values[1]:,.2f}")
                elif usd_values:
                    results[key] = usd_values[0]
                    if verbose:
                        print(f"  {key}: ${usd_values[0]:,.2f} (only one $ found, may be wrong)")

    return results


def _scrape_katana_once():
    """One browser attempt. Polls until every expected token renders, or the
    render deadline passes — whichever comes first. Returns what it found."""
    expected = set(KATANA_TOKEN_ROW_MAP.keys())
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ).new_page()
            # domcontentloaded, not networkidle: DeBank polls in the background,
            # so networkidle is not a reliable signal that the token list has
            # painted (same trap that broke the katanascan scrape in June).
            # The poll below is what actually decides when the data is there.
            page.goto(DEBANK_URL_KATANA, wait_until="domcontentloaded", timeout=60000)

            deadline = time.time() + KATANA_RENDER_TIMEOUT_SECONDS
            while True:
                lines = [l.strip() for l in page.inner_text("body").split('\n') if l.strip()]
                results = _parse_katana_tokens(lines, verbose=False)
                if expected.issubset(results):
                    break
                if time.time() >= deadline:
                    break
                time.sleep(KATANA_RENDER_POLL_SECONDS)
        finally:
            browser.close()

    # Re-parse verbosely so the log keeps its familiar per-token lines.
    for key, val in results.items():
        print(f"  {key}: ${val:,.2f}")
    return results


def scrape_debank_katana():
    print(f"\nScraping DeBank (Katana wallet)...")
    expected = set(KATANA_TOKEN_ROW_MAP.keys())
    results = {}

    for attempt in range(1, KATANA_SCRAPE_ATTEMPTS + 1):
        try:
            results = _scrape_katana_once()
        except Exception as e:
            print(f"  Attempt {attempt}/{KATANA_SCRAPE_ATTEMPTS} errored: {type(e).__name__}: {e}")
            results = {}

        if expected.issubset(results):
            print(f"  Results: {results}")
            return results

        missing = sorted(expected - set(results))
        print(f"  Attempt {attempt}/{KATANA_SCRAPE_ATTEMPTS}: got {len(results)}/{len(expected)} tokens, missing {missing}")
        if attempt < KATANA_SCRAPE_ATTEMPTS:
            time.sleep(KATANA_SCRAPE_BACKOFF_SECONDS)

    # Out of attempts. A partial scrape is still worth writing — verify_today()
    # will flag whatever rows stayed blank — but an empty/near-empty one must
    # raise so the first pass reports the failure instead of printing [OK].
    if len(results) >= KATANA_MIN_TOKENS:
        print(f"  WARNING: proceeding with partial scrape ({len(results)}/{len(expected)} tokens): {results}")
        return results

    raise RuntimeError(
        f"DeBank Katana scrape found only {len(results)}/{len(expected)} tokens "
        f"after {KATANA_SCRAPE_ATTEMPTS} attempts (need >= {KATANA_MIN_TOKENS}); "
        f"missing {sorted(expected - set(results))}"
    )


def fetch_dusd_supply_katana():
    # Was a Playwright scrape of katanascan.com's "Max Total Supply", which flaked
    # constantly (row 9 was the chronic verify miss that turned every run red).
    # Etherscan v2 supports Katana (chainid 747474), so use the same API path as
    # every other chain — reliable and no headless browser.
    print(f"\nFetching dUSD supply (Katana)...")
    return fetch_token_supply_chain(KATANA_CHAIN_ID, KATANA_DUSD_CONTRACT, 18, "dUSD (Katana)")


def write_to_katana_sheet(katana_data, katana_dusd_supply):
    print(f"\nWriting to Katana sheet...")
    creds = _load_google_credentials()
    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(KATANA_SHEET_TAB)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    row2 = sheet.row_values(2)
    col = next((i+1 for i, c in enumerate(row2) if str(c).strip() == today_str), len(row2)+1)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    print(f"  Column: {col_letter} (index {col}) for {today_str}")

    if col > sheet.col_count:
        sheet.add_cols(col - sheet.col_count)
        print(f"  Expanded sheet to {col} columns")

    updates = []
    if not sheet.cell(2, col).value:
        updates.append({"range": gspread.utils.rowcol_to_a1(2, col), "values": [[today_str]]})

    for k, r in KATANA_TOKEN_ROW_MAP.items():
        if k in katana_data:
            updates.append({"range": gspread.utils.rowcol_to_a1(r, col), "values": [[katana_data[k]]]})
            print(f"  Row {r} ({k}): ${katana_data[k]:,.2f}")
        else:
            print(f"  Missing: {k}")

    if katana_dusd_supply is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(KATANA_DUSD_ROW, col), "values": [[katana_dusd_supply]]})
        print(f"  Row {KATANA_DUSD_ROW} (dUSD): {katana_dusd_supply:,.6f}")

    # Formulas: Row 11 = SUM(3:8), Row 12 = SUM(9), Row 14 = 11/12
    updates.append({"range": gspread.utils.rowcol_to_a1(11, col), "values": [[f"=SUM({col_letter}3:{col_letter}8)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(12, col), "values": [[f"=SUM({col_letter}9)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(14, col), "values": [[f"={col_letter}11/{col_letter}12"]]})

    if updates:
        sheet.batch_update(updates, value_input_option='USER_ENTERED')
        # Copy formatting (borders, $/% number formats, etc.) from template col B
        sheet.spreadsheet.batch_update({'requests': [{
            'copyPaste': {
                'source':      {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 20, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'destination': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 20, 'startColumnIndex': col - 1, 'endColumnIndex': col},
                'pasteType': 'PASTE_FORMAT', 'pasteOrientation': 'NORMAL',
            }
        }]})
        sheet.format(f"{col_letter}14", {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}})
        print(f"  Wrote {len(updates)} values + format copy + % format on {col_letter}14")
    else:
        print("  Nothing to write.")


def scrape_debank_ethereum():
    print(f"\nScraping DeBank (Ethereum wallet)...")
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(DEBANK_URL_ETHEREUM, wait_until="networkidle", timeout=60000)
        time.sleep(8)
        lines = [l.strip() for l in page.inner_text("body").split('\n') if l.strip()]
        browser.close()

    # Raw wallet tokens (Token / $Price / Amount / $USD pattern)
    raw_keys = list(ETHEREUM_RAW_TOKEN_ROW_MAP.keys())
    for i, line in enumerate(lines):
        ll = line.lower().strip()
        for key in raw_keys:
            if key in results:
                continue
            if ll == key:
                upcoming = lines[i+1:i+5]
                usd_values = []
                for upcoming_line in upcoming:
                    if upcoming_line.startswith('<$'):
                        usd_values.append(0.0)
                    elif upcoming_line.startswith('$'):
                        val_str = upcoming_line.replace('$', '').replace(',', '').strip()
                        try:
                            val = float(val_str)
                            if val > 0:
                                usd_values.append(val)
                        except ValueError:
                            pass
                if len(usd_values) >= 2:
                    results[key] = usd_values[1]
                    print(f"  {key}: ${usd_values[1]:,.2f}")
                elif usd_values:
                    results[key] = usd_values[0]
                    print(f"  {key}: ${usd_values[0]:,.2f} (only one $ found)")

    # Protocol positions (Frax Staked sfrxUSD, Sky Yield Savings USDS)
    for key, _row, header, subtitle in ETHEREUM_PROTOCOL_POSITIONS:
        for i, line in enumerate(lines):
            if line == header and i + 2 < len(lines) and lines[i + 2] == subtitle:
                for j in range(i + 3, min(i + 25, len(lines))):
                    if lines[j].startswith('$'):
                        val_str = lines[j].replace('$', '').replace(',', '').strip()
                        try:
                            results[key] = float(val_str)
                            print(f"  {key} ({header} {subtitle}): ${results[key]:,.2f}")
                            break
                        except ValueError:
                            continue
                break
        if key not in results:
            print(f"  Missing: {key} ({header} {subtitle})")

    print(f"  Results: {results}")
    return results


def scrape_amo_ethereum():
    print(f"\nScraping DeBank (AMO wallet, Ethereum)...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(DEBANK_URL_AMO_ETHEREUM, wait_until="networkidle", timeout=60000)
        time.sleep(8)
        lines = [l.strip() for l in page.inner_text("body").split('\n') if l.strip()]
        browser.close()

    for i, line in enumerate(lines):
        if line == "Curve" and i + 2 < len(lines) and lines[i + 2] == "Farming":
            for j in range(i + 3, min(i + 25, len(lines))):
                if lines[j].startswith('$'):
                    val_str = lines[j].replace('$', '').replace(',', '').strip()
                    try:
                        val = float(val_str)
                        print(f"  AMO Curve LP: ${val:,.2f}")
                        return val
                    except ValueError:
                        continue
            break

    print(f"  Error: Curve Farming position not found")
    return None


def fetch_dusd_supply_ethereum():
    print(f"\nFetching dUSD supply (Ethereum)...")
    r = _etherscan_get({
        "chainid": ETHEREUM_CHAIN_ID,
        "module": "stats",
        "action": "tokensupply",
        "contractaddress": DUSD_CONTRACT_ETHEREUM,
        "apikey": ETHERSCAN_API_KEY,
    })
    if r and r.get("status") == "1":
        raw = int(r["result"])
        # Try 18 decimals first (most ERC-20 stablecoins on Ethereum), check magnitude
        s_18 = raw / 10**18
        s_6 = raw / 10**6
        # Pick the one that lands in a reasonable token-supply range (1k–1B)
        s = s_18 if 1_000 <= s_18 <= 1_000_000_000 else s_6
        print(f"  dUSD (Ethereum): {s:,.6f}  (raw={raw}, 18dec={s_18:,.6f}, 6dec={s_6:,.6f})")
        return s
    print(f"  Error: {r}")
    return None


def fetch_token_supply_fraxtal(contract, decimals, label):
    """Fetch raw token supply from Etherscan v2 (Fraxtal chain), divide by 10**decimals."""
    r = _etherscan_get({
        "chainid": FRAXTAL_CHAIN_ID,
        "module": "stats",
        "action": "tokensupply",
        "contractaddress": contract,
        "apikey": ETHERSCAN_API_KEY,
    })
    if r and r.get("status") == "1":
        raw = int(r["result"])
        scaled = raw / 10**decimals
        print(f"  {label}: {scaled:,.6f} (decimals={decimals})")
        return scaled
    print(f"  Error fetching {label}: {r}")
    return None


def fetch_frxeth_price():
    print(f"\nFetching frxETH price (CoinGecko)...")
    try:
        r = requests.get(COINGECKO_FRXETH_URL, timeout=15).json()
        price = r.get("frax-ether", {}).get("usd")
        if price:
            print(f"  frxETH price: ${price:,.2f}")
            return float(price)
        print(f"  Error: no price in response: {r}")
    except Exception as e:
        print(f"  Error: {e}")
    return None


def fetch_coingecko_prices(ids):
    """Batch-fetch USD prices from CoinGecko. Returns dict {id: price}."""
    if not ids:
        return {}
    print(f"\nFetching CoinGecko prices for: {ids}")
    try:
        r = requests.get(COINGECKO_PRICE_URL, params={"ids": ",".join(ids), "vs_currencies": "usd"}, timeout=20).json()
        prices = {k: v.get("usd") for k, v in r.items() if v.get("usd") is not None}
        for k in ids:
            if k in prices:
                print(f"  {k}: ${prices[k]}")
            else:
                print(f"  {k}: MISSING (will default to $1.00)")
        return prices
    except Exception as e:
        print(f"  Error: {e}")
        return {}


def fetch_fxb_prices_via_ytm():
    """Scrape facts.frax.finance/fxb table for YTM, compute price = 1/(1+YTM)^t per maturity."""
    print(f"\nFetching FXB YTMs from {FRAX_FACTS_FXB_URL}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(FRAX_FACTS_FXB_URL, wait_until="networkidle", timeout=60000)
        time.sleep(8)
        lines = [l.strip() for l in page.inner_text("body").split('\n') if l.strip()]
        browser.close()

    # Table rows: "FXB YYYY-MM-DD" → next line is maturity duration → next is YTM → next is supply
    today = datetime.now()
    prices = {}  # key: "YYYY-MM-DD" → price
    for i, line in enumerate(lines):
        if line.startswith("FXB ") and len(line) >= 14 and "-" in line:
            mat_str = line[4:14]  # "YYYY-MM-DD"
            try:
                mat_date = datetime.strptime(mat_str, "%Y-%m-%d")
            except ValueError:
                continue
            # YTM is 2 lines after the FXB line (skipping maturity-in line)
            if i + 2 < len(lines) and lines[i + 2].endswith('%'):
                try:
                    ytm = float(lines[i + 2].rstrip('%')) / 100.0
                except ValueError:
                    continue
                t_years = max((mat_date - today).days / 365.25, 0)
                if t_years <= 0:
                    price = 1.0  # matured
                else:
                    price = 1.0 / ((1 + ytm) ** t_years)
                prices[mat_str] = price
                print(f"  FXB {mat_str}: YTM={ytm:.4f}, t={t_years:.2f}yr → price=${price:.4f}")
    return prices


def fetch_dlend_fraxtal():
    print(f"\nFetching dLEND token supplies (Fraxtal)...")

    # Existing rows
    dusd  = fetch_token_supply_fraxtal(DLEND_DUSD_CONTRACT,   6,  "dLEND dUSD supply  (row 4)")
    sdusd = fetch_token_supply_fraxtal(DLEND_SDUSD_CONTRACT,  6,  "dLEND sdUSD supply (row 19)")

    # Extra rows — fetch all supplies first
    supplies = {}  # row → (symbol, supply, decimals, coingecko_id_or_FXB)
    for row, (sym, contract, decimals, cg_id) in DLEND_EXTRA_TOKENS.items():
        supply = fetch_token_supply_fraxtal(contract, decimals, f"  row {row} {sym}")
        supplies[row] = (sym, supply, cg_id)

    # Batch-fetch CoinGecko prices for non-FXB tokens (plus frax-ether for row 6)
    cg_ids = sorted({cg_id for (_, _, cg_id) in supplies.values() if cg_id and not cg_id.startswith("FXB:")} | {"frax-ether"})
    cg_prices = fetch_coingecko_prices(cg_ids)

    # Fetch FXB prices via YTM scrape
    fxb_prices = fetch_fxb_prices_via_ytm() if any(cg_id and cg_id.startswith("FXB:") for (_, _, cg_id) in supplies.values()) else {}

    # Compute USD value for each row
    usd_values = {}
    for row, (sym, supply, cg_id) in supplies.items():
        if supply is None:
            usd_values[row] = None
            continue
        if cg_id is None:
            price = 1.0
            print(f"  Row {row} {sym}: ${supply:,.2f} × $1.00 (default) = ${supply:,.2f}")
        elif cg_id.startswith("FXB:"):
            mat = cg_id.split(":", 1)[1]
            price = fxb_prices.get(mat, 1.0)  # fallback $1 face value if missing (e.g., matured FXB 2025)
            print(f"  Row {row} {sym}: {supply:,.4f} × ${price:.4f} = ${supply * price:,.2f}")
        else:
            price = cg_prices.get(cg_id, 1.0)
            print(f"  Row {row} {sym}: {supply:,.4f} × ${price} = ${supply * price:,.2f}")
        usd_values[row] = supply * price

    # frxETH (row 6) — special case: CoinGecko price for frax-ether
    frxeth_supply = fetch_token_supply_fraxtal(DLEND_FRXETH_CONTRACT, 18, "  row 6 frxETH (tokens)")
    frxeth_price = cg_prices.get("frax-ether") or fetch_frxeth_price()
    frxeth_usd = (frxeth_supply * frxeth_price) if (frxeth_supply is not None and frxeth_price) else None
    if frxeth_usd is not None:
        print(f"  Row 6 frxETH: {frxeth_supply:,.6f} × ${frxeth_price:,.2f} = ${frxeth_usd:,.2f}")

    try:
        apys = fetch_dlend_dusd_apys(FRAXTAL_CHAIN_ID)
    except Exception as e:
        print(f"  [APY fetch failed] {type(e).__name__}: {e}")
        apys = None

    return {
        "dusd": dusd,
        "frxeth_usd": frxeth_usd,
        "sdusd": sdusd,
        "extra_rows": usd_values,
        "apys": apys,
    }


def fetch_token_supply_chain(chain_id, contract, decimals, label):
    """Same as fetch_token_supply_fraxtal but parameterized chain."""
    r = _etherscan_get({
        "chainid": chain_id,
        "module": "stats",
        "action": "tokensupply",
        "contractaddress": contract,
        "apikey": ETHERSCAN_API_KEY,
    })
    if r and r.get("status") == "1":
        raw = int(r["result"])
        scaled = raw / 10**decimals
        print(f"  {label}: {scaled:,.6f} (decimals={decimals})")
        return scaled
    print(f"  Error fetching {label}: {r}")
    return None


def fetch_dlend_ethereum():
    print(f"\nFetching dLEND token supplies (Ethereum)...")
    dusd      = fetch_token_supply_chain(ETHEREUM_CHAIN_ID, DLEND_ETHEREUM_DUSD_CONTRACT,      18, "dLEND dUSD supply (row 3)")
    dusd_debt = fetch_token_supply_chain(ETHEREUM_CHAIN_ID, DLEND_ETHEREUM_DUSD_DEBT_CONTRACT, 18, "dLEND dUSD debt   (row 19)")

    supplies = {}
    for row, (sym, contract, decimals, cg_id) in DLEND_ETHEREUM_EXTRA_TOKENS.items():
        supply = fetch_token_supply_chain(ETHEREUM_CHAIN_ID, contract, decimals, f"  row {row} {sym}")
        supplies[row] = (sym, supply, cg_id)

    cg_ids = sorted({cg_id for (_, _, cg_id) in supplies.values() if cg_id and cg_id != "ZERO"})
    cg_prices = fetch_coingecko_prices(cg_ids)

    usd_values = {}
    for row, (sym, supply, cg_id) in supplies.items():
        # ZERO override applies regardless of supply fetch success — row is always $0
        if cg_id == "ZERO":
            usd_values[row] = 0.0
            print(f"  Row {row} {sym}: forced $0.00")
            continue
        if supply is None:
            usd_values[row] = None
            continue
        price = cg_prices.get(cg_id, 1.0) if cg_id else 1.0
        usd_values[row] = supply * price
        print(f"  Row {row} {sym}: {supply:,.6f} × ${price} = ${usd_values[row]:,.2f}")

    try:
        apys = fetch_dlend_dusd_apys(ETHEREUM_CHAIN_ID)
    except Exception as e:
        print(f"  [APY fetch failed] {type(e).__name__}: {e}")
        apys = None

    return {"dusd": dusd, "dusd_debt": dusd_debt, "extra_rows": usd_values, "apys": apys}


def _dlend_eth_call(chain_id, to, data_hex):
    """eth_call via Etherscan v2 proxy. Returns the result hex (no 0x prefix)."""
    r = _etherscan_get({
        "chainid": chain_id,
        "module": "proxy",
        "action": "eth_call",
        "to": to,
        "data": data_hex,
        "tag": "latest",
        "apikey": ETHERSCAN_API_KEY,
    })
    if not r or "result" not in r:
        raise RuntimeError(f"eth_call {to} failed: {r}")
    res = r["result"]
    if isinstance(res, str) and res.startswith("0x"):
        return res[2:]
    raise RuntimeError(f"eth_call {to} returned unexpected: {res!r}")


def _encode_address_arg(addr):
    """ABI-encode a single address argument as a 32-byte hex string."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def _compound_apr_to_apy(rate_ray):
    """Per-Aave-math-utils: APY = (1 + APR/SECONDS_PER_YEAR)^SECONDS_PER_YEAR - 1."""
    apr = rate_ray / RAY
    return (1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1


def _format_pct(decimal_value):
    return f"{decimal_value * 100:.2f}%"


def fetch_dlend_dusd_apys(chain_id):
    """Return four pre-formatted percentage strings for the dUSD reserve:
    supply_apy, gross_borrow_apy, rebate_apy, net_borrow_apy.
    Mirrors BorrowInfo.tsx + DStableBorrowAPYTooltip.tsx from dtrinity/interface.
    """
    cfg = DLEND_APY_ADDRESSES[chain_id]
    arg = _encode_address_arg(cfg["addr_prov"])
    dusd_addr = cfg["dusd_underlying"].lower()
    print(f"\nFetching dLEND dUSD APYs (chain {chain_id})...")

    pool_hex = _dlend_eth_call(chain_id, cfg["ui_pool"], _SEL_GET_RESERVES_DATA + arg)
    reserves, base = _abi_decode(_GET_RESERVES_DATA_OUTPUT, bytes.fromhex(pool_hex))
    market_unit, _market_price_usd, _net_price, _net_dec = base

    dusd = next((r for r in reserves if r[0].lower() == dusd_addr), None)
    if dusd is None:
        raise RuntimeError(f"chain {chain_id}: dUSD ({dusd_addr}) not in getReservesData output")
    decimals              = dusd[3]
    variable_borrow_index = dusd[14]
    liquidity_rate        = dusd[15]
    variable_borrow_rate  = dusd[16]
    total_scaled_var_debt = dusd[27]
    price_in_ref          = dusd[28]

    supply_apy = _compound_apr_to_apy(liquidity_rate)
    gross_borrow_apy = _compound_apr_to_apy(variable_borrow_rate)

    incs_hex = _dlend_eth_call(chain_id, cfg["ui_incs"], _SEL_GET_RESERVES_INCENTIVES + arg)
    (reserves_incs,) = _abi_decode(_GET_RESERVES_INCENTIVES_OUTPUT, bytes.fromhex(incs_hex))
    dusd_incs = next((r for r in reserves_incs if r[0].lower() == dusd_addr), None)

    rebate_apr = 0.0
    if dusd_incs is not None:
        _v_token, _v_ctrl, rewards = dusd_incs[2]   # vIncentiveData (variable-debt rebate)
        total_debt_tokens = (total_scaled_var_debt * variable_borrow_index) / RAY / (10 ** decimals)
        dusd_price_usd = (price_in_ref / market_unit) if market_unit else 0.0
        total_debt_usd = total_debt_tokens * dusd_price_usd
        now = int(time.time())
        if total_debt_usd > 0:
            for rw in rewards:
                emission_per_second = rw[3]
                emission_end_ts     = rw[6]
                reward_price_feed   = rw[7]
                reward_decimals     = rw[8]
                price_feed_decimals = rw[10]
                if emission_end_ts <= now or reward_price_feed <= 0:
                    continue
                emission_tokens_per_year = emission_per_second * SECONDS_PER_YEAR / (10 ** reward_decimals)
                reward_price_usd = reward_price_feed / (10 ** price_feed_decimals)
                rebate_apr += (emission_tokens_per_year * reward_price_usd) / total_debt_usd

    net_borrow_apy = gross_borrow_apy - rebate_apr
    print(f"  Supply APY:  {_format_pct(supply_apy)}")
    print(f"  Gross Borrow APY: {_format_pct(gross_borrow_apy)}")
    print(f"  Rebate APY:  {_format_pct(rebate_apr)}")
    print(f"  Net Borrow APY:   {_format_pct(net_borrow_apy)}")
    return {
        "supply_apy":       _format_pct(supply_apy),
        "gross_borrow_apy": _format_pct(gross_borrow_apy),
        "rebate_apy":       _format_pct(rebate_apr),
        "net_borrow_apy":   _format_pct(net_borrow_apy),
    }


def write_to_dlend_ethereum_sheet(data):
    print(f"\nWriting to dLEND Stats (Ethereum) sheet...")
    creds = _load_google_credentials()
    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(DLEND_ETHEREUM_SHEET_TAB)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    row_dates = sheet.row_values(DLEND_ETHEREUM_DATE_ROW)
    col = next((i+1 for i, c in enumerate(row_dates) if str(c).strip() == today_str), len(row_dates)+1)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    print(f"  Column: {col_letter} (index {col}) for {today_str}")

    if col > sheet.col_count:
        sheet.add_cols(col - sheet.col_count)
        print(f"  Expanded sheet to {col} columns")

    updates = []
    if not sheet.cell(DLEND_ETHEREUM_DATE_ROW, col).value:
        updates.append({"range": gspread.utils.rowcol_to_a1(DLEND_ETHEREUM_DATE_ROW, col), "values": [[today_str]]})

    if data.get("dusd") is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(3, col), "values": [[round(data["dusd"], 2)]]})
        print(f"  Row 3 (dUSD Supply): ${data['dusd']:,.2f}")

    for row, usd in (data.get("extra_rows") or {}).items():
        if usd is not None:
            updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[round(usd, 2)]]})
            print(f"  Row {row}: ${usd:,.2f}")

    if data.get("dusd_debt") is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(19, col), "values": [[round(data["dusd_debt"], 2)]]})
        print(f"  Row 19 (dUSD Debt): ${data['dusd_debt']:,.2f}")

    apys = data.get("apys")
    if apys:
        for row, key, label in [
            (22, "supply_apy",       "Supply APY"),
            (23, "gross_borrow_apy", "Gross Borrow APY"),
            (24, "rebate_apy",       "Rebate APY"),
            (25, "net_borrow_apy",   "Net Borrow APY"),
        ]:
            updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[apys[key]]]})
            print(f"  Row {row} ({label}): {apys[key]}")
    else:
        print(f"  Missing: APYs (rows 22-25)")

    # Formulas matching historical pattern: SUM(B4:B16), SUM(B3:B16), B19/B3, B19/B18
    updates.append({"range": gspread.utils.rowcol_to_a1(18, col), "values": [[f"=SUM({col_letter}4:{col_letter}16)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(27, col), "values": [[f"=SUM({col_letter}3:{col_letter}16)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(28, col), "values": [[f"={col_letter}19/{col_letter}3"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(29, col), "values": [[f"={col_letter}19/{col_letter}18"]]})

    if updates:
        sheet.batch_update(updates, value_input_option='USER_ENTERED')
        # Copy formatting from template col B
        sheet.spreadsheet.batch_update({'requests': [{
            'copyPaste': {
                'source':      {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 30, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'destination': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 30, 'startColumnIndex': col - 1, 'endColumnIndex': col},
                'pasteType': 'PASTE_FORMAT', 'pasteOrientation': 'NORMAL',
            }
        }]})
        print(f"  Wrote {len(updates)} values + format copy from col B")
    else:
        print("  Nothing to write.")


def write_to_dlend_fraxtal_sheet(data):
    print(f"\nWriting to dLEND Stats (Fraxtal) sheet...")
    creds = _load_google_credentials()
    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(DLEND_FRAXTAL_SHEET_TAB)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    row3 = sheet.row_values(3)
    col = next((i+1 for i, c in enumerate(row3) if str(c).strip() == today_str), len(row3)+1)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    print(f"  Column: {col_letter} (index {col}) for {today_str}")

    if col > sheet.col_count:
        sheet.add_cols(col - sheet.col_count)
        print(f"  Expanded sheet to {col} columns")

    updates = []
    if not sheet.cell(3, col).value:
        updates.append({"range": gspread.utils.rowcol_to_a1(3, col), "values": [[today_str]]})

    if data.get("dusd") is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(4, col), "values": [[round(data["dusd"], 2)]]})
        print(f"  Row 4 (dUSD Supply): ${data['dusd']:,.2f}")
    else:
        print(f"  Missing: dUSD")

    if data.get("frxeth_usd") is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(6, col), "values": [[round(data["frxeth_usd"], 2)]]})
        print(f"  Row 6 (frxETH Supply, USD): ${data['frxeth_usd']:,.2f}")
    else:
        print(f"  Missing: frxETH")

    if data.get("sdusd") is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(19, col), "values": [[round(data["sdusd"], 2)]]})
        print(f"  Row 19 (sdUSD Supply): ${data['sdusd']:,.2f}")
    else:
        print(f"  Missing: sdUSD")

    # Extra rows (5, 7-16) — USD values from supply × price
    for row, usd in (data.get("extra_rows") or {}).items():
        if usd is not None:
            updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[round(usd, 2)]]})
            print(f"  Row {row}: ${usd:,.2f}")
        else:
            print(f"  Missing: row {row}")

    apys = data.get("apys")
    if apys:
        for row, key, label in [
            (22, "supply_apy",       "Supply APY"),
            (23, "gross_borrow_apy", "Gross Borrow APY"),
            (24, "rebate_apy",       "Rebate APY"),
            (25, "net_borrow_apy",   "Net Borrow APY"),
        ]:
            updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[apys[key]]]})
            print(f"  Row {row} ({label}): {apys[key]}")
    else:
        print(f"  Missing: APYs (rows 22-25)")

    # Formulas matching historical pattern
    updates.append({"range": gspread.utils.rowcol_to_a1(18, col), "values": [[f"=SUM({col_letter}5:{col_letter}8)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(27, col), "values": [[f"=SUM({col_letter}4:{col_letter}16)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(28, col), "values": [[f"={col_letter}19/{col_letter}4"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(29, col), "values": [[f"={col_letter}19/{col_letter}18"]]})

    if updates:
        sheet.batch_update(updates, value_input_option='USER_ENTERED')
        # Copy formatting (borders, $/% formats, etc.) from template col B
        sheet.spreadsheet.batch_update({'requests': [{
            'copyPaste': {
                'source':      {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 30, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'destination': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 30, 'startColumnIndex': col - 1, 'endColumnIndex': col},
                'pasteType': 'PASTE_FORMAT', 'pasteOrientation': 'NORMAL',
            }
        }]})
        print(f"  Wrote {len(updates)} values + format copy from col B")
    else:
        print("  Nothing to write.")


def write_to_ethereum_sheet(eth_data, eth_dusd_supply, amo_value):
    print(f"\nWriting to Ethereum sheet...")
    creds = _load_google_credentials()
    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(ETHEREUM_SHEET_TAB)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    row3 = sheet.row_values(3)
    col = next((i+1 for i, c in enumerate(row3) if str(c).strip() == today_str), len(row3)+1)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    print(f"  Column: {col_letter} (index {col}) for {today_str}")

    if col > sheet.col_count:
        sheet.add_cols(col - sheet.col_count)
        print(f"  Expanded sheet to {col} columns")

    updates = []
    if not sheet.cell(3, col).value:
        updates.append({"range": gspread.utils.rowcol_to_a1(3, col), "values": [[today_str]]})

    # Raw tokens
    for k, r in ETHEREUM_RAW_TOKEN_ROW_MAP.items():
        if k in eth_data:
            updates.append({"range": gspread.utils.rowcol_to_a1(r, col), "values": [[eth_data[k]]]})
            print(f"  Row {r} ({k}): ${eth_data[k]:,.2f}")
        else:
            print(f"  Missing: {k}")

    # Protocol positions
    for key, r, _h, _s in ETHEREUM_PROTOCOL_POSITIONS:
        if key in eth_data:
            updates.append({"range": gspread.utils.rowcol_to_a1(r, col), "values": [[eth_data[key]]]})
            print(f"  Row {r} ({key}): ${eth_data[key]:,.2f}")
        else:
            print(f"  Missing: {key}")

    # AMO dUSD (row 10)
    if amo_value is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(ETHEREUM_AMO_ROW, col), "values": [[amo_value]]})
        print(f"  Row {ETHEREUM_AMO_ROW} (AMO Curve LP): ${amo_value:,.2f}")
    else:
        print(f"  Missing: AMO")

    # dUSD supply (row 11)
    if eth_dusd_supply is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(ETHEREUM_DUSD_ROW, col), "values": [[eth_dusd_supply]]})
        print(f"  Row {ETHEREUM_DUSD_ROW} (dUSD): ${eth_dusd_supply:,.2f}")
    else:
        print(f"  Missing: dUSD supply")

    # Formulas: Row 13 = SUM(4:10), Row 14 = Row 11, Row 16 = Row 13 / Row 14
    updates.append({"range": gspread.utils.rowcol_to_a1(13, col), "values": [[f"=SUM({col_letter}4:{col_letter}10)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(14, col), "values": [[f"={col_letter}11"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(16, col), "values": [[f"={col_letter}13/{col_letter}14"]]})

    if updates:
        sheet.batch_update(updates, value_input_option='USER_ENTERED')
        # Copy formatting (borders, $/% number formats, etc.) from template col C
        sheet.spreadsheet.batch_update({'requests': [{
            'copyPaste': {
                'source':      {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 20, 'startColumnIndex': 2, 'endColumnIndex': 3},
                'destination': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 20, 'startColumnIndex': col - 1, 'endColumnIndex': col},
                'pasteType': 'PASTE_FORMAT', 'pasteOrientation': 'NORMAL',
            }
        }]})
        sheet.format(f"{col_letter}16", {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}})
        print(f"  Wrote {len(updates)} values + format copy + % format on {col_letter}16")
    else:
        print("  Nothing to write.")


def write_to_sheet(debank_data, dusd_supply, protocol_data):
    print(f"\nWriting to Google Sheet...")
    creds = _load_google_credentials()
    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(SHEET_TAB_NAME)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    row3 = sheet.row_values(3)
    col = next((i+1 for i, c in enumerate(row3) if str(c).strip() == today_str), len(row3)+1)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    print(f"  Column: {col_letter} (index {col}) for {today_str}")

    if col > sheet.col_count:
        sheet.add_cols(col - sheet.col_count)
        print(f"  Expanded sheet to {col} columns")

    updates = []
    if not sheet.cell(3, col).value:
        updates.append({"range": gspread.utils.rowcol_to_a1(3, col), "values": [[today_str]]})

    for k, r in TOKEN_ROW_MAP.items():
        if k in debank_data:
            updates.append({"range": gspread.utils.rowcol_to_a1(r, col), "values": [[debank_data[k]]]})
            print(f"  Row {r} ({k}): ${debank_data[k]:,.2f}")
        else:
            print(f"  Missing: {k}")

    if "convex" in protocol_data:
        updates.append({"range": gspread.utils.rowcol_to_a1(CONVEX_ROW, col), "values": [[protocol_data["convex"]]]})
        print(f"  Row {CONVEX_ROW} (Convex dUSD+sfrxUSD): ${protocol_data['convex']:,.2f}")
    else:
        print(f"  Missing: convex")

    if "curve" in protocol_data:
        updates.append({"range": gspread.utils.rowcol_to_a1(CURVE_ROW, col), "values": [[protocol_data["curve"]]]})
        print(f"  Row {CURVE_ROW} (Curve dUSD/sFRAX): ${protocol_data['curve']:,.2f}")
    else:
        print(f"  Missing: curve")

    if dusd_supply:
        updates.append({"range": gspread.utils.rowcol_to_a1(DUSD_ROW, col), "values": [[dusd_supply]]})
        print(f"  Row {DUSD_ROW} (dUSD): {dusd_supply:,.6f}")

    # Formulas: Row 18 = SUM(4:15), Row 19 = Row 16, Row 21 = Row 18 / Row 19
    updates.append({"range": gspread.utils.rowcol_to_a1(18, col), "values": [[f"=SUM({col_letter}4:{col_letter}15)"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(19, col), "values": [[f"={col_letter}16"]]})
    updates.append({"range": gspread.utils.rowcol_to_a1(21, col), "values": [[f"={col_letter}18/{col_letter}19"]]})

    if updates:
        sheet.batch_update(updates, value_input_option='USER_ENTERED')
        # Copy formatting (borders, $/% number formats, etc.) from template col C
        sheet.spreadsheet.batch_update({'requests': [{
            'copyPaste': {
                'source':      {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 25, 'startColumnIndex': 2, 'endColumnIndex': 3},
                'destination': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 25, 'startColumnIndex': col - 1, 'endColumnIndex': col},
                'pasteType': 'PASTE_FORMAT', 'pasteOrientation': 'NORMAL',
            }
        }]})
        sheet.format(f"{col_letter}21", {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}})
        print(f"  Wrote {len(updates)} values + format copy + % format on {col_letter}21")
    else:
        print("  Nothing to write.")


def _run_fraxtal_balance():
    write_to_sheet(scrape_debank(), fetch_dusd_supply(), scrape_protocols())

def _run_katana_balance():
    write_to_katana_sheet(scrape_debank_katana(), fetch_dusd_supply_katana())

def _run_ethereum_balance():
    write_to_ethereum_sheet(scrape_debank_ethereum(), fetch_dusd_supply_ethereum(), scrape_amo_ethereum())

def _run_dlend_fraxtal():
    write_to_dlend_fraxtal_sheet(fetch_dlend_fraxtal())

def _run_dlend_ethereum():
    write_to_dlend_ethereum_sheet(fetch_dlend_ethereum())


# Manifest of rows that MUST contain a value in today's column on each sheet.
# Formula rows (sums/ratios) are excluded — they auto-compute from the data rows.
EXPECTED_SHEET_ROWS = {
    SHEET_TAB_NAME:              {"date_row": 3, "rows": [4, 5, 6, 7, 8, 9, 10, 11, 16],                                  "rerun": _run_fraxtal_balance},
    KATANA_SHEET_TAB:            {"date_row": 2, "rows": [3, 4, 5, 6, 7, 8, 9],                                           "rerun": _run_katana_balance},
    ETHEREUM_SHEET_TAB:          {"date_row": 3, "rows": [4, 5, 6, 7, 8, 9, 10, 11],                                      "rerun": _run_ethereum_balance},
    DLEND_FRAXTAL_SHEET_TAB:     {"date_row": 3, "rows": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 20, 22, 23, 24, 25],          "rerun": _run_dlend_fraxtal},
    DLEND_ETHEREUM_SHEET_TAB:    {"date_row": 2, "rows": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 20, 22, 23, 24, 25],       "rerun": _run_dlend_ethereum},
}


def _is_blank_cell(v):
    """A cell counts as missing only if truly empty. '0', '0.00', '$0.00' all count as present."""
    if v is None:
        return True
    s = str(v).strip()
    return s == ""


def verify_today(sheet_filter=None):
    """Read each sheet's today column, return {tab_name: [missing_row_numbers]}.

    sheet_filter: optional iterable of tab names to limit which sheets to check.
    Empty dict means every expected row has a value.
    """
    creds = _load_google_credentials()
    spreadsheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    today = datetime.now()
    today_str = f"{today.month}/{today.day}/{today.year}"
    misses = {}

    targets = sheet_filter if sheet_filter is not None else EXPECTED_SHEET_ROWS.keys()
    for tab in targets:
        conf = EXPECTED_SHEET_ROWS[tab]
        sheet = spreadsheet.worksheet(tab)
        date_row_vals = sheet.row_values(conf["date_row"])
        col = next((i + 1 for i, c in enumerate(date_row_vals) if str(c).strip() == today_str), None)
        if col is None:
            # No column for today at all → every expected row is missing
            misses[tab] = list(conf["rows"])
            print(f"  [verify] {tab}: today's column ({today_str}) not found → all {len(conf['rows'])} rows missing")
            continue

        col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
        first, last = min(conf["rows"]), max(conf["rows"])
        rng = f"{col_letter}{first}:{col_letter}{last}"
        # sheet.get returns a list-of-lists; rows beyond data are absent
        values = sheet.get(rng)
        # Build a {row: value} map for the requested column slice
        cell_by_row = {}
        for offset, vrow in enumerate(values):
            cell_by_row[first + offset] = vrow[0] if vrow else ""

        sheet_misses = [r for r in conf["rows"] if _is_blank_cell(cell_by_row.get(r))]
        if sheet_misses:
            misses[tab] = sheet_misses
            print(f"  [verify] {tab}: missing rows {sheet_misses}")
        else:
            print(f"  [verify] {tab}: all {len(conf['rows'])} rows present")

    return misses


MAX_VERIFY_ATTEMPTS = 2
# Sleep this many seconds before each retry so transient rate-limits / DeBank flakes
# have time to clear. Override via env var for local runs (e.g. RETRY_DELAY_SECONDS=0).
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "3600"))


def main():
    print("="*50)
    print(f"dTRINITY Scraper | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*50)
    started = time.time()

    tasks = {
        SHEET_TAB_NAME:           _run_fraxtal_balance,
        KATANA_SHEET_TAB:         _run_katana_balance,
        ETHEREUM_SHEET_TAB:       _run_ethereum_balance,
        DLEND_FRAXTAL_SHEET_TAB:  _run_dlend_fraxtal,
        DLEND_ETHEREUM_SHEET_TAB: _run_dlend_ethereum,
    }

    failures = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                print(f"\n[OK] {name}")
            except Exception as e:
                print(f"\n[FAIL] {name}: {type(e).__name__}: {e}")
                failures.append(name)

    # Verify and retry any sheets with missing expected rows
    for attempt in range(1, MAX_VERIFY_ATTEMPTS + 1):
        print(f"\n{'='*50}")
        print(f"[VERIFY] Pass {attempt}/{MAX_VERIFY_ATTEMPTS}")
        print(f"{'='*50}")
        misses = verify_today()
        if not misses:
            print("[OK] All expected rows populated.")
            break
        if RETRY_DELAY_SECONDS > 0:
            print(f"[VERIFY] {len(misses)} sheet(s) have misses. Sleeping {RETRY_DELAY_SECONDS}s ({RETRY_DELAY_SECONDS/60:.0f}m) before retry to let rate-limits clear...")
            time.sleep(RETRY_DELAY_SECONDS)
        print(f"[VERIFY] Retrying sheets: {list(misses.keys())}")
        with ThreadPoolExecutor(max_workers=len(misses)) as ex:
            futures = {ex.submit(EXPECTED_SHEET_ROWS[tab]["rerun"]): tab for tab in misses}
            for fut in as_completed(futures):
                tab = futures[fut]
                try:
                    fut.result()
                    print(f"\n[RETRY OK] {tab}")
                except Exception as e:
                    print(f"\n[RETRY FAIL] {tab}: {type(e).__name__}: {e}")

    print(f"\n{'='*50}")
    print(f"[VERIFY] Final check")
    print(f"{'='*50}")
    final_misses = verify_today()

    elapsed = time.time() - started
    print(f"\n{'='*50}")
    print(f"Done in {elapsed:.1f}s. {len(tasks) - len(failures)}/{len(tasks)} sheets succeeded on first pass.")
    if failures:
        print(f"First-pass failures: {', '.join(failures)}")
    if final_misses:
        print(f"[FAIL] Persistent misses after {MAX_VERIFY_ATTEMPTS} retries:")
        for tab, rows in final_misses.items():
            print(f"  {tab}: rows {rows}")
        print(f"{'='*50}")
        import sys
        sys.exit(1)
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
