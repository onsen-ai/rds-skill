"""Shared Aurora PostgreSQL client — config, args, IAM token generation, execute via psycopg2, read-only guard."""

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


# --- Config ---

def load_config():
    """Load saved config from ~/.rds-skill/config.json."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(config):
    """Save config to ~/.rds-skill/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def add_connection_args(parser):
    """Add standard connection args to an argparse parser."""
    parser.add_argument("--profile", help="AWS CLI profile")
    parser.add_argument("--host", help="Aurora cluster writer endpoint")
    parser.add_argument("--port", type=int, help="Database port (default: 5432)")
    parser.add_argument("--database", help="Database name")
    parser.add_argument("--db-user", dest="db_user", help="Database user")
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
    """Merge saved config with CLI args (CLI wins)."""
    config = load_config()
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
        print(f"ERROR: Missing connection parameter(s): {', '.join(missing)}", file=sys.stderr)
        print(f"Run setup first: python {Path(__file__).resolve().parent.parent / 'setup.py'}", file=sys.stderr)
        sys.exit(1)

    config.setdefault("port", 5432)
    config.setdefault("region", "eu-west-1")
    return config


# --- Read-only guard ---

ALLOWED_KEYWORDS = {"SELECT", "WITH", "SHOW", "EXPLAIN", "SET"}


def validate_sql(sql):
    """Validate that SQL is read-only. Raises ValueError if not."""
    clean = re.sub(r"--[^\n]*", "", sql)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
    clean = clean.strip()

    if not clean:
        raise ValueError("Empty SQL statement")

    first_keyword = clean.split()[0].upper().rstrip("(")

    if first_keyword not in ALLOWED_KEYWORDS:
        raise ValueError(
            f"Blocked statement type: {first_keyword}. "
            f"Only read-only queries are allowed ({', '.join(sorted(ALLOWED_KEYWORDS))})"
        )

    if re.search(r";\s*[A-Za-z]", clean):
        raise ValueError("Multi-statement queries are not allowed")


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
    """Execute a read-only query via psycopg2 with IAM auth token.

    Returns (columns, rows, metadata) where:
      - columns: list of column name strings
      - rows: list of lists (each inner list is one row)
      - metadata: dict with 'duration_secs', 'total_rows'
    """
    if psycopg2 is None:
        print("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    validate_sql(sql)

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
                return [], [], {"duration_secs": time.time() - start, "total_rows": 0}

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
