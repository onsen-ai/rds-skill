#!/usr/bin/env python3
"""List columns for a table with types, nullability, defaults, and indexes."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import add_connection_args, resolve_config, execute_query
from lib.formatter import format_output, format_duration

SQL_TEMPLATE = """
SELECT
    c.ordinal_position                                                   AS pos,
    c.column_name,
    UPPER(c.data_type)                                                   AS data_type,
    c.character_maximum_length                                           AS max_len,
    c.numeric_precision                                                  AS num_prec,
    c.numeric_scale                                                      AS num_scale,
    c.is_nullable,
    c.column_default,
    COALESCE(
        STRING_AGG(i.relname, ', ' ORDER BY i.relname),
        ''
    )                                                                    AS indexes
FROM information_schema.columns c
LEFT JOIN pg_namespace ns  ON ns.nspname  = c.table_schema
LEFT JOIN pg_class     tbl ON tbl.relname = c.table_name
                          AND tbl.relnamespace = ns.oid
LEFT JOIN pg_attribute att ON att.attrelid = tbl.oid
                          AND att.attname   = c.column_name
LEFT JOIN pg_index     ix  ON ix.indrelid  = tbl.oid
                          AND att.attnum    = ANY(ix.indkey)
LEFT JOIN pg_class     i   ON i.oid        = ix.indexrelid
WHERE c.table_schema = '{schema}'
  AND c.table_name   = '{table}'
GROUP BY
    c.ordinal_position, c.column_name, c.data_type,
    c.character_maximum_length, c.numeric_precision, c.numeric_scale,
    c.is_nullable, c.column_default
ORDER BY c.ordinal_position
"""


def main():
    parser = argparse.ArgumentParser(description="List columns for an Aurora PostgreSQL table")
    add_connection_args(parser)
    parser.add_argument("--schema", required=True, help="Schema name")
    parser.add_argument("--table", required=True, help="Table name")
    args = parser.parse_args()

    config = resolve_config(args)
    sql = SQL_TEMPLATE.format(schema=args.schema, table=args.table)
    columns, rows, meta = execute_query(sql, config, timeout=args.timeout, max_rows=args.max_rows)
    format_output(columns, rows, fmt=args.format, save_fmt=args.save_format,
                  save_path=args.save, no_save=args.no_save, sql=sql if args.save_sql else None)
    print(f"{len(rows)} columns. Duration: {format_duration(meta['duration_secs'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
