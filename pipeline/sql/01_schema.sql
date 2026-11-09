CREATE SCHEMA IF NOT EXISTS gtfs;

CREATE TABLE IF NOT EXISTS gtfs.feed (
  feed_id    serial PRIMARY KEY,
  sha256     text NOT NULL UNIQUE,     -- loading the same zip twice does nothing
  zip_path   text NOT NULL,
  fetched_at timestamptz NOT NULL,     -- from the file name
  timezone   text NOT NULL             -- agency.agency_timezone
);

CREATE TABLE IF NOT EXISTS gtfs.agency (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE,
  agency_id text NOT NULL DEFAULT '',  -- '' when the feed omits it (single-agency feeds may)
  agency_name text NOT NULL,
  agency_url text NOT NULL, agency_timezone text NOT NULL,
  PRIMARY KEY (feed_id, agency_id)
);

CREATE TABLE IF NOT EXISTS gtfs.routes (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, route_id text NOT NULL, agency_id text,
  route_short_name text, route_long_name text, route_type int NOT NULL,
  PRIMARY KEY (feed_id, route_id)
);

CREATE TABLE IF NOT EXISTS gtfs.stops (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, stop_id text, stop_name text NOT NULL,
  stop_lat double precision NOT NULL, stop_lon double precision NOT NULL,
  parent_station text,
  PRIMARY KEY (feed_id, stop_id)
);

CREATE TABLE IF NOT EXISTS gtfs.trips (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE,
  trip_id text,
  route_id text NOT NULL, service_id text NOT NULL,
  direction_id smallint, block_id text, shape_id text,
  PRIMARY KEY (feed_id, trip_id)
);

CREATE TABLE IF NOT EXISTS gtfs.stop_times (
  feed_id int, trip_id text, stop_sequence int,
  stop_id text NOT NULL,
  arrival_secs int, departure_secs int,   -- can exceed 86400; the spec allows NULL
  timepoint smallint,
  pickup_type int, drop_off_type int,
  PRIMARY KEY (feed_id, trip_id, stop_sequence),
  FOREIGN KEY (feed_id, trip_id) REFERENCES gtfs.trips ON DELETE CASCADE,
  FOREIGN KEY (feed_id, stop_id) REFERENCES gtfs.stops ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gtfs.calendar (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text,
  monday bool NOT NULL, tuesday bool NOT NULL, wednesday bool NOT NULL, thursday bool NOT NULL,
  friday bool NOT NULL, saturday bool NOT NULL, sunday bool NOT NULL,
  start_date date NOT NULL, end_date date NOT NULL,
  PRIMARY KEY (feed_id, service_id)
);

CREATE TABLE IF NOT EXISTS gtfs.calendar_dates (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text, date date,
  exception_type smallint NOT NULL CHECK (exception_type IN (1, 2)),
  PRIMARY KEY (feed_id, service_id, date)
);

CREATE INDEX IF NOT EXISTS trips_service_idx ON gtfs.trips (feed_id, service_id);        -- date → trips
CREATE INDEX IF NOT EXISTS calendar_dates_date_idx ON gtfs.calendar_dates (feed_id, date); -- exceptions on a date
CREATE INDEX IF NOT EXISTS stop_times_stop_idx ON gtfs.stop_times (feed_id, stop_id);    -- visits at a stop (Phase 2)
