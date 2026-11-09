import argparse
import csv
import hashlib
import os
import sys
import zipfile
import psycopg
from psycopg import sql

# Target table schema configurations and expected columns
EXPECTED = {
    "agency.txt": [
        "agency_id",
        "agency_name",
        "agency_url",
        "agency_timezone",
        "agency_lang",
        "agency_phone",
        "agency_fare_url",
        "agency_email",
    ],

    "stops.txt": [
        "stop_id",
        "stop_code",
        "stop_name",
        "tts_stop_name",
        "stop_desc",
        "stop_lat",
        "stop_lon",
        "zone_id",
        "stop_url",
        "location_type",
        "parent_station",
        "stop_timezone",
        "wheelchair_boarding",
        "level_id",
        "platform_code",
    ],

    "routes.txt": [
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_desc",
        "route_type",
        "route_url",
        "route_color",
        "route_text_color",
        "route_sort_order",
        "continuous_pickup",
        "continuous_drop_off",
        "network_id",
    ],

    "trips.txt": [
        "route_id",
        "service_id",
        "trip_id",
        "trip_headsign",
        "trip_short_name",
        "direction_id",
        "block_id",
        "shape_id",
        "wheelchair_accessible",
        "bikes_allowed",
    ],

    "stop_times.txt": [
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
        "stop_headsign",
        "pickup_type",
        "drop_off_type",
        "continuous_pickup",
        "continuous_drop_off",
        "shape_dist_traveled",
        "timepoint",
    ],

    "calendar.txt": [
        "service_id",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "start_date",
        "end_date",
    ],

    "calendar_dates.txt": [
        "service_id",
        "date",
        "exception_type",
    ],
}

# Foreign-key dependency execution order
FILE_ORDER = [
    "calendar.txt",
    "calendar_dates.txt",
    "routes.txt",
    "stops.txt",
    "trips.txt",
    "stop_times.txt",
]

# Casting & Ingestion queries per file
INSERT_STATEMENTS = {
    "agency.txt": sql.SQL("""
        INSERT INTO gtfs.agency (
            feed_id, agency_id, agency_name,
            agency_url, agency_timezone
        )
        SELECT
            %(feed)s,
            NULLIF(agency_id, ''),
            agency_name,
            agency_url,
            agency_timezone
        FROM stg;
    """),

    "stops.txt": sql.SQL("""
        INSERT INTO gtfs.stops (
            feed_id, stop_id, stop_name,
            stop_lat, stop_lon, parent_station
        )
        SELECT
            %(feed)s,
            stop_id,
            stop_name,
            stop_lat::real,
            stop_lon::real,
            NULLIF(parent_station, '')
        FROM stg;
    """),

    "routes.txt": sql.SQL("""
        INSERT INTO gtfs.routes (
            feed_id, route_id, agency_id,
            route_short_name, route_long_name, route_type
        )
        SELECT
            %(feed)s,
            route_id,
            NULLIF(agency_id, ''),
            NULLIF(route_short_name, ''),
            NULLIF(route_long_name, ''),
            route_type::int
        FROM stg;
    """),

    "trips.txt": sql.SQL("""
        INSERT INTO gtfs.trips (
            feed_id, trip_id, route_id,
            service_id, direction_id, block_id, shape_id
        )
        SELECT
            %(feed)s,
            trip_id,
            route_id,
            service_id,
            NULLIF(direction_id, '')::smallint,
            NULLIF(block_id, ''),
            NULLIF(shape_id, '')
        FROM stg;
    """),

    "stop_times.txt": sql.SQL("""
        INSERT INTO gtfs.stop_times (
            feed_id, trip_id, stop_sequence, stop_id,
            arrival_secs, departure_secs, timepoint
        )
        SELECT
            %(feed)s,
            trip_id,
            stop_sequence::int,
            stop_id,
            gtfs.parse_secs(arrival_time),
            gtfs.parse_secs(departure_time),
            NULLIF(timepoint, '')::smallint
        FROM stg;
    """),

    "calendar.txt": sql.SQL("""
        INSERT INTO gtfs.calendar (
            feed_id, service_id,
            monday, tuesday, wednesday, thursday,
            friday, saturday, sunday,
            start_date, end_date
        )
        SELECT
            %(feed)s,
            service_id,
            monday::boolean,
            tuesday::boolean,
            wednesday::boolean,
            thursday::boolean,
            friday::boolean,
            saturday::boolean,
            sunday::boolean,
            to_date(start_date, 'YYYYMMDD'),
            to_date(end_date, 'YYYYMMDD')
        FROM stg;
    """),

    "calendar_dates.txt": sql.SQL("""
        INSERT INTO gtfs.calendar_dates (
            feed_id, service_id, date, exception_type
        )
        SELECT
            %(feed)s,
            service_id,
            to_date(date, 'YYYYMMDD'),
            exception_type::smallint
        FROM stg;
    """),
}

