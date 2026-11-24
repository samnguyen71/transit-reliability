"""The numbers behind the findings in the top-level README, from observed visits and rollups.

Usage, from pipeline/:

    python findings.py DATA_DIR STATIC_ZIP [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                       [--exclude D,D] [--min-days N] [--max-abs-delay S]

DATA_DIR holds observed/visits/*.parquet (the VM's data/, copied down). STATIC_ZIP is the
static GTFS in effect for those dates, for headsigns and route names. --exclude defaults to
the DRT outage, 2026-11-15 and 11-16. --min-days is how many dates a trip must be seen on
to be ranked (4 of 5 weekdays for a week).

--max-abs-delay (default 3000 s) leaves out observed visits further off than that. They are
matching errors, not buses: trips starting after midnight are matched to the previous
night's run (delay about -24 h, about -1 h on the fall-back night), because DRT's
start_date is the calendar date of the run and observe uses it as the service date. Drop
the filter (0) once observe is fixed and every date redone. Coverage counts them as
observed either way.

Scoring matches rollup.py: departures -60..+300 s on time, last-stop arrivals <= +300 s.
"""

import argparse
import io
import zipfile

import duckdb
import pyarrow.csv as pv

p = argparse.ArgumentParser()
p.add_argument("data")
p.add_argument("static_zip")
p.add_argument("--from", dest="d0", default="1900-01-01")
p.add_argument("--to", dest="d1", default="2999-12-31")
p.add_argument("--exclude", default="2026-11-15,2026-11-16")  # DRT outage
p.add_argument("--min-days", type=int, default=4)
p.add_argument("--max-abs-delay", type=int, default=3000)  # seconds; 0 keeps every visit
a = p.parse_args()

c = duckdb.connect()
c.sql("SET TimeZone = 'America/Toronto'")
with zipfile.ZipFile(a.static_zip) as z:
    for name in ("trips", "stops", "routes"):
        tbl = pv.read_csv(io.BytesIO(z.read(f"{name}.txt")),
                          convert_options=pv.ConvertOptions(strings_can_be_null=False,
                                                            column_types={"route_id": "string",
                                                                          "stop_id": "string"}))
        c.register(name, tbl)

excl = ",".join(f"DATE '{d}'" for d in a.exclude.split(",") if d) or "NULL"
where = f"service_date BETWEEN DATE '{a.d0}' AND DATE '{a.d1}' AND service_date NOT IN ({excl})"
D = a.data.rstrip("/\\")
c.sql(f"CREATE VIEW v AS SELECT * FROM '{D}/observed/visits/*.parquet' WHERE {where}")


def show(title, sql):
    print(f"\n## {title}")
    c.sql(sql).show(max_width=200, max_rows=40)


show("Dates and coverage (scored visits)", """
    SELECT service_date, dayname(service_date) AS day,
           count(*) AS scored,
           round(100 * avg((status = 'observed')::int), 1) AS pct_observed,
           round(100 * avg((status IN ('cancelled','skipped'))::int), 1) AS pct_not_run
    FROM v WHERE scored GROUP BY ALL ORDER BY 1""")

# kind is worked out before the filter, so dropping a trip's first stop doesn't promote
# its second. Same thresholds as rollup.py: departures -60..+300 s, arrivals <= +300 s.
too_far = f"abs(delay_s) > {a.max_abs_delay}" if a.max_abs_delay else "false"
c.sql(f"""
    CREATE VIEW k AS
    SELECT v.*,
           CASE WHEN event = 'arrival' THEN 'last_stop'
                WHEN stop_sequence = min(stop_sequence) OVER (PARTITION BY service_date, trip_id)
                     THEN 'first_stop' ELSE 'timepoint' END AS kind,
           status = 'observed' AND coalesce({too_far}, false) AS too_far
    FROM v WHERE scored""")
c.sql("""
    CREATE VIEW s AS
    SELECT * EXCLUDE (too_far),
           delay_s > 300 AS is_late,
           CASE WHEN event = 'arrival' THEN delay_s <= 300 ELSE delay_s BETWEEN -60 AND 300 END
               AS is_on_time
    FROM k WHERE NOT too_far""")

if a.max_abs_delay:
    show(f"Left out: observed visits more than {a.max_abs_delay} s early or late", """
        SELECT coalesce(route_id, 'ALL') AS route_id, count(*) FILTER (WHERE too_far) AS left_out,
               round(100 * avg(too_far::int) FILTER (WHERE status = 'observed'), 2) AS pct_of_observed,
               round(median(delay_s) FILTER (WHERE too_far) / 3600, 1) AS median_h
        FROM k GROUP BY ROLLUP (route_id) HAVING count(*) FILTER (WHERE too_far) > 0
        ORDER BY route_id IS NOT NULL, left_out DESC LIMIT 16""")

