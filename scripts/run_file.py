#!/usr/bin/env python3
"""Run a .sql file against Aurora PostgreSQL, one statement at a time.

query.py runs a single statement and blocks everything else. Some files need what a single
statement — or a script pasted into a GUI client — cannot give them:

  * autocommit per statement. A DO block that COMMITs per batch is illegal inside an explicit
    transaction, and most clients wrap a pasted script in one. Sending statements individually
    with autocommit on is the only way those loops actually bound their lock duration.
  * one connection throughout. Pooled clients spread statements across backends, which silently
    breaks anything session-scoped (SET ROLE, hypopg index hiding, advisory locks).
  * RAISE NOTICE output. Progress lines from backfill loops are invisible in most grids.

This is the skill's deliberate exception to the one-statement rule. Because of that it is gated
twice, and BOTH gates must pass:

  1. write_mode — a connection whose write_mode is 'reject' refuses unless --allow-write.
  2. target     — anything other than the saved default connection refuses unless --allow-prod.
                  An unsaved connection, or one whose host/database/db-user/port is overridden on
                  the command line, counts as "not the default" and is protected too.

By default there is NO transaction around the file: each statement commits as it runs, so a
failure at statement 40 leaves the first 39 applied. That is what lets a batched backfill COMMIT
per batch and bound its lock duration. Pass --single-transaction for all-or-nothing instead —
at the cost of holding every lock the file takes until it finishes, and of rejecting the two
things that cannot live inside a transaction block: a DO block that COMMITs, and statements like
CREATE INDEX CONCURRENTLY or VACUUM. Run --dry-run first either way.

    run_file.py migrations/add_search.sql                     # saved default connection
    run_file.py --allow-write --timing migrations/add_search.sql
    run_file.py --allow-write --single-transaction migrations/add_search.sql
    run_file.py --connection prod --allow-write --allow-prod migrations/add_search.sql
    run_file.py --dry-run migrations/add_search.sql           # split and list, run nothing
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import load_config, resolve_config, generate_auth_token, connect_target

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# CLI flags that repoint the connection somewhere the saved config didn't describe.
TARGET_OVERRIDES = ("host", "database", "db_user", "port")

# Things PostgreSQL refuses to run inside an explicit transaction block, so they are worth
# flagging before --single-transaction wastes a run on them. Matched loosely on purpose — this
# only prints a warning, and a false positive costs nothing.
NON_TRANSACTIONAL = (
    (r"\bCREATE\s+INDEX\s+CONCURRENTLY\b", "CREATE INDEX CONCURRENTLY"),
    (r"\bDROP\s+INDEX\s+CONCURRENTLY\b", "DROP INDEX CONCURRENTLY"),
    (r"\bREINDEX\b[\s\S]*?\bCONCURRENTLY\b", "REINDEX CONCURRENTLY"),
    (r"\bVACUUM\b", "VACUUM"),
    (r"\bALTER\s+SYSTEM\b", "ALTER SYSTEM"),
    (r"(?<!\w)COMMIT\s*;", "COMMIT"),
    (r"(?<!\w)ROLLBACK\s*;", "ROLLBACK"),
)


# --- Splitting ---

def split_statements(sql):
    """Split SQL into top-level statements.

    Semicolons only end a statement outside of: dollar-quoted blocks ($$ or $tag$), single-quoted
    literals, double-quoted identifiers, line comments and block comments. Dollar quoting is the
    one that matters — function bodies and DO blocks are full of semicolons, and a naive split on
    ';' shreds them into fragments that each fail on their own.
    """
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]

        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end + 1
            buf.append(sql[i:end])
            i = end
            continue

        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue

        if ch in ("'", '"'):
            quote, j = ch, i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:   # doubled = escaped
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i:j + 1]
                end = sql.find(tag, j + 1)
                end = n if end == -1 else end + len(tag)
                buf.append(sql[i:end])
                i = end
                continue

        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf, i = [], i + 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if _has_sql(s)]


def _has_sql(chunk):
    """False for a chunk that is only comments — a trailing note is not a statement."""
    return bool(_strip_comments(chunk).strip())


def _strip_comments(chunk):
    stripped = re.sub(r"/\*.*?\*/", "", chunk, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", stripped)


def transaction_conflicts(statements):
    """Statements that will fail once the file is wrapped in a single transaction."""
    found = []
    for i, stmt in enumerate(statements, 1):
        body = _strip_comments(stmt)
        for pattern, name in NON_TRANSACTIONAL:
            if re.search(pattern, body, re.IGNORECASE):
                found.append((i, name))
                break
    return found


def label(stmt):
    """First meaningful line, for progress output."""
    for line in stmt.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return line[:110]
    return stmt[:110]


# --- Gates ---

def check_gates(args, config):
    """Both gates must pass. Exits with an explanation on refusal."""
    name = config.get("_connection_name")
    saved_default = load_config().get("default")
    overrides = [a for a in TARGET_OVERRIDES if getattr(args, a, None)]

    if config.get("write_mode") == "reject" and not args.allow_write:
        sys.exit(
            f"Refusing to run: connection '{name or 'ad-hoc'}' has write_mode 'reject'.\n"
            f"This runner executes whatever is in the file — usually DDL or a backfill.\n"
            f"Pass --allow-write if that is genuinely the intent."
        )

    if overrides:
        flags = ", ".join("--" + a.replace("_", "-") for a in overrides)
        reason = f"{flags} repoints this away from the saved connection"
    elif not name:
        reason = "this is an ad-hoc connection, not one saved in the config"
    elif name != saved_default:
        reason = f"'{name}' is not the default connection ('{saved_default}')"
    else:
        return

    if not args.allow_prod:
        sys.exit(
            f"Refusing to run against {config['database']}: {reason}.\n"
            f"Unknown targets are treated as production. Pass --allow-prod to confirm."
        )


# --- Connection ---

def open_connection(config, single_transaction=False):
    if psycopg2 is None:
        sys.exit("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary")

    token = generate_auth_token(config)
    dial_host, dial_port = connect_target(config)
    conn = psycopg2.connect(
        host=dial_host,
        port=dial_port,
        dbname=config["database"],
        user=config["db_user"],
        password=token,
        sslmode="require",
        connect_timeout=60,
    )
    # Autocommit is the default so a COMMIT inside a DO block is legal. Turning it off puts
    # psycopg2 back in charge of the transaction, which is exactly what --single-transaction wants.
    conn.autocommit = not single_transaction
    return conn


# --- Main ---

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Path to the .sql file to run")
    ap.add_argument("--connection", help="Named connection from ~/.rds-skill/config.json (defaults to the saved default)")
    ap.add_argument("--profile", help="AWS CLI profile (overrides connection)")
    ap.add_argument("--host", help="Aurora cluster endpoint (overrides connection)")
    ap.add_argument("--port", type=int, help="Database port (default: 5432)")
    ap.add_argument("--database", help="Database name (overrides connection)")
    ap.add_argument("--db-user", dest="db_user", help="Database user (overrides connection)")
    ap.add_argument("--allow-write", action="store_true",
                    help="Required when the connection's write_mode is 'reject'")
    ap.add_argument("--allow-prod", action="store_true",
                    help="Required for any target other than the saved default connection")
    ap.add_argument("--dry-run", action="store_true", help="Split and list statements, run nothing")
    ap.add_argument("--single-transaction", action="store_true",
                    help="Wrap the whole file in one transaction — all of it applies or none of it. "
                         "Holds every lock until the file finishes, and rejects COMMIT inside DO "
                         "blocks plus CREATE INDEX CONCURRENTLY / VACUUM")
    ap.add_argument("--timing", action="store_true",
                    help="Print every statement's duration, not just the slow ones")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="Keep going after a failed statement (default: stop)")
    args = ap.parse_args()

    if args.single_transaction and args.continue_on_error:
        ap.error("--continue-on-error cannot be combined with --single-transaction: once a "
                 "statement fails, the transaction is aborted and every later statement fails too")

    sql_path = Path(args.file)
    if not sql_path.exists():
        sys.exit(f"ERROR: SQL file not found: {args.file}")

    statements = split_statements(sql_path.read_text())
    print(f"{len(statements)} statements in {args.file}\n")

    # Checked before the --dry-run exit: a dry run is when you most want to hear about this.
    if args.single_transaction:
        conflicts = transaction_conflicts(statements)
        if conflicts:
            print("WARNING: --single-transaction, but these will fail inside a transaction block:")
            for i, name in conflicts:
                print(f"  statement {i}: {name}")
            print("  drop --single-transaction, or split those out into their own file\n")

    if args.dry_run:
        for i, s in enumerate(statements, 1):
            print(f"{i:3}. {label(s)}")
        return

    config = resolve_config(args)
    check_gates(args, config)

    conn = open_connection(config, single_transaction=args.single_transaction)
    print(f"connected: {config.get('_connection_name') or 'ad-hoc'} -> "
          f"{config['database']} as {config['db_user']}")
    print("single transaction — nothing applies unless every statement succeeds\n"
          if args.single_transaction else
          "no transaction — each statement commits as it runs\n")

    failures, started = 0, time.time()
    with conn.cursor() as cur:
        for i, stmt in enumerate(statements, 1):
            t0 = time.time()
            try:
                cur.execute(stmt)
                ms = (time.time() - t0) * 1000
                flag = "" if ms < 1000 else "  <-- slow"
                if args.timing or ms >= 1000:
                    print(f"{i:3}. {ms:9.1f} ms  {label(stmt)}{flag}")
                else:
                    print(f"{i:3}. {'ok':>9}     {label(stmt)}")
                if cur.description:                      # a statement that returned rows
                    rows = cur.fetchall()
                    cols = [d.name for d in cur.description]
                    print("      " + " | ".join(cols))
                    for r in rows[:50]:
                        print("      " + " | ".join("" if v is None else str(v) for v in r))
                    if len(rows) > 50:
                        print(f"      ... {len(rows) - 50} more rows")
            except Exception as exc:                     # noqa: BLE001 - report and decide
                failures += 1
                print(f"{i:3}. {'FAILED':>9}     {label(stmt)}")
                print(f"      {type(exc).__name__}: {exc}")
                if not args.continue_on_error:
                    if not args.single_transaction:
                        print(f"      stopped — statements 1-{i - 1} are already committed")
                    break
            finally:
                for notice in conn.notices:
                    print("      NOTICE: " + notice.strip())
                conn.notices.clear()

    if args.single_transaction:
        if failures:
            conn.rollback()
            print("\nrolled back — nothing in this file was applied")
        else:
            conn.commit()
            print("\ncommitted")

    conn.close()
    print(f"\n{len(statements) - failures}/{len(statements)} ok in {time.time() - started:.1f}s")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
