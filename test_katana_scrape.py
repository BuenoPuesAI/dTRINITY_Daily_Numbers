"""Tests for the Katana scrape hardening. No network, no browser."""
import os, sys
os.environ.setdefault("ETHERSCAN_API_KEY", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper
from scraper import _parse_katana_tokens, scrape_debank_katana, KATANA_TOKEN_ROW_MAP

FULL_BODY = """
Portfolio
frxusd
$1.00
<$0.01
sfrxusd
$1.05
$121,546.09
vbusdt
$1.00
$68,218.38
vbusdc
$1.00
$13,761.73
ausd
$1.00
$1.00
yusd
$1.01
$0.02
""".strip().split("\n")

EMPTY_BODY = ["Portfolio", "Loading...", "Connect Wallet"]
PARTIAL_BODY = FULL_BODY[:10]   # frxusd, sfrxusd, vbusdt only

fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got}\n        want {want}")
        fails.append(name)

# --- 1. Parser still extracts the same values as before -------------------
r = _parse_katana_tokens(FULL_BODY, verbose=False)
check("parse: all 6 tokens", sorted(r), sorted(KATANA_TOKEN_ROW_MAP))
check("parse: sfrxusd value", r["sfrxusd"], 121546.09)
check("parse: yusd value", r["yusd"], 0.02)
check("parse: frxusd zero balance kept", r["frxusd"], 0.0)
check("parse: empty page -> {}", _parse_katana_tokens(EMPTY_BODY, verbose=False), {})

# --- 2. Retry loop behaviour ----------------------------------------------
scraper.KATANA_SCRAPE_BACKOFF_SECONDS = 0  # keep the test fast

def stub(sequence):
    it = iter(sequence)
    def _once():
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return _once

full = _parse_katana_tokens(FULL_BODY, verbose=False)
partial = _parse_katana_tokens(PARTIAL_BODY, verbose=False)

# 2a. Succeeds first try -> no retries
scraper._scrape_katana_once = stub([full])
check("retry: clean first attempt", scrape_debank_katana(), full)

# 2b. Empty then full -> recovers in-process (the real-world daily case)
scraper._scrape_katana_once = stub([{}, full])
check("retry: empty then full recovers", scrape_debank_katana(), full)

# 2c. Exception then full -> exception is caught and retried
scraper._scrape_katana_once = stub([RuntimeError("nav timeout"), full])
check("retry: exception then full recovers", scrape_debank_katana(), full)

# 2d. Always empty -> raises instead of silently returning {}
scraper._scrape_katana_once = stub([{}, {}, {}])
try:
    scrape_debank_katana()
    check("retry: all-empty raises", "no raise", "RuntimeError")
except RuntimeError as e:
    check("retry: all-empty raises", "RuntimeError", "RuntimeError")
    print(f"        msg: {e}")

# 2e. Below the floor (3 tokens) on every attempt -> raises
three = {k: partial[k] for k in list(partial)[:3]}
scraper._scrape_katana_once = stub([three, three, three])
try:
    scrape_debank_katana()
    check("retry: 3/6 below floor raises", "no raise", "RuntimeError")
except RuntimeError:
    check("retry: 3/6 below floor raises", "RuntimeError", "RuntimeError")

# 2f. At the floor (4 tokens) after exhausting attempts -> partial accepted
four = {k: full[k] for k in list(full)[:4]}
scraper._scrape_katana_once = stub([four, four, four])
check("retry: 4/6 partial accepted at floor", scrape_debank_katana(), four)

# 2g. Exactly KATANA_SCRAPE_ATTEMPTS calls, no more
calls = []
def counting():
    calls.append(1)
    return {}
scraper._scrape_katana_once = counting
try:
    scrape_debank_katana()
except RuntimeError:
    pass
check("retry: attempt count capped", len(calls), scraper.KATANA_SCRAPE_ATTEMPTS)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