show("On time, whole period", """
    SELECT coalesce(kind, 'ALL') AS kind, count(*) FILTER (WHERE status = 'observed') AS observed,
           round(100 * avg(is_on_time::int), 1) AS pct_on_time,
           round(100 * avg(is_late::int), 1) AS pct_late_gt5,
           round(100 * avg((delay_s < -60 AND kind <> 'last_stop')::int), 1) AS pct_early,
           median(delay_s) AS median_delay_s
    FROM s WHERE status = 'observed' GROUP BY ROLLUP (kind) ORDER BY kind NULLS LAST""")

# One row per trip run: its start, and its last-stop arrival (the rider's outcome).
c.sql("""
    CREATE VIEW runs AS
    SELECT service_date, trip_id, any_value(route_id) AS route_id,
           min(scheduled) FILTER (WHERE kind = 'first_stop') AS start_sched,
           any_value(delay_s) FILTER (WHERE kind = 'last_stop' AND status = 'observed') AS arr_delay,
           count(*) FILTER (WHERE kind <> 'last_stop' AND status = 'observed') AS tp_obs,
           count(*) FILTER (WHERE kind <> 'last_stop' AND is_late) AS tp_late
    FROM s GROUP BY ALL""")

trip_cols = """
    r.route_id, coalesce(t.trip_headsign, '') AS headsign, t.service_id,
    strftime(min(start_sched), '%H:%M') AS starts"""
trip_join = "FROM runs r LEFT JOIN trips t ON t.trip_id = r.trip_id"

show(f"Trips most often late at the last stop (>5 min; seen on >= {a.min_days} days)", f"""
    SELECT r.trip_id, {trip_cols},
           count(arr_delay) AS days_seen, count(*) FILTER (WHERE arr_delay > 300) AS days_late,
           round(median(arr_delay) / 60, 1) AS median_min_late, round(max(arr_delay) / 60, 1) AS worst_min
    {trip_join} GROUP BY ALL HAVING count(arr_delay) >= {a.min_days}
    ORDER BY days_late * 1.0 / days_seen DESC, median(arr_delay) DESC LIMIT 15""")

show("Trips with the largest share of late timepoint departures (>5 min)", f"""
    SELECT r.trip_id, {trip_cols},
           count(*) FILTER (WHERE tp_obs > 0) AS days_seen, sum(tp_late) AS late, sum(tp_obs) AS obs,
           round(100.0 * sum(tp_late) / sum(tp_obs), 0) AS pct_late
    {trip_join} GROUP BY ALL HAVING count(*) FILTER (WHERE tp_obs > 0) >= {a.min_days}
    ORDER BY pct_late DESC, obs DESC LIMIT 15""")

# From the visits, not the rollups: the rollups can't drop the visits filtered out above
# (their delay_hist puts everything under -300 s in one bin).
show("Routes by on-time % (all scored kinds, >= 200 observed)", """
    SELECT s.route_id, any_value(rt.route_short_name) AS name, any_value(rt.route_long_name) AS long_name,
           count(*) AS observed, round(100 * avg(is_on_time::int), 1) AS pct_on_time,
           round(100 * avg(is_late::int), 1) AS pct_late
    FROM s LEFT JOIN routes rt ON rt.route_id = s.route_id
    WHERE status = 'observed' GROUP BY s.route_id HAVING count(*) >= 200 ORDER BY pct_on_time LIMIT 10""")

# hour() of a TIMESTAMPTZ is local here (TimeZone is set above), as in rollup.py's by_hour.
show("Hour of day (scheduled, local), all routes", """
    SELECT hour(scheduled) AS hour, count(*) AS observed,
           round(100 * avg(is_on_time::int), 1) AS pct_on_time, round(100 * avg(is_late::int), 1) AS pct_late
    FROM s WHERE status = 'observed' GROUP BY 1 ORDER BY 1""")

show("Weekday vs weekend", """
    SELECT CASE WHEN dayofweek(service_date) IN (0, 6) THEN 'weekend' ELSE 'weekday' END AS day_type,
           count(DISTINCT service_date) AS dates, count(*) AS observed,
           round(100 * avg(is_on_time::int), 1) AS pct_on_time
    FROM s WHERE status = 'observed' GROUP BY ALL""")
