# LedgerLens dashboard (Angular 21 LTS)

Standalone components, signals, strict TypeScript + strict templates, SCSS, RxJS.

| Page | Route | API |
|---|---|---|
| Live feed | `/live` | `WS /v2/ws/flagged?token=` (RxJS `webSocket`, exponential backoff 1s→30s) |
| Flagged table | `/flagged` | `GET /v2/transactions?flagged=true` (cursor stack for prev/next) |
| Customer detail | `/customers/:id` | `GET /v2/customers/{id}` (degrades if risk is down) |
| Agent chat | `/agent` | `POST /v2/agent/query` |
| Audit log | `/audit` (admin) | `GET /v2/audit`: route guard for UX, gateway 403 for enforcement |

Everything calls `/api/...`. In dev, `proxy.conf.json` forwards it to the gateway on
`localhost:8002` (WebSocket included). In the container, nginx does the same using `GATEWAY_URL`.

```bash
npm ci
npm start                       # ng serve on :4200; run the gateway with DEV_MODE=true for /dev/token login
npx ng test --watch=false       # Vitest
npx ng build                    # production build -> dist/frontend/browser
```

Container: `docker build -t ledgerlens/frontend .` runs as non-root on port 8080
(`nginxinc/nginx-unprivileged`). It also works under an arbitrary UID in group 0, which is what OpenShift uses.
