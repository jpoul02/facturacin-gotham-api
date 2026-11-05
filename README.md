# Hacienda simulada (firmador + API MH) · Gotham

There are no MH certificates, so this service simulates the signer and the Hacienda reception API. The paths and response shapes copy the official services, so switching to the real ones only means changing the base URL.

```bash
cd facturacion-gotham-api
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
.venv/Scripts/uvicorn main:app --reload --port 8113
```

Interactive docs: http://localhost:8113/docs

| Endpoint | What it does |
|---|---|
| `POST /firmardocumento/` | Signs `dteJson` with RS512 (JWS), the same as `svfe-api-firmador`. Body: `{nit, activo, passwordPri, dteJson}` |
| `POST /seguridad/auth` | Form fields `user`, `pwd`. Returns `body.token` ("Bearer ...") |
| `POST /fesv/recepciondte` | Receives the signed DTE. Checks the signature, the codigoGeneracion, the numeroControl, the emitter NIT, the totals and duplicates. Returns `PROCESADO` + `selloRecibido`, or `RECHAZADO` + `observaciones` |
| `POST /fesv/recepcion/consultadte/` | `{nitEmisor, tdte, codigoGeneracion}` |
| `POST /fesv/anulardte` | Signed void event. Checks that the DTE exists, that it is not already voided, and that the reason is present |

Flow: sign, then auth, then recepciondte (Authorization header = token).

Environment variables (with test defaults): `NIT_EMISOR`, `PASSWORD_PRI`, `MH_USER`, `MH_PWD`, `CORS_ORIGINS`.

Data and the signing key live in memory and reset on restart.

Tests: `.venv/Scripts/python -m pytest -q`

## Connecting the frontend

In the frontend repo ([facturaci-n-gotham](https://github.com/jpoul02/facturaci-n-gotham)), create `.env.local` with `NEXT_PUBLIC_HACIENDA_URL=http://localhost:8113` (see `.env.example`).
- Without the variable, the frontend keeps its local simulation.
- With it, every sale goes through the API: sign, then auth, then recepciondte. Approving a void calls anulardte.
- Hacienda's rejection reasons are recorded in Auditoría.

## Railway

Create a service from this repo; Railway detects the Dockerfile on its own. In the frontend service, set `NEXT_PUBLIC_HACIENDA_URL` to the API's URL before the build: it is a public variable, so it is baked into the bundle at build time.

## Visual monitor

Open http://localhost:8113/ while the API runs. It shows:
- The emission flow: POS, signer, MH auth, MH reception. Each step lights up when a request passes through it.
- The live activity log. Click a row to see the request and the response. Passwords are masked, and the JWS is shown decoded.
- Totals, and the DTEs received with their seal and status.
- The **Velocidad** control (Normal, Lento, Muy lento) slows down every request: 3 or 6 seconds, half on the way there and half on the way back. A packet moves across the diagram and a line of text describes each step, so a sale can be followed like a flowchart. The test scenarios always run at normal speed.
- The **Correr escenarios de prueba** button runs 11 cases against the API itself: valid emission, duplicate, tampered signature, totals that do not match, missing token, wrong passwords, query, void, and double void. `pytest` runs the same cases.
