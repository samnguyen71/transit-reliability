# Feed logger

Polls an agency's GTFS-realtime feeds every 30 seconds and keeps every
snapshot, byte for byte. It also keeps every version of the static GTFS
schedule. Everything later in the project is built from this archive, so it
runs nonstop on a small VM from day one.

## What it stores

```
data/raw/
  trip_updates/
    2026-10-08/                       one folder per UTC day
      20261008T213339Z.pb.gz          a snapshot, gzipped, exactly as received
      _manifest.jsonl                 one line per fetch attempt, good or bad
    _state.json                       last hash and ETag, so a restart doesn't re-save
  static_gtfs/
    2026-10-08/20261008T213341Z.zip   saved only when the agency publishes a new version
```

Each manifest line records `fetched_at` and a `status`: `saved`, `duplicate`
(same bytes as last time, not stored again), `not_modified` (HTTP 304),
`invalid`, `http_error`, `network_error`, `too_large`, `disk_low` or
`storage_error`. It also has the HTTP status, sizes, a hash, the file name,
the feed's own timestamp and age, the entity count and any error.

To read a snapshot in Phase 1, use the official bindings
(`pip install gtfs-realtime-bindings`):

```python
import gzip
from google.transit import gtfs_realtime_pb2

feed = gtfs_realtime_pb2.FeedMessage()
with open(path, "rb") as f:
    feed.ParseFromString(gzip.decompress(f.read()))
```

## 1. Try it on your computer

You need Python 3.11 or newer. From the `logger` folder:

```bash
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.toml config.toml  # Windows: copy config.example.toml config.toml
cp ../.env.example ../.env          # only needed if the feeds need an API key
```

