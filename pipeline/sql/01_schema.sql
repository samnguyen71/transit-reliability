CREATE SCHEMA IF NOT EXISTS gtfs;

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
  pickup_type int, drop_off_type int,
  PRIMARY KEY (feed_id, trip_id, stop_sequence),
  FOREIGN KEY (feed_id, trip_id) REFERENCES gtfs.trips ON DELETE CASCADE
);

CREATE TABLE gtfs.calendar (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text,
  monday bool NOT NULL, tuesday bool NOT NULL, wednesday bool NOT NULL, thursday bool NOT NULL,
  friday bool NOT NULL, saturday bool NOT NULL, sunday bool NOT NULL,
  start_date date NOT NULL, end_date date NOT NULL,
  PRIMARY KEY (feed_id, service_id)
);

CREATE TABLE gtfs.calendar_dates (
  feed_id int REFERENCES gtfs.feed ON DELETE CASCADE, service_id text, date date,
  exception_type smallint NOT NULL CHECK (exception_type IN (1, 2)),
  PRIMARY KEY (feed_id, service_id, date)
);

CREATE TABLE agency (
    agency_id text, agency_name text NOT NULL, agency_url text NOT NULL,
    agency_timezone text NOT NULL,
    PRIMARY KEY (feed_id, agency_id)
);

CREATE TABLE routes (
    route_id text NOT NULL, agency_id text,
    route_short_name text, route_long_name text, route_type int NOT NULL,
    PRIMARY KEY (feed_id, route_id)
);

CREATE TABLE stop (
  stop_id text, stop_name text NOT NULL,
  stop_lat real NOT NULL, stop_lon real NOT NULL,
  parent_station text,
  PRIMARY KEY (feed_id, stop_id)
  FOREIGN KEY (stop_id) REFERENCES gtfs.stop_times ON DELETE CASCADE
)

CREATE INDEX ON gtfs.trips (feed_id, service_id);      -- date → trips
CREATE INDEX ON gtfs.calendar_dates (feed_id, date);   -- exceptions on a date
CREATE INDEX ON gtfs.stop_times (feed_id, stop_id);    -- visits at a stop (Phase 2)
