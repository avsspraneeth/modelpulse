#!/usr/bin/env python3
"""
ModelPulse — local LLM usage dashboard for oMLX + Hermes Agent.

Universal design:
  * Token usage is sourced from oMLX (the OpenAI-compatible server on :8000),
    which every local-model harness (Hermes, Codex, Claude Code, ...) flows
    through. We do NOT query each harness for tokens.
  * Attribution ("what is using it") is sourced from each harness's session
    store. Hermes (state.db) is wired now; others can be added later.
  * The app keeps its OWN persistent SQLite DB (pulse.db) and ingests from the
    sources above, so history survives oMLX's 90-day stats deletion.

Data sources ingested into pulse.db:
  requests      <- tail ~/.omlx/logs/server.log  ("Chat completion:" lines)
  hourly_usage  <- ~/.omlx/usage.sqlite3         (model_usage_hourly)
  model_totals  <- ~/.omlx/stats.json            (per_model cumulative)
  sessions      <- ~/.hermes/state.db            (sessions + session_model_usage)
  snapshots     <- live sampler (tok/s, memory) every SAMPLE_INTERVAL seconds

Run:  python3 modelpulse.py            (serves on :8787)
      python3 modelpulse.py --port 9000
      python3 modelpulse.py --collect  (one-shot ingest, no server)
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"

# Defaults — the standard oMLX + Hermes layout. Override in config.json, via
# env vars, or CLI flags. Precedence: CLI > env > config.json > defaults.
DEFAULTS = {
    "port": 8787,
    "splash_url": "http://localhost:8000",
    "omlx_home": "~/.omlx",
    "omlx_log": None,          # default: {omlx_home}/logs/server.log
    "omlx_usage_db": None,     # default: {omlx_home}/usage.sqlite3
    "omlx_stats": None,        # default: {omlx_home}/stats.json
    "hermes_state_db": "auto",  # "auto" = most recently active profile; or an explicit path
    "db_path": "~/modelpulse/pulse.db",
    "sample_interval": 2.0,
    "decay_tau": 8.0,
    "live_window": 600,
}

# Effective values (set by apply_config). Kept as module globals so the rest
# of the code reads them without change.
DB_PATH = BASE / "pulse.db"
OMLX_HOME = Path.home() / ".omlx"
OMLX_LOG = OMLX_HOME / "logs" / "server.log"
OMLX_USAGE_DB = OMLX_HOME / "usage.sqlite3"
OMLX_STATS = OMLX_HOME / "stats.json"
HERMES_STATE = Path.home() / ".hermes" / "state.db"
SPLASH_URL = "http://localhost:8000"
PORT = 8787
SAMPLE_INTERVAL = 2.0          # seconds between live samples
DECAY_TAU = 8.0               # seconds, exponential decay time-constant for live tok/s
LIVE_WINDOW = 600             # seconds of live chart history to keep/serve

log = logging.getLogger("modelpulse")

# Set in main() so the HTTP handler can trigger a live config reload.
COLLECTOR = None


def _expand(p):
    return str(Path(str(p)).expanduser())


def detect_hermes_state_db():
    """Find the most recently active Hermes profile's state.db.

    Scans the default profile plus every profile under ~/.hermes/profiles/ and
    returns the one with the newest session. This is the profile the user is
    actually chatting in, so the token breakdown matches the Hermes UI.
    """
    candidates = [Path.home() / ".hermes" / "state.db"]
    profiles_dir = Path.home() / ".hermes" / "profiles"
    if profiles_dir.is_dir():
        candidates += [p / "state.db" for p in sorted(profiles_dir.iterdir()) if p.is_dir()]
    best, best_ts = None, 0.0
    for c in candidates:
        if not c.exists():
            continue
        ts = 0.0
        try:
            con = sqlite3.connect(f"file:{c}?mode=ro", uri=True)
            r = con.execute("SELECT COALESCE(MAX(started_at),0) FROM sessions").fetchone()
            con.close()
            ts = float(r[0] or 0)
        except Exception:  # noqa: BLE001
            ts = c.stat().st_mtime
        if ts > best_ts:
            best, best_ts = c, ts
    return best


def load_config(cli_port=None):
    """Merge defaults <- config.json <- env vars <- CLI. Returns a dict."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            for k in DEFAULTS:
                if k in user and user[k] not in (None, ""):
                    cfg[k] = user[k]
        except Exception as e:  # noqa: BLE001
            log.warning("config.json unreadable (%s); using defaults", e)
    env_map = {
        "port": "MODELPULE_PORT",
        "splash_url": "SPLASH_URL",
        "omlx_home": "OMLX_HOME",
        "omlx_log": "OMLX_LOG",
        "omlx_usage_db": "OMLX_USAGE_DB",
        "omlx_stats": "OMLX_STATS",
        "hermes_state_db": "HERMES_STATE_DB",
        "db_path": "MODELPULE_DB",
    }
    for k, env in env_map.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    if cli_port:
        cfg["port"] = cli_port
    # Resolve "auto" for the Hermes DB -> most recently active profile.
    if cfg["hermes_state_db"] == "auto":
        detected = detect_hermes_state_db()
        cfg["hermes_state_db"] = str(detected) if detected else str(Path.home() / ".hermes" / "state.db")
    for k in ("omlx_home", "omlx_log", "omlx_usage_db", "omlx_stats",
              "hermes_state_db", "db_path"):
        if cfg[k]:
            cfg[k] = _expand(cfg[k])
    return cfg


def apply_config(cfg):
    """Push the effective config into the module globals the code reads."""
    global DB_PATH, OMLX_HOME, OMLX_LOG, OMLX_USAGE_DB, OMLX_STATS
    global HERMES_STATE, SPLASH_URL, PORT, SAMPLE_INTERVAL, DECAY_TAU, LIVE_WINDOW
    OMLX_HOME = Path(cfg["omlx_home"])
    DB_PATH = Path(cfg["db_path"])
    OMLX_LOG = Path(cfg["omlx_log"]) if cfg["omlx_log"] else OMLX_HOME / "logs" / "server.log"
    OMLX_USAGE_DB = Path(cfg["omlx_usage_db"]) if cfg["omlx_usage_db"] else OMLX_HOME / "usage.sqlite3"
    OMLX_STATS = Path(cfg["omlx_stats"]) if cfg["omlx_stats"] else OMLX_HOME / "stats.json"
    HERMES_STATE = Path(cfg["hermes_state_db"])
    SPLASH_URL = cfg["splash_url"]
    PORT = int(cfg["port"])
    SAMPLE_INTERVAL = float(cfg["sample_interval"])
    DECAY_TAU = float(cfg["decay_tau"])
    LIVE_WINDOW = int(cfg["live_window"])