Put your agency's feed URLs in `config.toml` (find them on the agency's
developer page or the [Mobility Database](https://mobilitydatabase.org)), and
the API key, if there is one, in `../.env`. Then:

```bash
python -m feedlogger peek trip_updates                  # what's in the feed?
python -m feedlogger run --once --data-dir ./data/raw   # fetch each feed once
```

## 2. Choose the agency with `peek`

`peek` fetches a feed once, stores nothing, and describes it. Give it a feed
name from `config.toml`, any URL, or a saved snapshot file. Look for:

- **HTTP 200** and "GTFS-realtime 2.0, full dataset".
- **Trip IDs on nearly everything.** Without them, matching to the schedule is hard.
- **A fresh feed timestamp:** seconds old, not minutes.
- **The notes at the end.** Whether passed stops stay in the feed decides how
  Phase 1 finds actual arrival times.
- **The disk estimate**, compared with your VM's disk.

Run it during service hours: at 3 a.m. many feeds are empty.

## 3. Run it on the VM

1. **Create the VM.** Ubuntu 24.04 LTS with SSH key login. The logger needs
   very little: a B1s is enough, and it's free for 12 months on new Azure
   accounts. Size the disk using the `peek` estimate.

2. **Install Docker**, then log out and back in:

   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
   sudo usermod -aG docker $USER
   ```

3. **Get the code there.** Push this repo to GitHub, then on the VM:

   ```bash
   git clone https://github.com/YOUR-USERNAME/transit-reliability.git
   cd transit-reliability
   ```

   A public repo clones without logging in. For a private one, copy the
   folder up with `scp -r` instead.

4. **Configure and start.** Commit `logger/config.toml`, since it records
   which feeds you logged. The key goes in `.env`, which is never committed.

   ```bash
   cp .env.example .env && nano .env   # API key, and Healthchecks URLs later
   mkdir -p data                       # create this before the first start
   docker compose run --rm logger run --once
   docker compose up -d --build
   docker compose logs -f logger       # Ctrl+C stops watching, not the logger
   ```

   You should see one "first snapshot saved" line per feed. After that, each
   feed logs a summary every 15 minutes, and problems right away.

After changing `config.toml` or `.env`, apply it with
`docker compose up -d --force-recreate`.

## 4. Get an email if it stops

1. Make a free [Healthchecks.io](https://healthchecks.io) account and add one
   check per realtime feed. Set its **Period** to 1 minute and its
   **Grace Time** to 10 minutes. The logger pings at most once a minute, and
   only after a good fetch, so you're alerted after about 10 minutes of failures.
2. Put each check's ping URL in `.env` (`HC_TRIP_UPDATES=...`) and uncomment
   the matching `healthcheck_url` line in `config.toml`.
3. Apply it: `docker compose up -d --force-recreate`.
4. Test it: `docker compose stop logger`, wait for the email,
   then `docker compose start logger`.

## 5. Check on it

```bash
docker compose ps                         # is it up?
docker compose logs --tail 50 logger      # recent summaries and errors
docker compose run --rm logger report     # gaps, failures, disk use, days until full
```

Run `report` after the first full day: it does the disk math for you and says
which feed to poll less often if the disk won't last.

## 6. Back up the data (next week)

Copy new files to object storage every night with
[rclone](https://rclone.org). Cloudflare R2, Backblaze B2 and Azure Blob all
work.

```bash
sudo -v ; curl https://rclone.org/install.sh | sudo bash
rclone config          # add a remote, here called "backup"
crontab -e             # opens your schedule in an editor
```

Add this line to the schedule. It runs at 03:30 every night:

```
30 3 * * * rclone copy $HOME/transit-reliability/data/raw backup:transit-raw --max-age 48h --exclude "_tmp/**" >> $HOME/backup.log 2>&1
```

`--max-age 48h` uploads only recent files, so each night is quick. To get an
email if the backup stops, add `&& curl -fsS -m 10 --retry 3 YOUR-PING-URL`
with its own Healthchecks check.

## Commands

| Command | What it does |
| --- | --- |
| `run` | Polls every feed until stopped. This is what Docker runs. |
| `run --once` | Fetches each feed once, prints the result, exits 1 if any failed |
| `peek NAME_URL_OR_FILE` | Fetches once and describes the contents; stores nothing |
| `report [--days 7]` | Stored data, failures, gaps and when the disk fills up |

On the VM: `docker compose run --rm logger COMMAND`. On your computer:
`python -m feedlogger COMMAND`, from this folder.

## How it works

These are the decisions worth knowing for interviews.

- **Raw first.** Snapshots are stored exactly as received, and parsing happens
  later. A parser bug in November means reprocessing October, not losing it.
- **Every attempt is recorded.** The manifest separates "the feed didn't
  change" from "the logger was down", which Phase 1 needs to mark gaps
  honestly instead of inventing data.
- **Unchanged snapshots aren't stored twice.** A SHA-256 hash catches
  identical responses. ETag and Last-Modified headers let a server answer
  "nothing new" without resending the file.
- **Atomic writes.** Downloads go to `_tmp` and are moved into place with one
  rename, so a crash or power cut never leaves half a file.
- **UTC everywhere.** Clocks go back on November 1. In local time 1:30 a.m.
  happens twice that night; in UTC every timestamp is unique.
- **Static GTFS by version.** Realtime data refers to trips by the IDs in the
  schedule that was running then, and agencies replace the schedule a few
  times a year. Without old versions, old realtime data can't be matched.
- **Polite failure handling.** After consecutive failures the wait doubles,
  up to 5 minutes. A server's `Retry-After` is respected.
- **Disk guard.** Below `min_free_gb` the logger stops saving, so the VM stays
  usable. The missing pings then trigger the alert.
- **No secrets in logs.** API keys live only in `.env`, and are hidden in
  logs, manifests and error messages.
- **Validation without generated code.** `gtfsrt.py` reads the protobuf wire
  format directly. It checks each snapshot really is GTFS-realtime, not an
  HTML error page, and powers `peek`.

## Troubleshooting

- **"can't write to /data/raw"**: Docker created `data` as root because it
  didn't exist yet. Fix it with `sudo chown -R 1000:1000 data`, then `docker compose up -d`.
- **"config.toml is a folder"**: Docker created it because the file was
  missing. Run `sudo rm -r logger/config.toml`, copy the example, and start again.
- **HTTP 401 or 403**: the API key is missing or sent the wrong way (header
  vs URL). `peek` shows what the server said.
- **`invalid`**: the server sent something that isn't a feed, often an HTML
  error page. It's kept as `.invalid.gz`; look inside with `zcat FILE | head -c 500`.
- **No alert when you stopped it**: check the ping URL in `.env` and that
  `healthcheck_url` is uncommented, then recreate the container.
- **Disk filling up**: `report` names the biggest feed. Raise its `interval_seconds`.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest
```

The tests build GTFS-realtime messages byte by byte and serve them from a
local HTTP server, so they need no network. CI runs them on every push and
also builds the Docker image.
