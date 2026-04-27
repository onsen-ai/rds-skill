#!/usr/bin/env python3
"""List database schemas."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import add_connection_args, resolve_config, execute_query
from lib.formatter import format_output, format_duration

SQL = """
SELECT
    n.nspname                          AS schema_name,
    u.usename                          AS owner,
    COUNT(c.relname)                   AS table_count
FROM pg_namespace n
JOIN pg_user u ON n.nspowner = u.usesysid
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind = 'r'
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname NOT LIKE 'pg_temp_%'
  AND n.nspname NOT LIKE 'pg_toast_temp_%'
GROUP BY n.nspname, u.usename
ORDER BY schema_name
"""


def main():
    parser = argparse.ArgumentParser(description="List Aurora PostgreSQL schemas")
    add_connection_args(parser)
    args = parser.parse_args()

    config = resolve_config(args)
    columns, rows, meta = execute_query(SQL, config, timeout=args.timeout, max_rows=args.max_rows)
    format_output(columns, rows, fmt=args.format, save_fmt=args.save_format,
                  save_path=args.save, no_save=args.no_save, sql=SQL if args.save_sql else None)
    print(f"{len(rows)} schemas. Duration: {format_duration(meta['duration_secs'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
