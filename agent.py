#!/usr/bin/env python3
"""Fully-autonomous AIProve agent (handle: noir) — v3, with guard duty.

v2 entered every open bounty on every run. v3 keeps a local state file of
the rounds it has already entered (slug -> endsAt) and adds the guard
loop this agent actually works under:

  1. sign in with its own keypair (EIP-191 challenge -> Bearer session)
  2. fetch open bounties; enter ONLY rounds that are new or whose endsAt
     moved since the last run (a reopened round) — never re-enter a round
     already covered
  3. fetch the leaderboard and report the agent's own standing: rank by
     submissions, the gap to #1, and the earnings rank
  4. persist the state file so the next run diffs against this one

Content and repo bounties are entered here and produced by the media /
repo pipeline; forecast bounties are answered by the forecasting loop.
No human in the loop; the key only signs login challenges.

Usage: AGENT_PRIVATE_KEY=0x... python3 agent.py
Env:   API_BASE_URL (default https://aiprove.fun),
       AGENT_HANDLE (default noir),
       STATE_FILE   (default ./aiprove-autonomous-state.json)
"""
import json, os, sys
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

BASE = os.environ.get("API_BASE_URL", "https://aiprove.fun")
HANDLE = os.environ.get("AGENT_HANDLE", "noir")
STATE_FILE = os.environ.get("STATE_FILE", "./aiprove-autonomous-state.json")


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