def write_default_config():
    """Write a self-documenting config.json if none exists. Returns True if written."""
    if CONFIG_PATH.exists():
        return False
    docs = {
        "port": "Dashboard HTTP port (default 8787)",
        "splash_url": "oMLX/Splash OpenAI server base URL (default http://localhost:8000)",
        "omlx_home": "oMLX home dir holding logs/, usage.sqlite3, stats.json (default ~/.omlx)",
        "omlx_log": "Optional: exact path to the oMLX server log (default {omlx_home}/logs/server.log)",
        "omlx_usage_db": "Optional: exact path to oMLX usage.sqlite3 (default {omlx_home}/usage.sqlite3)",
        "omlx_stats": "Optional: exact path to oMLX stats.json (default {omlx_home}/stats.json)",
        "hermes_state_db": "Hermes state.db (default ~/.hermes/state.db; use ~/.hermes/profiles/<name>/state.db for a profile)",
        "db_path": "Where ModelPulse stores its own persistent DB (default ~/modelpulse/pulse.db)",
        "sample_interval": "Seconds between live samples (default 2.0)",
        "decay_tau": "Live tok/s decay time-constant, seconds (default 8.0)",
        "live_window": "Live chart window, seconds (default 600)",
    }
    out = {"_docs": docs}
    out.update({k: v for k, v in DEFAULTS.items() if v is not None})
    with open(CONFIG_PATH, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    return True


def check_config():
    """Print a diagnostics report: each source path + whether it's usable."""
    def line(label, ok, detail):
        print(f"  [{'OK  ' if ok else 'MISS'}] {label}: {detail}")
    print("ModelPulse configuration")
    print(f"  dashboard port : {PORT}  ->  http://127.0.0.1:{PORT}")
    print(f"  own DB         : {DB_PATH}")
    print(f"  oMLX home      : {OMLX_HOME}")
    line("server.log", OMLX_LOG.exists(), str(OMLX_LOG))
    line("usage.sqlite3", OMLX_USAGE_DB.exists(), str(OMLX_USAGE_DB))
    line("stats.json", OMLX_STATS.exists(), str(OMLX_STATS))
    print(f"  Hermes state.db: {HERMES_STATE}")
    line("state.db", HERMES_STATE.exists(), str(HERMES_STATE))
    try:
        req = urllib.request.Request(SPLASH_URL + "/health")
        with urllib.request.urlopen(req, timeout=4) as resp:
            d = json.load(resp)
        line("splash /health", True,
             f"{SPLASH_URL} (models={d.get('engine_pool', {}).get('model_count', '?')})")
    except Exception as e:  # noqa: BLE001
        line("splash /health", False, f"{SPLASH_URL} ({e})")


def reload_config():
    """Re-read config.json and apply it live. Returns (cfg, changed_keys).

    Re-points the ingester globals. If the oMLX log path changed, resets the
    tailer so it reopens the new file on its next poll. Port changes are
    reported but NOT applied (the HTTP server is already bound) — the caller
    should tell the user to restart for those.
    """
    global COLLECTOR
    old = {
        "omlx_log": str(OMLX_LOG),
        "omlx_usage_db": str(OMLX_USAGE_DB),
        "omlx_stats": str(OMLX_STATS),
        "hermes_state_db": str(HERMES_STATE),
        "splash_url": SPLASH_URL,
        "port": PORT,
        "db_path": str(DB_PATH),
    }
    cfg = load_config()
    apply_config(cfg)
    changed = []
    if str(OMLX_LOG) != old["omlx_log"]:
        changed.append("omlx_log")
        if COLLECTOR:
            COLLECTOR.tailer.reset()
    if str(OMLX_USAGE_DB) != old["omlx_usage_db"]:
        changed.append("omlx_usage_db")
    if str(OMLX_STATS) != old["omlx_stats"]:
        changed.append("omlx_stats")
    if str(HERMES_STATE) != old["hermes_state_db"]:
        changed.append("hermes_state_db")
    if SPLASH_URL != old["splash_url"]:
        changed.append("splash_url")
    if str(DB_PATH) != old["db_path"]:
        changed.append("db_path")
    needs_restart = PORT != old["port"]
    log.info("config reloaded (changed=%s, needs_restart=%s)", changed, needs_restart)
    return cfg, changed, needs_restart


def source_status():
    """Per-source OK/MISS report (used by the settings page + /api/config)."""
    def one(label, path, exists):
        return {"label": label, "path": str(path), "ok": bool(exists)}
    out = [
        one("oMLX server.log", OMLX_LOG, OMLX_LOG.exists()),
        one("oMLX usage.sqlite3", OMLX_USAGE_DB, OMLX_USAGE_DB.exists()),
        one("oMLX stats.json", OMLX_STATS, OMLX_STATS.exists()),
        one("Hermes state.db", HERMES_STATE, HERMES_STATE.exists()),
        one("ModelPulse DB", DB_PATH, DB_PATH.exists()),
    ]
    splash_ok, splash_detail = False, SPLASH_URL
    try:
        req = urllib.request.Request(SPLASH_URL + "/health")
        with urllib.request.urlopen(req, timeout=4) as resp:
            d = json.load(resp)
        splash_ok = True
        splash_detail = f"{SPLASH_URL} (models={d.get('engine_pool', {}).get('model_count', '?')})"
    except Exception as e:  # noqa: BLE001
        splash_detail = f"{SPLASH_URL} ({e})"
    out.append({"label": "Splash /health", "path": SPLASH_URL, "ok": splash_ok, "detail": splash_detail})
    return out

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    ts           REAL NOT NULL,
    model        TEXT NOT NULL,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    total_time   REAL NOT NULL DEFAULT 0,
    decode_tps   REAL NOT NULL DEFAULT 0,
    ttft         REAL,
    finish_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);

