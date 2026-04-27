#!/usr/bin/env python3
"""List tables in a schema with row counts and size."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import add_connection_args, resolve_config, execute_query
from lib.formatter import format_output, format_duration

SQL_TEMPLATE = """
SELECT
    t.table_name,
    t.table_type,
    COALESCE(s.n_live_tup, 0)                                                        AS row_count,
    pg_size_pretty(pg_total_relation_size(
        quote_ident(t.table_schema) || '.' || quote_ident(t.table_name)
    ))                                                                                AS total_size,
    pg_size_pretty(pg_relation_size(
        quote_ident(t.table_schema) || '.' || quote_ident(t.table_name)
    ))                                                                                AS table_size,
    pg_size_pretty(pg_indexes_size(
        quote_ident(t.table_schema) || '.' || quote_ident(t.table_name)
    ))                                                                                AS index_size,
    s.last_vacuum::DATE                                                               AS last_vacuum,
    s.last_analyze::DATE                                                              AS last_analyze
FROM information_schema.tables t
LEFT JOIN pg_stat_user_tables s
       ON s.schemaname = t.table_schema
      AND s.relname    = t.table_name
WHERE t.table_schema = '{schema}'
ORDER BY t.table_name
"""


def main():
    parser = argparse.ArgumentParser(description="List tables in an Aurora PostgreSQL schema")
    add_connection_args(parser)
    parser.add_argument("--schema", required=True, help="Schema name")
    args = parser.parse_args()

    config = resolve_config(args)
    sql = SQL_TEMPLATE.format(schema=args.schema)
    columns, rows, meta = execute_query(sql, config, timeout=args.timeout, max_rows=args.max_rows)
    format_output(columns, rows, fmt=args.format, save_fmt=args.save_format,
                  save_path=args.save, no_save=args.no_save, sql=sql if args.save_sql else None)
    print(f"{len(rows)} tables. Duration: {format_duration(meta['duration_secs'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
