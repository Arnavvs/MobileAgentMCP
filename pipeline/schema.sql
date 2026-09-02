-- MobileAgentMCP data pipeline schema
--
-- Design rule that drives everything: the fact table is OBSERVATIONS, not
-- entities. The same reel or profile seen twice on different days is a
-- trajectory, not a duplicate to be collapsed - follower deltas and engagement
-- growth fall out for free, and nothing is ever silently overwritten.
--
-- Every table carries provenance (which run, which device, which app version)
-- so a wrong value can be traced to the run that produced it rather than
-- quietly poisoning the set.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One scrape run: a single session against one device.
CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  device_serial TEXT,
  device_model  TEXT,
  android       TEXT,
  transport     TEXT,               -- usb | wireless
  app           TEXT,               -- instagram | reddit | twitter
  app_version   TEXT,
  tool_version  TEXT,
  notes         TEXT
);

-- Per-step timings. Populated for EVERY step so the optimisation pass works
-- from measurements rather than guesses.
CREATE TABLE IF NOT EXISTS timings (
  run_id     TEXT NOT NULL REFERENCES runs(run_id),
  step       TEXT NOT NULL,         -- open_profile | scan_grid | reel_details ...
  target     TEXT,                  -- handle / query / timeline
  seconds    REAL NOT NULL,
  items      INTEGER,               -- how many things the step produced
  bytes      INTEGER,               -- for transport steps
  started_at TEXT,
  PRIMARY KEY (run_id, step, target, started_at)
);

-- Accounts. One row per handle per platform; history lives in observations.
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,      -- platform:handle
  platform   TEXT NOT NULL,
  handle     TEXT NOT NULL,
  first_seen TEXT,
  last_seen  TEXT,
  UNIQUE (platform, handle)
);

-- A profile as observed at a moment in time. Re-scraping appends.
CREATE TABLE IF NOT EXISTS profile_observations (
  obs_id       TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL REFERENCES accounts(account_id),
  run_id       TEXT REFERENCES runs(run_id),
  observed_at  TEXT NOT NULL,
  display_name TEXT,
  bio          TEXT,
  external_link TEXT,
  followers    INTEGER,
  following    INTEGER,
  post_count   INTEGER,
  verified     INTEGER,
  is_private   INTEGER,
  raw_json     TEXT
);

-- Content items: posts, reels, tweets, reddit posts.
CREATE TABLE IF NOT EXISTS items (
  item_id     TEXT PRIMARY KEY,     -- platform:shortcode | synthetic hash
  platform    TEXT NOT NULL,
  kind        TEXT NOT NULL,        -- post | reel | tweet | reddit_post
  account_id  TEXT REFERENCES accounts(account_id),
  shortcode   TEXT,                 -- canonical id when known
  permalink   TEXT,
  first_seen  TEXT,
  last_seen   TEXT,
  times_seen  INTEGER DEFAULT 1
);

-- An item as observed at a moment. Engagement counts live HERE, not on items,
-- because they change - that change is the interesting signal.
CREATE TABLE IF NOT EXISTS item_observations (
  obs_id      TEXT PRIMARY KEY,
  item_id     TEXT NOT NULL REFERENCES items(item_id),
  run_id      TEXT REFERENCES runs(run_id),
  observed_at TEXT NOT NULL,
  caption     TEXT,
  audio       TEXT,
  location    TEXT,
  posted_age  TEXT,                 -- "3h", "6 June" as displayed
  likes       INTEGER,
  comments    INTEGER,
  shares      INTEGER,
  saves       INTEGER,
  reposts     INTEGER,
  views       INTEGER,              -- impressions on X, view count on IG reels
  media_kind  TEXT,
  media_count INTEGER,
  is_ad       INTEGER DEFAULT 0,
  source      TEXT,                 -- profile_grid | feed | search | timeline
  raw_json    TEXT
);

-- Comments / replies, with nesting where the platform declares it.
CREATE TABLE IF NOT EXISTS comments (
  comment_id  TEXT PRIMARY KEY,
  item_id     TEXT REFERENCES items(item_id),
  run_id      TEXT REFERENCES runs(run_id),
  observed_at TEXT,
  author      TEXT,
  text        TEXT,
  likes       INTEGER,
  posted_age  TEXT,
  depth       INTEGER,
  depth_source TEXT,                -- declared | indent | unknown
  parent_id   TEXT REFERENCES comments(comment_id),
  hidden_replies INTEGER,           -- collapsed count, completeness signal
  raw_json    TEXT
);

-- Media files on disk. Kept separate so the DB stays small and portable.
CREATE TABLE IF NOT EXISTS media (
  media_id    TEXT PRIMARY KEY,
  item_id     TEXT REFERENCES items(item_id),
  run_id      TEXT REFERENCES runs(run_id),
  kind        TEXT,                 -- reel_video | screenshot | thumbnail
  local_path  TEXT NOT NULL,
  bytes       INTEGER,
  duration_s  REAL,
  video_s     REAL,
  audio_s     REAL,
  acquired    TEXT,                 -- screen_capture | cdn_download
  pull_seconds REAL,                -- transport cost, for the pipeline budget
  sha1        TEXT,
  created_at  TEXT
);

-- Analysis output, appended not overwritten, tagged with the pipeline version
-- that produced it so results stay reproducible across model changes.
CREATE TABLE IF NOT EXISTS analysis (
  analysis_id TEXT PRIMARY KEY,
  item_id     TEXT REFERENCES items(item_id),
  media_id    TEXT REFERENCES media(media_id),
  kind        TEXT,                 -- transcript | vlm_caption | embedding | ocr
  model       TEXT,
  value       TEXT,
  confidence  REAL,
  pipeline_version TEXT,
  created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_obs_item ON item_observations(item_id);
CREATE INDEX IF NOT EXISTS idx_item_obs_time ON item_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_prof_obs_acct ON profile_observations(account_id);
CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(item_id);
CREATE INDEX IF NOT EXISTS idx_media_item    ON media(item_id);
CREATE INDEX IF NOT EXISTS idx_timings_run   ON timings(run_id);

-- Full-text search over the things you actually search by.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  caption, audio, location, handle, content=''
);
