CREATE OR REPLACE FUNCTION gtfs.service_time(service_date date, secs int, tz text)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT ((service_date + time '12:00') AT TIME ZONE tz)
         - interval '12 hours' + secs * interval '1 second'
$$;

CREATE OR REPLACE FUNCTION gtfs.active_services(p_feed int, p_date date)
RETURNS TABLE (service_id text) LANGUAGE sql STABLE AS $$
  (SELECT c.service_id FROM gtfs.calendar c
    WHERE c.feed_id = p_feed
      AND p_date BETWEEN c.start_date AND c.end_date
      AND (ARRAY[c.monday, c.tuesday, c.wednesday, c.thursday,
                 c.friday, c.saturday, c.sunday])[extract(isodow FROM p_date)::int]
   EXCEPT
   SELECT d.service_id FROM gtfs.calendar_dates d
    WHERE d.feed_id = p_feed AND d.date = p_date AND d.exception_type = 2)
  UNION
  SELECT d.service_id FROM gtfs.calendar_dates d
   WHERE d.feed_id = p_feed AND d.date = p_date AND d.exception_type = 1
$$;

CREATE OR REPLACE FUNCTION gtfs.scheduled_stop_visits(p_feed int, p_service_date date)
RETURNS TABLE (
    service_date date,
    trip_id text,
    route_id text,
    direction_id smallint,
    stop_sequence int,
    stop_id text,
    arrival_secs int,
    departure_secs int,
    arrival_at timestamptz,
    departure_at timestamptz
)
LANGUAGE sql STABLE AS $$
SELECT
    p_service_date,
    t.trip_id,
    t.route_id,
    t.direction_id,
    st.stop_sequence,
    st.stop_id,
    st.arrival_secs,
    st.departure_secs,
    gtfs.service_time(
        p_service_date,
        st.arrival_secs,
        f.timezone
    ) AS arrival_at,
    gtfs.service_time(
        p_service_date,
        st.departure_secs,
        f.timezone
    ) AS departure_at
FROM gtfs.active_services(p_feed, p_service_date) s
JOIN gtfs.trips t
  ON t.feed_id = p_feed
 AND t.service_id = s.service_id
JOIN gtfs.stop_times st
  ON st.feed_id = t.feed_id
 AND st.trip_id = t.trip_id
JOIN gtfs.feed f
  ON f.feed_id = p_feed
-- Aliased trips are copies of a canonical trip that is already listed.
WHERE NOT EXISTS (SELECT 1 FROM gtfs.trip_alias a
                  WHERE a.feed_id = t.feed_id AND a.trip_id = t.trip_id);
$$;

-- Record every exact +24 h copy of a trip as an alias of the earlier copy.
-- Strict on purpose: same route, service and direction, same stop_sequences and
-- stops, and every arrival and departure exactly 86400 s later (blank stays blank).
-- Returns the number of aliases recorded.
CREATE OR REPLACE FUNCTION gtfs.mark_overnight_twins(p_feed int)
RETURNS int LANGUAGE sql AS $$
WITH sig AS (
    SELECT t.trip_id, t.route_id, t.service_id, t.direction_id,
           array_agg(st.stop_sequence ORDER BY st.stop_sequence) AS seqs,
           array_agg(st.stop_id ORDER BY st.stop_sequence) AS stops,
           array_agg(st.arrival_secs ORDER BY st.stop_sequence) AS arr,
           array_agg(st.departure_secs ORDER BY st.stop_sequence) AS dep,
           array_agg(st.arrival_secs - 86400 ORDER BY st.stop_sequence) AS arr_back,
           array_agg(st.departure_secs - 86400 ORDER BY st.stop_sequence) AS dep_back
    FROM gtfs.trips t
    JOIN gtfs.stop_times st ON st.feed_id = t.feed_id AND st.trip_id = t.trip_id
    WHERE t.feed_id = p_feed
    GROUP BY t.trip_id, t.route_id, t.service_id, t.direction_id
), ins AS (
    INSERT INTO gtfs.trip_alias (feed_id, trip_id, canonical_trip_id, reason)
    SELECT p_feed, late.trip_id, early.trip_id, 'overnight_twin_24h'
    FROM sig late
    JOIN sig early
      ON early.route_id = late.route_id
     AND early.service_id = late.service_id
     AND early.direction_id IS NOT DISTINCT FROM late.direction_id
     AND early.seqs = late.seqs
     AND early.stops = late.stops
     AND early.arr = late.arr_back
     AND early.dep = late.dep_back
    RETURNING 1
)
SELECT count(*)::int FROM ins;
$$;

-- The trip a realtime trip_id should be scored as. Keep realtime's start_date as is:
-- DRT already sends the calendar date, which is the canonical (early) trip's service date.
CREATE OR REPLACE FUNCTION gtfs.canonical_trip_id(p_feed int, p_trip_id text)
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT a.canonical_trip_id FROM gtfs.trip_alias a
      WHERE a.feed_id = p_feed AND a.trip_id = p_trip_id),
    p_trip_id)
$$;

CREATE OR REPLACE FUNCTION gtfs.parse_secs(val text)
RETURNS integer AS $$
SELECT CASE 
    WHEN val IS NULL OR val = '' THEN NULL
    ELSE (split_part(val, ':', 1)::integer * 3600) + 
         (split_part(val, ':', 2)::integer * 60) + 
         (split_part(val, ':', 3)::integer)
END;
$$ LANGUAGE sql IMMUTABLE;