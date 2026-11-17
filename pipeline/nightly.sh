#!/usr/bin/env bash
# The nightly job on the VM: load new static zips, parse finished UTC days, observe
# every service date that has both its days parsed, then roll the results up by route,
# stop and hour. systemd runs it at 00:30 UTC (systemd/transit-nightly.timer); setup is
# in OBSERVED_TIMES.md, "Nightly on the VM".
#
# Every step skips work already done, so a missed night is caught up by the next run.
#
# Reads from the repo's .env:
#   DATABASE_URL       optional; otherwise built from POSTGRES_PASSWORD (must be URL-safe)
#   POSTGRES_PASSWORD  the db service's password
#   HC_NIGHTLY         optional Healthchecks ping URL: start, then success or failure
# PYTHON (environment) is the pipeline's interpreter, default pipeline/.venv/bin/python.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$REPO/data"
PYTHON="${PYTHON:-$REPO/pipeline/.venv/bin/python}"

env_value() {  # the last KEY=value line in .env, without surrounding quotes
    [[ -f "$REPO/.env" ]] || return 0
    grep -E "^$1=" "$REPO/.env" | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

HC="$(env_value HC_NIGHTLY)"
ping_hc() {  # ping_hc start | ping_hc <exit status>
    [[ -n "$HC" ]] && curl -fsS -m 10 --retry 3 -o /dev/null "$HC/$1" || true
}

DATABASE_URL="${DATABASE_URL:-$(env_value DATABASE_URL)}"
if [[ -z "$DATABASE_URL" ]]; then
    pw="$(env_value POSTGRES_PASSWORD)"
    DATABASE_URL="postgresql://postgres:${pw}@127.0.0.1:5432/postgres"
fi
export DATABASE_URL
# Otherwise Python buffers stdout in the journal while stderr isn't, and the log's order
# is wrong: a date's error shows up before the summaries of dates done earlier.
export PYTHONUNBUFFERED=1

step() { echo "== $*"; }

# The job's peak, as counted against the service's MemoryMax. It includes page cache
# (Parquet just read), which the kernel reclaims before killing anything.
report_peak() {
    local cg dir peak swap
    cg="$(sed -n 's/^0:://p' /proc/self/cgroup)"
    dir="/sys/fs/cgroup${cg}"
    [[ -r "$dir/memory.peak" ]] || return 0
    peak=$(( $(cat "$dir/memory.peak") / 1048576 ))
    swap=0
    [[ -r "$dir/memory.swap.peak" ]] && swap=$(( $(cat "$dir/memory.swap.peak") / 1048576 ))
    echo "== memory peak ${peak} MiB, swap peak ${swap} MiB"
}

run() {
    ping_hc start
    cd "$REPO" || return 1

    # A db stopped by hand stays stopped (restart: unless-stopped), so start it here.
    step "database"
    docker compose up -d db || return 1
    for _ in $(seq 30); do
        docker compose exec -T db pg_isready -U postgres -q && break
        sleep 2
    done
    docker compose exec -T db pg_isready -U postgres -q || { echo "database not ready"; return 1; }

    cd "$REPO/pipeline" || return 1

    # Each zip is loaded once (by sha256); the rest are skipped.
    step "static feeds"
    shopt -s nullglob
    zips=("$DATA"/raw/static_gtfs/*/*.zip)
    if (( ${#zips[@]} )); then
        "$PYTHON" -m gtfsdb.load "${zips[@]}" | grep -v "^Skipping "
        codes=("${PIPESTATUS[@]}")
        (( codes[0] == 0 )) || return 1
    fi

    status=0
    step "parse"
    "$PYTHON" -m realtime.parse "$DATA/raw" "$DATA/parsed" || status=1
    # Observe even after a parse failure: dates whose days did parse still get done.
    step "observe"
    "$PYTHON" -m realtime.observe "$DATA/parsed" "$DATA/observed" --threads 1 || status=1
    # Likewise: every date with a visits file gets its rollups, new or redone ones.
    step "rollup"
    "$PYTHON" -m realtime.rollup "$DATA/observed" "$DATA/rollups" || status=1
    return $status
}

run
status=$?
report_peak
ping_hc "$status"
exit "$status"
