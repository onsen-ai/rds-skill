#!/usr/bin/env python3
"""Profile an Aurora PostgreSQL table — per-column statistics via a single query."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import add_connection_args, resolve_config, execute_query
from lib.formatter import format_output, format_duration

COLUMNS_SQL = """
SELECT column_name, UPPER(data_type) AS data_type
FROM information_schema.columns
WHERE table_schema = '{schema}'
  AND table_name   = '{table}'
ORDER BY ordinal_position
"""


def build_profile_sql(schema, table, columns_info):
    """Build a profiling SQL query from column metadata."""
    parts = []
    for col_name, data_type in columns_info:
        is_numeric = any(t in data_type.upper() for t in
                         ["INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL",
                          "BIGINT", "SMALLINT", "MONEY"])
        is_date = any(t in data_type.upper() for t in ["DATE", "TIMESTAMP", "TIME"])

        col_q = f'"{col_name}"'
        parts.append(f"""
SELECT
    '{col_name}'::TEXT                                                        AS column_name,
    '{data_type}'::TEXT                                                       AS data_type,
    COUNT(*)                                                                  AS total_rows,
    SUM(CASE WHEN {col_q} IS NULL THEN 1 ELSE 0 END)                         AS null_count,
    ROUND(
        SUM(CASE WHEN {col_q} IS NULL THEN 1 ELSE 0 END)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100, 1
    )                                                                         AS null_pct,
    COUNT(DISTINCT {col_q})                                                   AS distinct_count,
    {'MIN(' + col_q + ')::TEXT' if is_numeric or is_date else 'MIN(' + col_q + '::TEXT)'}  AS min_val,
    {'MAX(' + col_q + ')::TEXT' if is_numeric or is_date else 'MAX(' + col_q + '::TEXT)'}  AS max_val,
    {'ROUND(AVG(' + col_q + '::NUMERIC), 4)::TEXT' if is_numeric else "''::TEXT"}          AS avg_val
FROM "{schema}"."{table}"
""")

    return "\nUNION ALL\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Profile an Aurora PostgreSQL table — per-column statistics")
    add_connection_args(parser)
    parser.add_argument("--schema", required=True, help="Schema name")
    parser.add_argument("--table", required=True, help="Table name")
    args = parser.parse_args()

    config = resolve_config(args)

    print(f"Fetching columns for {args.schema}.{args.table}...", file=sys.stderr)
    col_sql = COLUMNS_SQL.format(schema=args.schema, table=args.table)
    _, col_rows, _ = execute_query(col_sql, config, timeout=args.timeout, max_rows=500)

    if not col_rows:
        print(f"ERROR: No columns found for {args.schema}.{args.table}", file=sys.stderr)
        sys.exit(1)

    columns_info = [(row[0], row[1]) for row in col_rows]
    print(f"  Found {len(columns_info)} columns. Running profile query...", file=sys.stderr)

    profile_sql = build_profile_sql(args.schema, args.table, columns_info)
    columns, rows, meta = execute_query(profile_sql, config, timeout=300, max_rows=500)

    format_output(columns, rows, fmt=args.format, save_fmt=args.save_format,
                  save_path=args.save, no_save=args.no_save, sql=profile_sql if args.save_sql else None)
    print(f"Duration: {format_duration(meta['duration_secs'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
