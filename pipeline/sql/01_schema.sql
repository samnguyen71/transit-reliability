CREATE SCHEMA gtfs;

CREATE TABLE gtfs.feed (
  feed_id    serial PRIMARY KEY,
  sha256     text NOT NULL UNIQUE,     -- loading the same zip twice does nothing
  zip_path   text NOT NULL,
  fetched_at timestamptz NOT NULL,     -- from the file name
  timezone   text NOT NULL             -- agency.agency_timezone
);

CREATE TABLE gtfs.trips (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE,
  trip_id text,
  route_id text NOT NULL, service_id text NOT NULL,
  direction_id smallint, block_id text, shape_id text,
  PRIMARY KEY (feed_id, trip_id)
);

CREATE TABLE gtfs.stop_times (
  feed_id int, trip_id text, stop_sequence int,
  stop_id text NOT NULL,
  arrival_secs int, departure_secs int,   -- can exceed 86400; the spec allows NULL
  timepoint smallint,
  PRIMARY KEY (feed_id, trip_id, stop_sequence),
  FOREIGN KEY (feed_id, trip_id) REFERENCES gtfs.trips ON DELETE CASCADE
);

CREATE TABLE gtfs.calendar (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text,
  monday bool, tuesday bool, wednesday bool, thursday bool,
  friday bool, saturday bool, sunday bool,
  start_date date NOT NULL, end_date date NOT NULL,
  PRIMARY KEY (feed_id, service_id)
);

CREATE TABLE gtfs.calendar_dates (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text, date date,
  exception_type smallint NOT NULL CHECK (exception_type IN (1, 2)),
  PRIMARY KEY (feed_id, service_id, date)
);

CREATE INDEX ON gtfs.trips (feed_id, service_id);      -- date → trips
CREATE INDEX ON gtfs.calendar_dates (feed_id, date);   -- exceptions on a date
CREATE INDEX ON gtfs.stop_times (feed_id, stop_id);    -- visits at a stop (Phase 2)
