#!/usr/bin/env python3
"""Fully-autonomous AIProve agent (handle: noir) — v4: guard + forecasts.

v3 added guard duty (state-diff entry, leaderboard report). v4 completes
the forecast loop the v2 docstring always promised:

  1. sign in with its own keypair (EIP-191 challenge -> Bearer session)
  2. GUARD: enter only new or reopened rounds (state file diff)
  3. FORECASTS: for every open forecast bounty, build a submission
     payload from a price snapshot (JSON file: fresh quotes with 24h
     changes) using the payload shapes the arena accepts:
       direction (btc-tomorrow, forecast-eth-24h, forecast-btc-7d)
           -> {"prediction": {"answer": "yes"/"no", "prob": p}}
       value     (forecast-nvda-3d, spy-close-1dollar, gold)
           -> {"prediction": {"value": N}}
       signal    (signal-eth-pnl-24h)
           -> {"prediction": {"direction": "long"/"short"}}
     Direction follows the 24h move; confidence scales with the move's
     size and is capped to the calibrated 0.55-0.65 band (a forecast is
     Brier-scored: theatrical confidence is a losing bet).
  4. report the leaderboard standing; persist state.

Forecast submission itself stays behind the operator's freshness rule:
prices must be quoted at submit time, so `main()` prints the built
payloads and submits them only when SUBMIT_FORECASTS=1 is set with a
snapshot younger than 15 minutes. `--dry-run-forecasts prices.json`
builds and prints payloads with no auth and no network writes.

Usage: AGENT_PRIVATE_KEY=0x... python3 agent.py
       python3 agent.py --dry-run-forecasts prices.json
"""
import json, os, sys, time
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

BASE = os.environ.get("API_BASE_URL", "https://aiprove.fun")
HANDLE = os.environ.get("AGENT_HANDLE", "noir")
STATE_FILE = os.environ.get("STATE_FILE", "./aiprove-autonomous-state.json")

DIRECTION_SLUGS = {"btc-tomorrow", "forecast-eth-24h", "forecast-btc-7d"}
VALUE_SLUGS = {"forecast-nvda-3d": "NVDA", "spy-close-1dollar": "SPY",
               "forecast-gold-tomorrow": "GLD"}
SIGNAL_SLUGS = {"signal-eth-pnl-24h"}
ASSET_OF = {"btc-tomorrow": "BTC", "forecast-eth-24h": "ETH",
            "forecast-btc-7d": "BTC", "signal-eth-pnl-24h": "ETH"}


def prob_from_change(chg_pct):
    """Calibrated confidence from a 24h % move: 0.55 base, +0.05 per
    2.5% of move, capped at 0.65 (Brier-safe band)."""
    return round(min(0.65, 0.55 + 0.05 * (abs(chg_pct) // 2.5)), 2)


def build_forecasts(bounties, prices):
    """Pure builder: open bounties + price snapshot -> {slug: payload}.
    prices: {ASSET: {"price": float, "change24h": float %}}."""
    out = {}
    for b in bounties:
        slug = b["slug"]
        if slug in DIRECTION_SLUGS:
            q = prices[ASSET_OF[slug]]
            up = q["change24h"] >= 0
            out[slug] = {"prediction": {
                "answer": "yes" if up else "no",
                "prob": prob_from_change(q["change24h"])}, "handle": HANDLE}
        elif slug in VALUE_SLUGS:
            q = prices[VALUE_SLUGS[slug]]
            out[slug] = {"prediction": {"value": round(q["price"])},
                         "handle": HANDLE}
        elif slug in SIGNAL_SLUGS:
            q = prices[ASSET_OF[slug]]
            out[slug] = {"prediction": {
                "direction": "long" if q["change24h"] >= 0 else "short"},
                "handle": HANDLE}
    return out


def authenticate(acct):
    r = requests.get(f"{BASE}/api/auth/challenge",
                     params={"address": acct.address}, timeout=30)
    r.raise_for_status()
    sig = acct.sign_message(
        encode_defunct(text=r.json()["challenge"])).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    r = requests.post(f"{BASE}/api/auth/verify",
                      json={"address": acct.address, "signature": sig},
                      timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"entered": {}}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--dry-run-forecasts":
        snap = json.load(open(sys.argv[2]))
        bounties = requests.get(f"{BASE}/api/bounties",
                                params={"status": "open"},
                                timeout=30).json()["bounties"]
        built = build_forecasts(bounties, snap["prices"])
        print(f"dry-run: {len(built)} forecast payloads from snapshot "
              f"as of {snap.get('asOf')}")
        for slug, payload in sorted(built.items()):
            print(" ", slug, json.dumps(payload))
        return 0

    doc = requests.get(f"{BASE}/skill.md", timeout=30).text
    assert "Agent Bounty Board" in doc
    acct = Account.from_key(os.environ["AGENT_PRIVATE_KEY"])
    hdr = {"Authorization": f"Bearer {authenticate(acct)}",
           "content-type": "application/json"}
    print("agent", acct.address, "handle", HANDLE)

    state = load_state()
    entered = state.setdefault("entered", {})

    bounties = requests.get(f"{BASE}/api/bounties",
                            params={"status": "open"},
                            timeout=30).json()["bounties"]
    fresh, reopened, covered = [], [], []
    for b in bounties:
        slug, ends = b["slug"], b.get("endsAt")
        if slug not in entered:
            fresh.append(slug)
        elif entered[slug] != ends:
            reopened.append(slug)
        else:
            covered.append(slug)
            continue
        requests.post(f"{BASE}/api/bounties/{slug}/enter",
                      headers=hdr, json={"handle": HANDLE}, timeout=30)
        entered[slug] = ends
    print("new rounds entered:     ", fresh)
    print("reopened rounds entered:", reopened)
    print("already covered:        ", len(covered), "rounds")

    snap_path = os.environ.get("PRICES_JSON")
    if snap_path:
        snap = json.load(open(snap_path))
        age_min = (time.time() - snap["ts"]) / 60
        built = build_forecasts(bounties, snap["prices"])
        print(f"forecast payloads built: {len(built)} "
              f"(snapshot age {age_min:.0f} min)")
        if os.environ.get("SUBMIT_FORECASTS") == "1" and age_min <= 15:
            for slug, payload in sorted(built.items()):
                r = requests.post(f"{BASE}/api/bounties/{slug}/submit",
                                  headers=hdr, json=payload, timeout=30)
                print("  submitted", slug, r.status_code)
        else:
            print("  (not submitted: SUBMIT_FORECASTS!=1 or snapshot stale)")

    board = requests.get(f"{BASE}/api/leaderboard", timeout=30).json()
    rows = board if isinstance(board, list) else board.get("leaderboard", [])
    by_subs = sorted(rows, key=lambda x: -(x.get("submissions") or 0))
    by_earn = sorted(rows, key=lambda x: -(x.get("earningsUsd") or 0))
    for i, x in enumerate(by_subs, 1):
        if x.get("handle") == HANDLE:
            top = by_subs[0]
            print(f"submissions rank: #{i} ({x.get('submissions')} subs; "
                  f"#1 is {top.get('handle')} with "
                  f"{top.get('submissions')}, gap "
                  f"{(top.get('submissions') or 0) - (x.get('submissions') or 0)})")
    for i, x in enumerate(by_earn, 1):
        if x.get("handle") == HANDLE:
            print(f"earnings rank:    #{i} (${x.get('earningsUsd')}, "
                  f"{x.get('wins')} wins)")

    json.dump(state, open(STATE_FILE, "w"), indent=1)
    print("state saved:", STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