CREATE TABLE IF NOT EXISTS hourly_usage (
    timestamp_hour INTEGER NOT NULL,
    model_id       TEXT NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    prefill_seconds REAL NOT NULL DEFAULT 0,
    generation_seconds REAL NOT NULL DEFAULT 0,
    request_seconds REAL NOT NULL DEFAULT 0,
    timed_requests INTEGER NOT NULL DEFAULT 0,
    ingested_at    REAL NOT NULL,
    PRIMARY KEY (timestamp_hour, model_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS model_totals (
    model_id       TEXT PRIMARY KEY,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    prefill_duration REAL NOT NULL DEFAULT 0,
    generation_duration REAL NOT NULL DEFAULT 0,
    ingested_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    source         TEXT,
    model          TEXT,
    started_at     REAL,
    ended_at       REAL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    display_name   TEXT,
    ingested_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    ts             REAL PRIMARY KEY,
    tps            REAL NOT NULL DEFAULT 0,
    wired_bytes    INTEGER NOT NULL DEFAULT 0,
    model_bytes    INTEGER NOT NULL DEFAULT 0,
    ceiling_bytes  INTEGER NOT NULL DEFAULT 0,
    loaded_model   TEXT,
    active_requests INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db() -> None:
    con = get_db()
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    log.info("DB ready at %s", DB_PATH)


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(con, key, value):
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# Ingesters
# --------------------------------------------------------------------------- #
COMPLETION_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - omlx\.server - INFO - \[-\] - "
    r"Chat completion: model=(?P<model>[^,]+), (?P<completion>\d+) tokens in "
    r"(?P<total>[\d.]+)s \((?P<tps>[\d.]+) tok/s\), prompt: (?P<prompt>\d+), "
    r"finish_reason=(?P<finish>\w+)"
    r"(?:, max_tokens=(?P<maxtok>\S+))?"
    r"(?:, request_max_tokens=(?P<reqmax>\S+))?"
    r"(?:, stream_model_ttft=(?P<ttft>[\d.]+)s)?"
)


def parse_completion_line(line: str):
    m = COMPLETION_RE.match(line.strip())
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        return None
    return {
        "ts": ts,
        "model": m.group("model").strip(),
        "completion_tokens": int(m.group("completion")),
        "prompt_tokens": int(m.group("prompt")),
        "total_time": float(m.group("total")),
        "decode_tps": float(m.group("tps")),
        "ttft": float(m.group("ttft")) if m.group("ttft") else None,
        "finish_reason": m.group("finish"),
    }


def request_id_for(rec: dict) -> str:
    # Natural key: precise timestamp + model + sizes. Stable across re-ingest.
    return f"{rec['ts']:.3f}|{rec['model']}|{rec['completion_tokens']}|{rec['prompt_tokens']}"


def ingest_requests_backfill(con) -> int:
    """Parse the whole existing server.log once (deduped by request_id)."""
    if not OMLX_LOG.exists():
        return 0
    n = 0
    with open(OMLX_LOG, "r", errors="replace") as f:
        for line in f:
            if "Chat completion:" not in line:
                continue
            rec = parse_completion_line(line)
            if not rec:
                continue
            rid = request_id_for(rec)
            con.execute(
                "INSERT OR IGNORE INTO requests(request_id,ts,model,completion_tokens,"
                "prompt_tokens,total_time,decode_tps,ttft,finish_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, rec["ts"], rec["model"], rec["completion_tokens"],
                 rec["prompt_tokens"], rec["total_time"], rec["decode_tps"],
                 rec["ttft"], rec["finish_reason"]),
            )
            n += 1
    con.commit()
    return n


class LogTailer:
    """Follows the oMLX server log, emitting new completion records."""

    def __init__(self):
        self._fh = None
        self._inode = None
        self._offset = 0
        self._last_record = None  # most recent parsed record (for live tok/s)

    def _open(self, from_start: bool):
        if not OMLX_LOG.exists():
            return
        st = OMLX_LOG.stat()
        self._fh = open(OMLX_LOG, "r", errors="replace")
        self._inode = st.st_ino
        self._offset = 0 if from_start else st.st_size
        self._fh.seek(self._offset)

    def resume(self, con):
        """Resume from saved offset if same file, else backfill from start."""
        saved_inode = meta_get(con, "log_inode")
        saved_offset = meta_get(con, "log_offset", "0")
        if not OMLX_LOG.exists():
            return
        st = OMLX_LOG.stat()
        same_file = saved_inode and int(saved_inode) == st.st_ino
        if same_file and st.st_size >= int(saved_offset):
            self._open(from_start=False)
            self._fh.seek(int(saved_offset))
            log.info("log tailer resumed at offset %d", self._offset)
        else:
            # New/rotated file: backfill from start (deduped), then follow.
            n = ingest_requests_backfill(con)
            self._open(from_start=False)  # seek to end for live
            log.info("log tailer backfilled %d historical requests", n)

    def poll(self, con):
        """Read new lines; return list of new records."""
        new = []
        if self._fh is None:
            self._open(from_start=False)
            return new
        try:
            # Detect rotation/truncation.
            st = OMLX_LOG.stat()
            if st.st_ino != self._inode or st.st_size < self._fh.tell():
                self._fh.close()
                self._open(from_start=False)
                return new
            for line in self._fh:
                if "Chat completion:" not in line:
                    continue
                rec = parse_completion_line(line)
                if not rec:
                    continue
                rid = request_id_for(rec)
                cur = con.execute(
                    "INSERT OR IGNORE INTO requests(request_id,ts,model,completion_tokens,"
                    "prompt_tokens,total_time,decode_tps,ttft,finish_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (rid, rec["ts"], rec["model"], rec["completion_tokens"],
                     rec["prompt_tokens"], rec["total_time"], rec["decode_tps"],
                     rec["ttft"], rec["finish_reason"]),
                )
                if cur.rowcount:
                    new.append(rec)
                    self._last_record = rec
            self._offset = self._fh.tell()
            meta_set(con, "log_inode", self._inode)
            meta_set(con, "log_offset", self._offset)
            con.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("log tailer error: %s", e)
        return new

    @property
    def last_record(self):
        return self._last_record

    def reset(self):
        """Drop the open handle so the next poll() reopens (used after a
        config change points OMLX_LOG at a different file)."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        self._fh = None
        self._inode = None
        self._offset = 0


def ingest_hourly_usage(con) -> int:
    """Copy oMLX hourly rows into our persistent table (upsert, never delete)."""
    if not OMLX_USAGE_DB.exists():
        return 0
    try:
        src = sqlite3.connect(f"file:{OMLX_USAGE_DB}?mode=ro", uri=True)
        rows = src.execute(
            "SELECT timestamp_hour, model_id, requests, prompt_tokens, "
            "completion_tokens, cached_tokens, prefill_seconds, generation_seconds, "
            "request_seconds, timed_requests FROM model_usage_hourly"
        ).fetchall()
        src.close()
    except Exception as e:  # noqa: BLE001
        log.warning("hourly ingest failed: %s", e)
        return 0
    now = time.time()
    n = 0
    for r in rows:
        con.execute(
            "INSERT INTO hourly_usage(timestamp_hour,model_id,requests,prompt_tokens,"
            "completion_tokens,cached_tokens,prefill_seconds,generation_seconds,"
            "request_seconds,timed_requests,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(timestamp_hour,model_id) DO UPDATE SET "
            "requests=excluded.requests, prompt_tokens=excluded.prompt_tokens, "
            "completion_tokens=excluded.completion_tokens, cached_tokens=excluded.cached_tokens, "
            "prefill_seconds=excluded.prefill_seconds, generation_seconds=excluded.generation_seconds, "
            "request_seconds=excluded.request_seconds, timed_requests=excluded.timed_requests, "
            "ingested_at=excluded.ingested_at",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], now),
        )
        n += 1
    con.commit()
    return n


def ingest_model_totals(con) -> int:
    """Copy oMLX stats.json per_model cumulative totals (upsert)."""
    if not OMLX_STATS.exists():
        return 0
    try:
        with open(OMLX_STATS) as f:
            stats = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("stats.json read failed: %s", e)
        return 0
    now = time.time()
    n = 0
    for model, d in stats.get("per_model", {}).items():
        con.execute(
            "INSERT INTO model_totals(model_id,prompt_tokens,completion_tokens,cached_tokens,"
            "requests,prefill_duration,generation_duration,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(model_id) DO UPDATE SET "
            "prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens, "
            "cached_tokens=excluded.cached_tokens, requests=excluded.requests, "
            "prefill_duration=excluded.prefill_duration, "
            "generation_duration=excluded.generation_duration, ingested_at=excluded.ingested_at",
            (model, d.get("prompt_tokens", 0), d.get("completion_tokens", 0),
             d.get("cached_tokens", 0), d.get("requests", 0),
             d.get("prefill_duration", 0), d.get("generation_duration", 0), now),
        )
        n += 1
    con.commit()
    return n


def ingest_sessions(con) -> int:
    """Copy Hermes sessions + per-model usage into our persistent table (upsert)."""
    if not HERMES_STATE.exists():
        return 0
    # If the active profile changed, drop the old profile's sessions so the
    # breakdown always reflects the profile the user is actually in.
    profile_key = str(HERMES_STATE)
    prev = meta_get(con, "hermes_profile")
    if prev is None or prev != profile_key:
        con.execute("DELETE FROM sessions")
        if prev:
            log.info("Hermes profile changed %s -> %s; cleared sessions", prev, profile_key)
    meta_set(con, "hermes_profile", profile_key)
    try:
        src = sqlite3.connect(f"file:{HERMES_STATE}?mode=ro", uri=True)
        rows = src.execute(
            "SELECT s.id, s.source, s.model, s.started_at, s.ended_at, "
            "COALESCE(s.input_tokens,0), COALESCE(s.output_tokens,0), "
            "COALESCE(s.cache_read_tokens,0), COALESCE(s.cache_write_tokens,0), "
            "COALESCE(s.reasoning_tokens,0), COALESCE(s.actual_cost_usd,0), "
            "s.display_name FROM sessions s"
        ).fetchall()
        src.close()
    except Exception as e:  # noqa: BLE001
        log.warning("sessions ingest failed: %s", e)
        return 0
    now = time.time()
    n = 0
    for r in rows:
        con.execute(
            "INSERT INTO sessions(id,source,model,started_at,ended_at,input_tokens,"
            "output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,"
            "actual_cost_usd,display_name,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET source=excluded.source, model=excluded.model, "
            "started_at=excluded.started_at, ended_at=excluded.ended_at, "
            "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
            "cache_read_tokens=excluded.cache_read_tokens, "
            "cache_write_tokens=excluded.cache_write_tokens, "
            "reasoning_tokens=excluded.reasoning_tokens, "
            "actual_cost_usd=excluded.actual_cost_usd, display_name=excluded.display_name, "
            "ingested_at=excluded.ingested_at",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], now),
        )
        n += 1
    con.commit()
    return n


# --------------------------------------------------------------------------- #
# Live samplers
# --------------------------------------------------------------------------- #
def read_wired_bytes() -> int:
    """Wired memory (GPU on Apple Silicon) from vm_stat, in bytes."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = 16384
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page = int(m.group(1))
        m = re.search(r"Pages wired down:\s+(\d+)", out)
        if not m:
            return 0
        return int(m.group(1)) * page
    except Exception:  # noqa: BLE001
        return 0


