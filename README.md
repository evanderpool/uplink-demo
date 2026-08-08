# Uplink — Public Demo

A live, internet-reachable demo of [Uplink](https://github.com/evanderpool/uplink),
a local-first document retrieval system. This repo is a deployment fork: same
engine, different trust posture.

**What's different from the real Uplink:**

| | Uplink (the product) | This demo |
|---|---|---|
| Where documents live | Your machine, never leave it | Public records only: ten Apple 10-K annual reports (SEC filings, FY2016–FY2025) in their original `.xls` format, shipped in `demo-docs/` |
| Who answers questions | A supervised Claude Code session ("borrowed brain") | An API worker (`uplink/api_brain.py`) calling Claude Haiku |
| Writes | Uploads/feedback/notes on localhost | All disabled except asking a question |
| Ask limits | None | 5 questions per visitor, back after 3 days (`uplink/ratelimit.py`) |

What's the same: retrieval is BM25 over SQLite FTS5, answers are composed
only from retrieved chunks, and every citation is mechanically verified
against the index before it is published — the model picks chunks by number
and the coordinates are copied verbatim from search results, so it cannot
cite a document it was never shown.

## Deploy (Render, free plan)

1. Push this repo to GitHub.
2. In Render: New → Blueprint → point it at the repo (`render.yaml` does the rest).
3. Set one environment variable in the dashboard:
   - `ANTHROPIC_API_KEY` — powers Ask AI (set a monthly spend cap on the
     Anthropic workspace; the demo is designed to cost pennies).
4. The build indexes the shipped filings; the app serves at your
   `*.onrender.com` address. Free plan sleeps when idle — first visit
   after a nap takes ~30–60s.

## Run locally

```
pip install -r requirements-demo.txt
python scripts/build_demo_index.py
set UPLINK_DEMO=1            # PowerShell: $env:UPLINK_DEMO="1"
set ANTHROPIC_API_KEY=...    # omit to run search-only
python -m uplink serve --db data/uplink.db
```

Without `UPLINK_DEMO=1` this behaves exactly like upstream Uplink.

## License

MIT, same as upstream.
