# API

FastAPI over the nightly rollups (`data/rollups`), the static GTFS in Postgres and the
logger's live TripUpdates (`data/raw`). Settings are in `transit_api/settings.py`.

## Run it on your computer

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn transit_api.main:app --reload       # from api/, with DATABASE_URL set
pytest -q                                   # database tests skip without DATABASE_URL
```

Then open http://127.0.0.1:8000/docs.

## Run it on the VM

It runs in Docker next to the logger and Postgres: one uvicorn worker, with
[Caddy](https://caddyserver.com) in front (`proxy/Caddyfile`).

```
internet ── :80/:443 ── proxy (Caddy) ── api:8000 ── db:5432
                                           │
                                  ./data (read-only)
VM only ── 127.0.0.1:8000 ─────────────────┘
```

- **One worker.** The rollup cache, the parsed live snapshot and the Postgres pool
  belong to one process. A second worker would double them, and the VM has 887 MiB.
- **Port 8000 is published on 127.0.0.1 only.** Inside its container uvicorn listens on
  0.0.0.0, so Caddy can reach it over Compose's network. Docker's published ports bypass
  ufw, so the `127.0.0.1:` in `docker-compose.yml` is what keeps 8000 private.
- **80 and 443 are public.** They bypass ufw as well, so Azure's network security group
  decides whether they're open.

Once, on the VM:

1. **Open ports 80 and 443.** In the Azure portal: the VM → Networking → add inbound
   port rules for TCP 80 and TCP 443 (and UDP 443 for HTTP/3).
2. **Optional: a domain name, for HTTPS.** Azure gives one for free. The VM → Overview
   → "DNS name" (it says "Not configured" until you set one) → type a label such as
   `drt-reliability` → Save. Make the IP static on the same page, or the name stops
   working when the IP changes. Then copy the **whole** DNS name from the Overview page,
   label included, into `.env`:

       SITE_ADDRESS=drt-reliability.northcentralus.cloudapp.azure.com

   Check it resolves to the VM before starting Caddy: `getent hosts` followed by the
   name. Caddy gets a Let's Encrypt certificate the first time it starts, and sends HTTP
   to HTTPS. Without `SITE_ADDRESS` the API is plain HTTP on port 80. If `docker compose
   ps proxy` says `Restarting`, `docker compose logs --tail 20 proxy` says why: "does not
   qualify for certificate" means the name in `.env` is incomplete.
3. **Start it.** `POSTGRES_PASSWORD` in `.env` must be URL-safe, as for the nightly job.

       git pull
       docker compose up -d --build api proxy
       docker compose logs -f api proxy        # Ctrl+C stops watching

4. **Check it.**

       curl -s 127.0.0.1:8000/health           # on the VM, straight to uvicorn
       curl -s http://<public IP or name>/health   # from your computer, through Caddy

   `/health` says what works: `database`, the newest rollup date, and whether the live
   feed is `ok`. From your computer, `curl -s http://<public IP>:8000/health` must fail.

To see `/docs` without exposing anything new, tunnel to it:
`ssh -L 8000:127.0.0.1:8000 azureuser@<VM>`, then open http://127.0.0.1:8000/docs.

After a `git pull` that changes `api/`, rebuild: `docker compose up -d --build api`.
After changing `proxy/Caddyfile` or `SITE_ADDRESS`: `docker compose up -d --force-recreate proxy`.

Memory: `docker stats --no-stream`. The API uses about 105 MiB and is capped at 300 MB
(`mem_limit`); past the cap Docker kills it and `restart: unless-stopped` starts it again.
