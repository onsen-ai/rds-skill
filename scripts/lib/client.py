"""Shared Aurora PostgreSQL client — multi-connection config, args, IAM token generation, execute via psycopg2, write-mode-aware guard."""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

CONFIG_DIR = Path.home() / ".rds-skill"
CONFIG_FILE = CONFIG_DIR / "config.json"

WRITE_MODES = ("reject", "accept", "ask", "auto")
DEFAULT_WRITE_MODE = "reject"

CONNECTION_FIELDS = ("profile", "host", "port", "database", "db_user", "region", "write_mode")


# --- Config ---

def _empty_config():
    return {"default": None, "connections": {}}


def _is_legacy(config):
    """A pre-multi-connection config has top-level connection fields and no `connections` key."""
    return "connections" not in config and any(
        config.get(k) for k in ("host", "database", "db_user")
    )


def _migrate_legacy(config):
    """Wrap an old-shape single-connection config into the new shape."""
    legacy = {k: config[k] for k in CONNECTION_FIELDS if k in config}
    legacy.setdefault("write_mode", DEFAULT_WRITE_MODE)
    new_config = {
        "default": "main",
        "connections": {"main": legacy},
    }
    # Preserve top-level non-connection fields (e.g. `python`)
    for k, v in config.items():
        if k in CONNECTION_FIELDS:
            continue
        if k in ("default", "connections"):
            continue
        new_config[k] = v
    return new_config


def load_config():
    """Load config from ~/.rds-skill/config.json. Migrates old single-connection shape transparently."""
    if not CONFIG_FILE.exists():
        return _empty_config()
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    if _is_legacy(config):
        config = _migrate_legacy(config)
        try:
            save_config(config)
        except OSError:
            # Read-only filesystem or similar — keep the migrated config in memory
            pass
    config.setdefault("default", None)
    config.setdefault("connections", {})
    return config


def save_config(config):
    """Save config to ~/.rds-skill/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def list_connections():
    """Return a list of (name, is_default) tuples sorted by name."""
    config = load_config()
    default = config.get("default")
    return sorted(
        ((name, name == default) for name in config.get("connections", {})),
        key=lambda x: x[0],
    )


def save_connection(name, conn, set_default=False):
    """Save one connection. If `set_default` or it's the only connection, mark it as default."""
    config = load_config()
    config["connections"][name] = conn
    if set_default or len(config["connections"]) == 1 or not config.get("default"):
        config["default"] = name
    save_config(config)
    return config


def remove_connection(name):
    """Delete one connection. Returns the new config. Caller must handle default re-pointing."""
    config = load_config()
    if name not in config.get("connections", {}):
        raise KeyError(name)
    del config["connections"][name]
    if config.get("default") == name:
        # Pick any remaining connection as the new default, else None
        remaining = sorted(config["connections"].keys())
        config["default"] = remaining[0] if remaining else None
    save_config(config)
    return config


def set_default_connection(name):
    config = load_config()
    if name not in config.get("connections", {}):
        raise KeyError(name)
    config["default"] = name
    save_config(config)
    return config


# --- Args & resolution ---

def add_connection_args(parser):
    """Add standard connection args to an argparse parser."""
    parser.add_argument("--connection", help="Named connection from ~/.rds-skill/config.json (defaults to the saved default)")
    parser.add_argument("--profile", help="AWS CLI profile (overrides connection)")
    parser.add_argument("--host", help="Aurora cluster writer endpoint (overrides connection)")
    parser.add_argument("--port", type=int, help="Database port (default: 5432)")
    parser.add_argument("--database", help="Database name (overrides connection)")
    parser.add_argument("--db-user", dest="db_user", help="Database user (overrides connection)")
    parser.add_argument("--format", choices=["txt", "csv", "json"], default="txt",
                        help="Output format (default: txt)")
    parser.add_argument("--save-format", dest="save_format", choices=["txt", "csv", "json"], default="csv",
                        help="File save format (default: csv)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Max query wait time in seconds (default: 120)")
    parser.add_argument("--max-rows", dest="max_rows", type=int, default=1000,
                        help="Max rows to fetch (default: 1000)")
    parser.add_argument("--save", help="Save output to file path")
    parser.add_argument("--no-save", dest="no_save", action="store_true",
                        help="Don't auto-save results to ~/rds-exports/")
    parser.add_argument("--save-sql", dest="save_sql", action="store_true",
                        help="Save the SQL query alongside results as .sql file")