def read_splash_health():
    """Return (model_bytes, ceiling_bytes, loaded_count, default_model) or None."""
    try:
        req = urllib.request.Request(f"{SPLASH_URL}/health")
        with urllib.request.urlopen(req, timeout=4) as resp:
            d = json.load(resp)
        pool = d.get("engine_pool", {})
        return (
            int(pool.get("current_model_memory", 0)),
            int(pool.get("final_ceiling", 0)),
            int(pool.get("loaded_count", 0)),
            d.get("default_model"),
        )
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self):
        self.tailer = LogTailer()
        self.current_tps = 0.0
        self._last_sample = time.time()
        self._stop = threading.Event()

    def _decay(self, now):
        dt = now - self._last_sample
        if dt > 0:
            self.current_tps *= 2.718281828 ** (-dt / DECAY_TAU)
        self._last_sample = now

    def step(self, con):
        now = time.time()
        self._decay(now)

        # 1) New requests from the log -> bump live tok/s.
        new_recs = self.tailer.poll(con)
        if new_recs:
            latest = max(new_recs, key=lambda r: r["ts"])
            self.current_tps = max(self.current_tps, latest["decode_tps"])

        # 2) Periodic ingests (cheap upserts).
        ingest_hourly_usage(con)
        ingest_model_totals(con)
        ingest_sessions(con)

        # 3) Memory + active model.
        wired = read_wired_bytes()
        health = read_splash_health()
        model_bytes = ceiling = 0
        loaded_model = None
        if health:
            model_bytes, ceiling, _loaded_count, loaded_model = health
        # Active model = model of the most recent request (fallback to default).
        row = con.execute(
            "SELECT model FROM requests ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        active_model = row["model"] if row else loaded_model

        # 4) Snapshot.
        con.execute(
            "INSERT OR REPLACE INTO snapshots(ts,tps,wired_bytes,model_bytes,"
            "ceiling_bytes,loaded_model,active_requests) VALUES(?,?,?,?,?,?,?)",
            (now, round(self.current_tps, 2), wired, model_bytes, ceiling,
             active_model, len(new_recs)),
        )
        # Prune old snapshots (keep LIVE_WINDOW * 2).
        cutoff = now - LIVE_WINDOW * 2
        con.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        con.commit()

    def run(self):
        con = get_db()
        self.tailer.resume(con)
        log.info("collector started (interval %.1fs)", SAMPLE_INTERVAL)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.step(con)
            except Exception as e:  # noqa: BLE001
                log.warning("collector step error: %s", e)
            elapsed = time.time() - t0
            self._stop.wait(max(0.2, SAMPLE_INTERVAL - elapsed))
        con.close()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
# API query helpers
# --------------------------------------------------------------------------- #
def q_summary(con):
    mt = con.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
        "COALESCE(SUM(cached_tokens),0) ca, COALESCE(SUM(requests),0) r, "
        "COALESCE(SUM(generation_duration),0) gd, COALESCE(SUM(prefill_duration),0) pd "
        "FROM model_totals"
    ).fetchone()
    total_tokens = mt["p"] + mt["c"]
    avg_tps = (mt["c"] / mt["gd"]) if mt["gd"] > 0 else 0.0
    cache_hit = (mt["ca"] / mt["p"]) if mt["p"] > 0 else 0.0

    snap = con.execute(
        "SELECT tps, wired_bytes, model_bytes, ceiling_bytes, loaded_model "
        "FROM snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    req = con.execute(
        "SELECT model, decode_tps, ts FROM requests ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return {
        "total_tokens": total_tokens,
        "prompt_tokens": mt["p"],
        "completion_tokens": mt["c"],
        "cached_tokens": mt["ca"],
        "total_requests": mt["r"],
        "avg_tps": round(avg_tps, 2),
        "cache_hit_rate": round(cache_hit, 4),
        "current_tps": round(snap["tps"], 2) if snap else 0.0,
        "last_request": (
            {"model": req["model"], "tps": req["decode_tps"], "ts": req["ts"]}
            if req else None
        ),
        "memory": {
            "wired_bytes": snap["wired_bytes"] if snap else 0,
            "model_bytes": snap["model_bytes"] if snap else 0,
            "ceiling_bytes": snap["ceiling_bytes"] if snap else 0,
            "loaded_model": snap["loaded_model"] if snap else None,
        },
        "now": time.time(),
    }