def read_agency_timezone(zf: zipfile.ZipFile) -> str | None:
    if "agency.txt" not in zf.namelist():
        return None
    
    with zf.open("agency.txt") as f:
        # Decode stream on the fly to support csv.DictReader
        lines = (line.decode("utf-8-sig") for line in f)
        reader = csv.DictReader(lines)
        for row in reader:
            tz = row.get("agency_timezone")
            if tz:
                return tz.strip()
    return None

def calculate_sha256(filepath: str) -> str:
    """Computes SHA-256 hash of a file on disk."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

# Create a temporary staging table, streams COPY data, and runs INSERT ... SELECT
def process_gtfs_file(cur: psycopg.Cursor, zf: zipfile.ZipFile, name: str, feed_id: int):
    table_alias = name.replace(".txt", "")
    stg = sql.Identifier(f"stg_{table_alias}")

    # 1. Read header line separately
    with zf.open(name) as f:
        header_line = f.readline()
    
    cols = [c.strip() for c in next(csv.reader([header_line.decode("utf-8-sig")]))]

    # 2. Create staging table inside current transaction
    cur.execute(
        sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
            stg,
            sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in cols)
        )
    )

    # 3. Stream raw CSV into staging table using psycopg3 copy pipeline
    with cur.copy(sql.SQL("COPY {} FROM STDIN (FORMAT csv, HEADER true)").format(stg)) as cp, \
         zf.open(name) as f:
        while chunk := f.read(1 << 20):
            cp.write(chunk)

    # 4. Fill in missing expected columns if they were absent in the input CSV
    if name in EXPECTED:
        for c in EXPECTED[name]:
            cur.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} text").format(
                    stg, sql.Identifier(c)
                )
            )

    # 5. Execute INSERT INTO gtfs.* SELECT ... FROM stg
    cur.execute(INSERT_STATEMENTS[name].format(stg=stg), {"feed": feed_id})

    # Drop temporary staging table to free backend resource for next file
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(stg))

# Process a single GTFS zip archive inside a dedicated transaction block
def load_zip(conn: psycopg.Connection, zip_path: str):
    sha256 = calculate_sha256(zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        tz = read_agency_timezone(zf)

        # Transaction boundary: All inserts roll back if an exception occurs
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gtfs.feed (sha256, agency_timezone, loaded_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (sha256) DO NOTHING
                    RETURNING feed_id;
                    """,
                    (sha256, tz),
                )
                row = cur.fetchone()
                
                # If no row returned, this hash has already been loaded successfully
                if not row:
                    print(f"Skipping '{zip_path}' (sha256: {sha256[:8]}...): already loaded.")
                    return

                feed_id = row[0]
                print(f"Loading '{zip_path}' (feed_id: {feed_id})...")

                for filename in FILE_ORDER:
                    if filename in zf.namelist():
                        print(f"  -> Ingesting {filename}...")
                        process_gtfs_file(cur, zf, filename, feed_id)

def main():
    parser = argparse.ArgumentParser(description="Load GTFS ZIP archives into PostgreSQL.")
    parser.add_argument("zip_files", nargs="+", help="Paths to GTFS .zip files")
    args = parser.parse_args()

    conn_string = os.getenv("DATABASE_URL")
    if not conn_string:
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # psycopg3 defaults to explicit transactions (autocommit=False)
    with psycopg.connect(conn_string) as conn:
        for zip_path in args.zip_files:
            try:
                load_zip(conn, zip_path)
            except Exception as err:
                print(f"Failed to load '{zip_path}'. Transaction rolled back. Error: {err}", file=sys.stderr)

if __name__ == "__main__":
    main()