def resolve_config(args):
    """Resolve the active connection: pick named/default connection, apply CLI overrides, fill defaults.

    Returns a flat dict carrying everything execute_query needs (including write_mode).

    Backwards compatibility: if no saved connections exist and CLI overrides supply enough info
    (host + database + db_user), build an ephemeral connection — preserves the old "pass everything
    on the command line" workflow.
    """
    full = load_config()
    connections = full.get("connections", {})

    requested = getattr(args, "connection", None)
    name = requested or full.get("default")

    if requested and requested not in connections:
        available = ", ".join(sorted(connections)) or "(none)"
        print(f"ERROR: Connection '{requested}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)

    if name and name in connections:
        config = dict(connections[name])
        config["_connection_name"] = name
    else:
        # No saved connection — fall back to ephemeral CLI-only mode (pre-multi-connection behaviour)
        config = {"_connection_name": None}

    # CLI overrides
    if args.profile:
        config["profile"] = args.profile
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.database:
        config["database"] = args.database
    if args.db_user:
        config["db_user"] = args.db_user

    missing = [k for k in ("host", "database", "db_user") if not config.get(k)]
    if missing:
        if connections:
            available = ", ".join(sorted(connections))
            print(f"ERROR: Missing connection parameter(s): {', '.join(missing)}.", file=sys.stderr)
            print(f"Use --connection NAME (available: {available}) or pass --host/--database/--db-user.", file=sys.stderr)
        else:
            print(f"ERROR: Missing connection parameter(s): {', '.join(missing)}.", file=sys.stderr)
            print(f"Run setup first: python {Path(__file__).resolve().parent.parent / 'setup.py'}", file=sys.stderr)
            print(f"Or pass --host/--database/--db-user on the command line.", file=sys.stderr)
        sys.exit(1)

    config.setdefault("port", 5432)
    config.setdefault("region", "eu-west-1")
    config.setdefault("write_mode", DEFAULT_WRITE_MODE)
    if config["write_mode"] not in WRITE_MODES:
        print(f"ERROR: Invalid write_mode '{config['write_mode']}'. "
              f"Must be one of: {', '.join(WRITE_MODES)}", file=sys.stderr)
        sys.exit(1)
    return config


# --- Write-mode guard ---

READ_ONLY_KEYWORDS = {"SELECT", "WITH", "SHOW", "EXPLAIN", "SET"}


def _strip_sql(sql):
    clean = re.sub(r"--[^\n]*", "", sql)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
    return clean.strip()


def validate_sql(sql, write_mode=DEFAULT_WRITE_MODE):
    """Validate SQL according to the connection's write_mode.

    - reject:           only allow read-only keywords (SELECT/WITH/SHOW/EXPLAIN/SET)
    - accept/ask/auto:  allow any single statement; the LLM is expected to gate writes via SKILL.md guidance
    - all modes:        block multi-statement queries (injection defence)

    Raises ValueError on rejection.
    """
    clean = _strip_sql(sql)
    if not clean:
        raise ValueError("Empty SQL statement")

    first_keyword = clean.split()[0].upper().rstrip("(")

    if write_mode == "reject" and first_keyword not in READ_ONLY_KEYWORDS:
        raise ValueError(
            f"Blocked: '{first_keyword}' is not allowed when the connection's write_mode is 'reject'. "
            f"Allowed: {', '.join(sorted(READ_ONLY_KEYWORDS))}. "
            f"To allow writes, re-run setup.py and set this connection's write_mode to accept/ask/auto."
        )

    if re.search(r";\s*[A-Za-z]", clean):
        raise ValueError("Multi-statement queries are not allowed (injection defence — applies in all write_modes)")


# --- IAM token generation ---

def _generate_auth_token(config):
    """Generate a temporary IAM auth token via AWS CLI."""
    cmd = [
        "aws", "rds", "generate-db-auth-token",
        "--hostname", config["host"],
        "--port", str(config.get("port", 5432)),
        "--username", config["db_user"],
        "--region", config.get("region", "eu-west-1"),
    ]
    if config.get("profile"):
        cmd += ["--profile", config["profile"]]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to generate IAM auth token: {result.stderr.strip()}")
    return result.stdout.strip()


# --- Query execution ---

def execute_query(sql, config, timeout=120, max_rows=1000):
    """Execute a SQL query via psycopg2 with IAM auth token, gated by the connection's write_mode.

    Returns (columns, rows, metadata) where:
      - columns: list of column name strings
      - rows: list of lists (each inner list is one row)
      - metadata: dict with 'duration_secs', 'total_rows'
    """
    if psycopg2 is None:
        print("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    write_mode = config.get("write_mode", DEFAULT_WRITE_MODE)
    validate_sql(sql, write_mode=write_mode)

    token = _generate_auth_token(config)

    start = time.time()

    try:
        conn = psycopg2.connect(
            host=config["host"],
            port=config.get("port", 5432),
            database=config["database"],
            user=config["db_user"],
            password=token,
            sslmode="require",
            connect_timeout=10,
            options=f"-c statement_timeout={timeout * 1000}",
        )
    except psycopg2.OperationalError as e:
        raise RuntimeError(f"Connection failed: {e}")

    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description is None:
                # Non-SELECT statement — commit so writes persist (relevant for accept/ask/auto modes)
                conn.commit()
                affected = cur.rowcount if cur.rowcount >= 0 else 0
                return [], [], {"duration_secs": time.time() - start, "total_rows": affected}

            columns = [desc[0] for desc in cur.description]
            rows = []

            while True:
                batch = cur.fetchmany(min(500, max_rows - len(rows)))
                if not batch:
                    break
                rows.extend([list(row) for row in batch])
                if len(rows) >= max_rows:
                    break

            total_rows = cur.rowcount if cur.rowcount >= 0 else len(rows)

    except psycopg2.Error as e:
        raise RuntimeError(f"Query failed: {e}")
    finally:
        conn.close()

    duration = time.time() - start
    return columns, rows, {
        "duration_secs": duration,
        "total_rows": max(total_rows, len(rows)),
    }
