CREATE FUNCTION gtfs.service_time(service_date date, secs int, tz text)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT ((service_date + time '12:00') AT TIME ZONE tz)
         - interval '12 hours' + secs * interval '1 second'
$$;

CREATE FUNCTION gtfs.active_services(p_feed int, p_date date)
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

CREATE FUNCTION gtfs.scheduled_stop_visits(p_feed int, p_service_date date)
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
  ON f.feed_id = p_feed;
$$;