def q_heatmap(con, days: int):
    """GitHub-style grid: rows=hour(0-23), cols=day. value=total tokens."""
    since = int((datetime.now() - timedelta(days=days)).timestamp()) // 3600 * 3600
    rows = con.execute(
        "SELECT timestamp_hour, prompt_tokens, completion_tokens FROM hourly_usage "
        "WHERE timestamp_hour >= ?",
        (since,),
    ).fetchall()
    grid = {}
    for r in rows:
        total = r["prompt_tokens"] + r["completion_tokens"]
        if total <= 0:
            continue
        dt = datetime.fromtimestamp(r["timestamp_hour"])
        key = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        grid.setdefault(key, {})[dt.hour] = grid.get(key, {}).get(dt.hour, 0) + total
    return {"days": days, "grid": grid}


def q_models(con):
    rows = con.execute(
        "SELECT model_id, prompt_tokens, completion_tokens, cached_tokens, requests, "
        "generation_duration, prefill_duration FROM model_totals ORDER BY "
        "(prompt_tokens+completion_tokens) DESC"
    ).fetchall()
    out = []
    for r in rows:
        tps = (r["completion_tokens"] / r["generation_duration"]) if r["generation_duration"] > 0 else 0.0
        out.append({
            "model": r["model_id"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "cached_tokens": r["cached_tokens"],
            "total_tokens": r["prompt_tokens"] + r["completion_tokens"],
            "requests": r["requests"],
            "avg_tps": round(tps, 2),
            "cache_hit_rate": round(r["cached_tokens"] / r["prompt_tokens"], 4) if r["prompt_tokens"] else 0.0,
        })
    return out


def q_requests(con, limit: int):
    rows = con.execute(
        "SELECT ts, model, completion_tokens, prompt_tokens, total_time, decode_tps, "
        "ttft, finish_reason FROM requests ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def q_sessions(con):
    now = time.time()
    rows = con.execute(
        "SELECT id, source, model, started_at, ended_at, input_tokens, output_tokens, "
        "cache_read_tokens, reasoning_tokens, actual_cost_usd, display_name FROM sessions "
        "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 40"
    ).fetchall()
    out = []
    for r in rows:
        active = r["ended_at"] is None or (now - r["ended_at"]) < 3600
        out.append({
            "id": r["id"],
            "source": r["source"],
            "model": r["model"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "reasoning_tokens": r["reasoning_tokens"],
            "cost_usd": r["actual_cost_usd"],
            "display_name": r["display_name"],
            "active": active,
        })
    return out


def q_live(con, window: int):
    """Live throughput as a scatter of per-request tok/s.

    Each point is one completed request's decode_tps — an average over that
    request's generation, straight from the oMLX log (the log has no finer
    "instant" value). The window average is compiled from those request
    points, not from a decayed 2s sample.
    """
    since = time.time() - window
    rows = con.execute(
        "SELECT ts, decode_tps, model, completion_tokens FROM requests "
        "WHERE ts >= ? AND decode_tps > 0 ORDER BY ts ASC",
        (since,),
    ).fetchall()
    vals = [r["decode_tps"] for r in rows]
    avg = sum(vals) / len(vals) if vals else 0.0
    return {
        "window": window,
        "points": [
            {"ts": r["ts"], "tps": r["decode_tps"], "model": r["model"],
             "completion": r["completion_tokens"]}
            for r in rows
        ],
        "avg_tps": round(avg, 2),
        "count": len(vals),
    }


def q_context(con, window: int):
    """Context-window usage over time: prompt tokens per request (from oMLX log).

    Each point is one completed request; prompt_tokens is the context size that
    request saw. Shows how the context grows across a conversation.
    """
    since = time.time() - window
    rows = con.execute(
        "SELECT ts, prompt_tokens, completion_tokens, model FROM requests "
        "WHERE ts >= ? ORDER BY ts ASC",
        (since,),
    ).fetchall()
    return {
        "window": window,
        "points": [
            {"ts": r["ts"], "prompt": r["prompt_tokens"],
             "completion": r["completion_tokens"], "model": r["model"]}
            for r in rows
        ],
    }


def q_token_breakdown(con):
    """Token composition from the (active-profile) Hermes sessions table.

    This is the source that matches the Hermes UI: input / output / reasoning /
    cache_read / cache_write, overall and per model, plus the live context size
    of the most active session.
    """
    tot = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out, "
        "COALESCE(SUM(cache_read_tokens),0) cr, COALESCE(SUM(cache_write_tokens),0) cw, "
        "COALESCE(SUM(reasoning_tokens),0) reason, COUNT(*) n FROM sessions"
    ).fetchone()
    per_model = con.execute(
        "SELECT model, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out, "
        "COALESCE(SUM(cache_read_tokens),0) cr, COALESCE(SUM(reasoning_tokens),0) reason "
        "FROM sessions GROUP BY model ORDER BY (input_tokens+output_tokens) DESC"
    ).fetchall()
    # Most active session = largest input_tokens (the one filling the context).
    active = con.execute(
        "SELECT id, source, model, input_tokens, output_tokens, reasoning_tokens, "
        "cache_read_tokens, display_name FROM sessions "
        "ORDER BY input_tokens DESC LIMIT 1"
    ).fetchone()
    return {
        "total": {
            "input": tot["inp"], "output": tot["out"], "cache_read": tot["cr"],
            "cache_write": tot["cw"], "reasoning": tot["reason"], "sessions": tot["n"],
        },
        "per_model": [
            {"model": r["model"], "input": r["inp"], "output": r["out"],
             "cache_read": r["cr"], "reasoning": r["reason"]}
            for r in per_model
        ],
        "active_session": dict(active) if active else None,
    }


def read_max_model_len(model: str):
    """Fetch max_model_len for a model from Splash /v1/models (cached per call)."""
    try:
        req = urllib.request.Request(f"{SPLASH_URL}/v1/models")
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.load(resp).get("data", [])
        for m in data:
            if m.get("id") == model:
                return int(m.get("max_model_len", 0))
    except Exception:  # noqa: BLE001
        pass
    return 0


def q_memory(con):
    snap = con.execute(
        "SELECT wired_bytes, model_bytes, ceiling_bytes, loaded_model FROM snapshots "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    req = con.execute("SELECT model FROM requests ORDER BY ts DESC LIMIT 1").fetchone()
    now = time.time()
    cutoff = now - 3600
    active_sessions = con.execute(
        "SELECT id, source, model, started_at, input_tokens, output_tokens, display_name "
        "FROM sessions WHERE ended_at IS NULL OR ended_at > ? "
        "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 8",
        (cutoff,),
    ).fetchall()
    wired = snap["wired_bytes"] if snap else 0
    model_bytes = snap["model_bytes"] if snap else 0
    ceiling = snap["ceiling_bytes"] if snap else 0
    active_model = req["model"] if req else (snap["loaded_model"] if snap else None)

    # Context-window utilization: the most active session's input tokens vs the
    # model's max context. This is the honest "how full is the context" signal.
    ctx_tokens = 0
    ctx_session = None
    for s in active_sessions:
        if (s["input_tokens"] or 0) > ctx_tokens:
            ctx_tokens = s["input_tokens"]
            ctx_session = s
    max_len = read_max_model_len(active_model) if active_model else 0
    ctx_frac = (ctx_tokens / max_len) if max_len else 0.0
    # Estimated KV memory: fraction of the headroom (ceiling - weights) in use.
    headroom = max(0, ceiling - model_bytes)
    ctx_est_bytes = int(headroom * ctx_frac)

    return {
        "wired_bytes": wired,
        "model_bytes": model_bytes,
        "ceiling_bytes": ceiling,
        "headroom_bytes": headroom,
        "context_tokens": ctx_tokens,
        "max_context_tokens": max_len,
        "context_frac": round(ctx_frac, 4),
        "context_est_bytes": ctx_est_bytes,
        "context_session": (
            {"id": ctx_session["id"], "source": ctx_session["source"],
             "display_name": ctx_session["display_name"]}
            if ctx_session else None
        ),
        "loaded_model": snap["loaded_model"] if snap else None,
        "active_model": active_model,
        "active_sessions": [dict(r) for r in active_sessions],
    }


def q_tokens_live(con):
    """Unified token breakdown for the main live counter.

    Two honest sources, clearly separated:
      * oMLX (universal — every harness): prompt / completion / cached.
        fresh_input = prompt - cached (the tokens actually sent, not cache hits).
        completion = tokens generated (this INCLUDES thinking tokens).
      * Hermes (this profile only): reasoning = thinking tokens, plus the live
        counter for the currently-active session (input/output/reasoning).

    The four bars shown in the UI are: In (fresh) / Cached / Out / Think.
    'Out' is oMLX completion; 'Think' is the Hermes reasoning subset of it.
    """
    mt = con.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
        "COALESCE(SUM(cached_tokens),0) ca FROM model_totals"
    ).fetchone()
    prompt, completion, cached = mt["p"], mt["c"], mt["ca"]
    fresh_input = max(0, prompt - cached)

    # Hermes reasoning (active profile) — the thinking tokens.
    reason = con.execute(
        "SELECT COALESCE(SUM(reasoning_tokens),0) FROM sessions"
    ).fetchone()[0]

    # Live counter: the single most-recently-active session (ended_at IS NULL).
    active = con.execute(
        "SELECT model, display_name, input_tokens, output_tokens, reasoning_tokens, "
        "cache_read_tokens, started_at FROM sessions "
        "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    # Live counter across ALL currently-active sessions.
    act_tot = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
        "COALESCE(SUM(reasoning_tokens),0) r, COUNT(*) n FROM sessions WHERE ended_at IS NULL"
    ).fetchone()

    return {
        "total": prompt + completion,
        "fresh_input": fresh_input,
        "cached": cached,
        "output": completion,
        "reasoning": reason,
        "output_ex_reasoning": max(0, completion - reason),
        "active_session": dict(active) if active else None,
        "active_sessions": {
            "input": act_tot["i"], "output": act_tot["o"],
            "reasoning": act_tot["r"], "count": act_tot["n"],
        },
        "now": time.time(),
    }


def q_stats(con):
    """Extra stats, split into live (fast-changing) and persistent (cumulative)."""
    now = time.time()

    # ---- live ----
    snap = con.execute(
        "SELECT tps, active_requests FROM snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    # tokens/min: mean per-request tok/s over the last 60s * 60 (request-based,
    # not the decayed snapshot sample).
    recent = con.execute(
        "SELECT decode_tps FROM requests WHERE ts >= ? AND decode_tps > 0", (now - 60,)
    ).fetchall()
    tps_vals = [r["decode_tps"] for r in recent]
    tokens_per_min = round((sum(tps_vals) / len(tps_vals)) * 60) if tps_vals else 0
    # peak per-request tok/s in the live window
    peak = con.execute(
        "SELECT MAX(decode_tps) m FROM requests WHERE ts >= ?", (now - LIVE_WINDOW,)
    ).fetchone()["m"] or 0
    # avg TTFT over the last hour of requests
    ttft = con.execute(
        "SELECT AVG(ttft) a, COUNT(*) n FROM requests WHERE ts >= ? AND ttft IS NOT NULL",
        (now - 3600,),
    ).fetchone()
    # tokens generated in the last 60s (from completed requests)
    gen_60 = con.execute(
        "SELECT COALESCE(SUM(completion_tokens),0) c FROM requests WHERE ts >= ?",
        (now - 60,),
    ).fetchone()["c"]

    # ---- persistent ----
    mt = con.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
        "COALESCE(SUM(cached_tokens),0) ca, COALESCE(SUM(requests),0) r, "
        "COALESCE(SUM(generation_duration),0) gd, COALESCE(SUM(prefill_duration),0) pd "
        "FROM model_totals"
    ).fetchone()
    total_tokens = mt["p"] + mt["c"]
    avg_tokens_per_req = round(total_tokens / mt["r"]) if mt["r"] else 0
    # cost + reasoning from Hermes sessions
    sess = con.execute(
        "SELECT COALESCE(SUM(actual_cost_usd),0) cost, COALESCE(SUM(reasoning_tokens),0) reason, "
        "COALESCE(SUM(output_tokens),0) out FROM sessions"
    ).fetchone()
    reasoning_ratio = (sess["reason"] / sess["out"]) if sess["out"] else 0.0
    # tokens today / this week (from hourly_usage, the persistent per-hour table)
    day_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    week_start = day_start - 6 * 86400
    today = con.execute(
        "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t FROM hourly_usage WHERE timestamp_hour >= ?",
        (day_start,),
    ).fetchone()["t"]
    week = con.execute(
        "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t FROM hourly_usage WHERE timestamp_hour >= ?",
        (week_start,),
    ).fetchone()["t"]
    # peak day (busiest 24h) from hourly_usage
    peak_day_row = con.execute(
        "SELECT timestamp_hour, SUM(prompt_tokens+completion_tokens) t FROM hourly_usage "
        "GROUP BY (timestamp_hour/86400) ORDER BY t DESC LIMIT 1"
    ).fetchone()
    peak_day = None
    if peak_day_row and peak_day_row["t"]:
        peak_day = {
            "date": datetime.fromtimestamp(peak_day_row["timestamp_hour"]).strftime("%Y-%m-%d"),
            "tokens": peak_day_row["t"],
        }
    # total generation time (how long the model has been producing tokens)
    gen_hours = mt["gd"] / 3600

    return {
        "live": {
            "tokens_per_min": tokens_per_min,
            "active_requests": snap["active_requests"] if snap else 0,
            "avg_ttft": round(ttft["a"], 2) if ttft and ttft["a"] else None,
            "peak_tps": round(peak, 1),
            "generated_last_60s": gen_60,
        },
        "persistent": {
            "total_cost_usd": round(sess["cost"], 4),
            "avg_tokens_per_request": avg_tokens_per_req,
            "tokens_today": today,
            "tokens_this_week": week,
            "cache_savings": mt["ca"],
            "reasoning_ratio": round(reasoning_ratio, 4),
            "peak_day": peak_day,
            "total_generation_hours": round(gen_hours, 1),
            "total_prefill_hours": round(mt["pd"] / 3600, 1),
        },
        "now": now,
    }


def q_config():
    """Current effective config + per-source status (for the settings page)."""
    cfg = load_config()
    # Don't leak the resolved 'auto' hermes path as if it were user-set; show
    # the raw config.json value instead so the form round-trips cleanly.
    raw = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                raw = json.load(f)
        except Exception:  # noqa: BLE001
            raw = {}
    editable = {
        k: raw.get(k, DEFAULTS.get(k)) for k in
        ("port", "splash_url", "omlx_home", "omlx_log", "omlx_usage_db",
         "omlx_stats", "hermes_state_db", "db_path", "sample_interval",
         "decay_tau", "live_window")
    }
    return {
        "config": editable,
        "effective": {
            "port": PORT, "splash_url": SPLASH_URL, "omlx_home": str(OMLX_HOME),
            "omlx_log": str(OMLX_LOG), "omlx_usage_db": str(OMLX_USAGE_DB),
            "omlx_stats": str(OMLX_STATS), "hermes_state_db": str(HERMES_STATE),
            "db_path": str(DB_PATH),
        },
        "sources": source_status(),
        "config_path": str(CONFIG_PATH),
    }


def update_config(payload: dict):
    """Validate + write editable keys to config.json. Returns (ok, message, cfg)."""
    allowed = {
        "port": int, "splash_url": str, "omlx_home": str, "omlx_log": (str, type(None)),
        "omlx_usage_db": (str, type(None)), "omlx_stats": (str, type(None)),
        "hermes_state_db": str, "db_path": str, "sample_interval": float,
        "decay_tau": float, "live_window": int,
    }
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object", None
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        cfg = {"_docs": {}}
    changed = []
    for k, typ in allowed.items():
        if k not in payload:
            continue
        v = payload[k]
        if v is None or v == "":
            cfg[k] = None if typ is not str else ""
            changed.append(k)
            continue
        try:
            cfg[k] = typ(v)
        except (ValueError, TypeError):
            return False, f"invalid value for '{k}': {v!r}", None
        changed.append(k)
    # sanity: port range
    if isinstance(cfg.get("port"), int) and not (1 <= cfg["port"] <= 65535):
        return False, "port must be 1-65535", None
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return True, f"saved {len(changed)} key(s): {', '.join(changed) or 'none'}", cfg


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "ModelPulse/1.0"

    def log_message(self, *args):  # silence default stderr logging
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, status=200):
        if not path.exists():
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)
        con = get_db()
        try:
            if path == "/" or path == "/index.html":
                self._send_html(BASE / "dashboard.html")
            elif path == "/api/summary":
                self._send_json(q_summary(con))
            elif path == "/api/heatmap":
                days = int(qs.get("days", ["90"])[0])
                self._send_json(q_heatmap(con, days))
            elif path == "/api/models":
                self._send_json(q_models(con))
            elif path == "/api/requests":
                limit = int(qs.get("limit", ["50"])[0])
                self._send_json(q_requests(con, limit))
            elif path == "/api/sessions":
                self._send_json(q_sessions(con))
            elif path == "/api/live":
                window = int(qs.get("window", [str(LIVE_WINDOW)])[0])
                self._send_json(q_live(con, window))
            elif path == "/api/context":
                window = int(qs.get("window", [str(LIVE_WINDOW)])[0])
                self._send_json(q_context(con, window))
            elif path == "/api/tokens":
                self._send_json(q_token_breakdown(con))
            elif path == "/api/memory":
                self._send_json(q_memory(con))
            elif path == "/api/tokens-live":
                self._send_json(q_tokens_live(con))
            elif path == "/api/stats":
                self._send_json(q_stats(con))
            elif path == "/api/config":
                self._send_json(q_config())
            elif path == "/settings" or path == "/settings.html":
                self._send_html(BASE / "settings.html")
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            log.exception("API error on %s", path)
            self._send_json({"error": str(e)}, 500)
        finally:
            con.close()

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode() or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            if path == "/api/config":
                ok, msg, _cfg = update_config(payload)
                self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)
            elif path == "/api/config/reload":
                _cfg, changed, needs_restart = reload_config()
                self._send_json({
                    "ok": True,
                    "changed": changed,
                    "needs_restart": needs_restart,
                    "message": ("reloaded; restart required for port change"
                                if needs_restart else "reloaded live"),
                })
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            log.exception("POST error on %s", path)
            self._send_json({"error": str(e)}, 500)


