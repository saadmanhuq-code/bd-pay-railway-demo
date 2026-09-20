# BD Pay — Railway demo scaffolding

**Host choice:** Railway (not Vercel) for edge apps + optional Python gateway + Postgres.

**Cost:** free/trial only until CEO approves billing. Do not enable paid addons without a nod.

**Honesty:** public demo stays **mock / simulator**. No live rails, no fake “production paid.”

## Services (recommended)

| Railway service | Root / source | Start |
|---|---|---|
| `ops-console` | `apps/ops-console` | `npx next start -p $PORT` after build |
| `developer-portal` | `apps/developer-portal` | same |
| `diner-app` | `apps/diner-app` | same (secondary for gateway scrutiny) |
| `gateway` (optional) | repo `Dockerfile` role `app` | needs Postgres |

Monorepo note: `@bdpay/edge` must resolve for Next config. Prefer building from **repo root** with a root directory set per service, or install workspaces then build the app.

## Build (Nixpacks / Railway)

Suggested per Next service (set **Root Directory** to the app path):

```
# Build
npm ci --ignore-scripts
npm install --ignore-scripts --workspace=packages/edge
# ensure @bdpay/edge linked into the app if needed
npx next build

# Start
npx next start -p ${PORT:-3000}
```

See also `railway.toml` next to each app.

## Env

Copy from `.env.example`. Minimum for mock FE:

```
NEXT_PUBLIC_BDPAY_API_BASE=/api/mock
BDPAY_ENABLE_MOCK=1
NODE_ENV=production
```

## Gateway (optional second phase)

Use the existing hardened `Dockerfile` (Python roles: `app` | `vault` | `migrate`). Attach Railway Postgres only on free/trial; stop idle services after demos.

## Operator checklist

1. Create Railway project `bd-pay-demo` (private).
2. Add three services from GitHub `saadmanhuq-code/bd-pay` (or GitLab mirror) @ `main`, each with root directory `apps/<name>`.
3. Set env from `.env.example`; generate `*.up.railway.app` domains.
4. Walk login → payment-links / ops cases with MOCK banner visible.
5. Tear down or sleep services when not demoting.

Do **not** flip `CONNECTOR_MODE` to live/sandbox without CEO + credentials unpark.
