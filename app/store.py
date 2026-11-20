import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, source BLOB NOT NULL, book BLOB NOT NULL,
  result TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  run_id TEXT NOT NULL REFERENCES runs(id), version INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL,
  PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS supplements (
  run_id TEXT NOT NULL REFERENCES runs(id),
  supplement_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL,
  changed INTEGER NOT NULL,
  frozen INTEGER NOT NULL,
  cleared_version INTEGER,
  created TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  PRIMARY KEY(run_id,supplement_id)
);
CREATE TABLE IF NOT EXISTS supplement_keys (
  run_id TEXT NOT NULL,
  supplement_id TEXT NOT NULL,
  key TEXT NOT NULL,
  changed INTEGER NOT NULL,
  baseline_amount TEXT NOT NULL,
  current_amount TEXT NOT NULL,
  PRIMARY KEY(run_id,supplement_id,key)
);
"""


@contextmanager
def connection():
    path = Path(settings.DB)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()
