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

CREATE FUNCTION gtfs.scheduled_stop_visits(feed, service_date)
