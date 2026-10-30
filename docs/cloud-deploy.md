# Cloud demo: Render (backend) + Vercel (frontend)

> Status: configs are written and statically checked (YAML/JSON parse, Kafka start command tested
> locally with the pinned image). They have **not** been deployed to a live Render or Vercel
> account from this repo. Treat the first deploy as a test.

## Topology

| Component | Where | Plan | Why |
|---|---|---|---|
| Angular dashboard | Vercel (`frontend/vercel.json`) | Hobby | static build; `/api/*` rewritten to the gateway |
| gateway | Render web service | free | only web services can be free. Free services can *send* but not *receive* private traffic, which suits the gateway |
| kafka | Render private service (`deploy/render/kafka.Dockerfile`) | 1c-2g | 512 MB heap; advertises its private IP; no disk, so data doesn't survive a restart (demo) |
| couchbase | Render private service + 5 GB disk | 2c-4g | CE needs ~1.5 GB; init script runs at start (idempotent) |
| risk, ingest, generator | private service / workers | 0.5c-512mb | |
| agent | Render private service | 1c-2g | torch + MiniLM embeddings; the FAISS index is built at first start |
| MongoDB | MongoDB Atlas M0 (external) | free | 512 MB storage, hence `GEN_RATE_PER_SEC=2` |

Check current prices on render.com before creating the Blueprint. Everything except the gateway
is paid.

**WebSocket:** Vercel rewrites don't proxy WebSockets. The live feed therefore connects directly to
`wss://<gateway>.onrender.com`, set at build time by `LEDGERLENS_WS_BASE`. The `<meta name="ledgerlens-ws-base">`
tag in `frontend/src/index.html` is the hook.

**Security:** `DEV_TOKEN_ROLES=analyst`, so anyone visiting can log in as an analyst (synthetic data
only), but nobody can self-issue admin. To get an admin token, mint it locally with the Render-generated
`JWT_SECRET`: `JWT_SECRET=... uv run python services/gateway/scripts/mint_token.py --sub you --role admin`.

## HUMAN ACTION REQUIRED

1. **Push the repo to GitHub.** Render and Vercel deploy from Git.
   `gh repo create ledgerlens --private --source . --push`
2. **MongoDB Atlas:** create a free M0 cluster, a database user, and a network rule. Render has no
   static egress IPs on low plans, so allow `0.0.0.0/0` and rely on the strong password. Copy the
   `mongodb+srv://...` URI.
3. **Render:** go to Dashboard → New → Blueprint and pick the repo; Render reads `render.yaml`. When prompted, paste the
   Atlas URI into each `MONGO_URI`, and optionally a `GOOGLE_API_KEY` for the agent. Confirm the
   gateway's public URL. If it isn't `https://ledgerlens-gateway.onrender.com`, edit the rewrite
   destination in `frontend/vercel.json`.
4. **Vercel:** create a new project from the repo and set *Root Directory* to `frontend`. Add an env var
   `LEDGERLENS_WS_BASE=wss://ledgerlens-gateway.onrender.com`, then deploy.
5. **Smoke test:** open the Vercel URL, log in as analyst, and check:
   - Live feed shows `live`.
   - The Flagged table fills once risk has scored traffic.
   - Agent chat answers.

## Known caveats

- The free gateway sleeps after inactivity, so the first request after idle is slow.
- Kafka has no disk. If it restarts, topics are recreated empty, and consumers resubscribe through the
  gateway's and ingest's normal reconnect paths.
- The `fromService` YAML shape and `disk` keys follow Render's docs as read on 2026-10-30. The
  fetched page's code samples were stripped, so the exact nesting is unverified.
