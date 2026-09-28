# BD Pay — Railway public demo (mock / simulator)

Three Next.js services, all mock / simulator only (no live money rails):

| Service | Public URL | Entry |
|---|---|---|
| `ops-console` | https://ops-console-production-c93d.up.railway.app | `/login` |
| `developer-portal` | https://developer-portal-production-d141.up.railway.app | `/login` |
| `diner-app` | https://diner-app-production.up.railway.app | `/` |

## Required environment (production)

| Variable | ops-console | developer-portal | diner-app | Notes |
|---|---|---|---|---|
| `NODE_ENV` | `production` | `production` | `production` | Production signal for the fail-closed checks |
| `BDPAY_SESSION_SECRET` | **required** | **required** | **required** | ≥ 32 chars; `openssl rand -hex 32`; distinct per service |
| `BDPAY_ENABLE_MOCK` | `1` | `1` | `1` | Server-side mock opt-in (SEC-01) |
| `NEXT_PUBLIC_BDPAY_ENABLE_MOCK` | `1` | `1` | `1` | Client mock opt-in (build time) |
| `NEXT_PUBLIC_BDPAY_API_BASE` | `/api/mock` | `/api/mock` | `/api/mock` | Build time |

`BDPAY_SESSION_SECRET` signs the demo session cookies. When `NODE_ENV=production`
and it is missing or too short, the app refuses to issue or verify sessions and
logs an error naming `BDPAY_SESSION_SECRET` — it never falls back to the public
development secret (not even with `BDPAY_ENABLE_MOCK=1`). Local dev and tests
need no setup: outside production the dev fallback is still used.
`BDPAY_MOCK_SESSION_SECRET` remains a legacy alias read when
`BDPAY_SESSION_SECRET` is unset.

Never commit secret values; set them in the Railway service Variables tab.

## Walkthrough

1. ops-console `/login`: any email + password, then the mock TOTP code.
2. developer-portal `/login`: pick a persona, TOTP step-up for mutations.
3. diner-app `/`: browse offers; phone OTP sign-in for pay flows.

See [`README.md`](README.md) for build/start settings and the operator checklist.
