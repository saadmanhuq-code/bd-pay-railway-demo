# bd-pay-railway-demo

Public build mirror of the BD Pay frontend monorepo subset for Railway demos.

Contains npm workspaces root + `apps/{ops-console,developer-portal,diner-app}` + `packages/edge`.
Mock / simulator only — not live money rails.

Private source of truth remains elsewhere; this repo exists so Railway can clone without private GitHub access.

## Gateway Docker

Gateway image lives at `Dockerfile.gateway` so frontend Railpack builds are not forced onto the Python Dockerfile at repo root.