def serve(port: int):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("ModelPulse serving on http://127.0.0.1:%d", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="ModelPulse — local LLM usage dashboard")
    ap.add_argument("--port", type=int, default=None, help="dashboard port (overrides config)")
    ap.add_argument("--collect", action="store_true", help="one-shot ingest, no server")
    ap.add_argument("--init-config", action="store_true",
                    help="write a default config.json if missing, then exit")
    ap.add_argument("--check", action="store_true",
                    help="print a config diagnostics report and exit")
    ap.add_argument("--show-config", action="store_true",
                    help="print the effective config and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Make sure a config.json exists so it's easy to find and edit.
    write_default_config()
    cfg = load_config(cli_port=args.port)
    apply_config(cfg)

    if args.init_config:
        print("config.json ready at", CONFIG_PATH)
        return
    if args.show_config:
        print(json.dumps(cfg, indent=2))
        return
    if args.check:
        check_config()
        return

    init_db()

    if args.collect:
        con = get_db()
        tailer = LogTailer()
        tailer.resume(con)
        n_req = len(tailer.poll(con))
        n_h = ingest_hourly_usage(con)
        n_m = ingest_model_totals(con)
        n_s = ingest_sessions(con)
        print(f"ingested: requests(new)={n_req} hourly={n_h} models={n_m} sessions={n_s}")
        con.close()
        return

    collector = Collector()
    global COLLECTOR
    COLLECTOR = collector
    t = threading.Thread(target=collector.run, daemon=True)
    t.start()
    try:
        serve(PORT)
    finally:
        collector.stop()


if __name__ == "__main__":
    main()
