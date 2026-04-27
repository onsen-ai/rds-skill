---
name: rds
description: Query any AWS Aurora PostgreSQL cluster via IAM authentication and psycopg2. Use whenever the user mentions RDS, Aurora, PostgreSQL, Aurora PgSQL, database queries, schema exploration, table metadata, column listing, data profiling, or wants to explore Aurora database objects. Also use for local analytics on previously saved query results. Works with any Aurora PostgreSQL cluster with IAM database authentication enabled. Always use this skill for any Aurora PostgreSQL task, even simple SELECT queries.
---

# Aurora PostgreSQL Skill

Read-only Aurora PostgreSQL exploration and business analysis via IAM authentication. No passwords or secrets required — your AWS IAM identity is your credential. Cross-platform (Mac + Windows). Works with any AI coding agent.

All scripts are in `${CLAUDE_SKILL_DIR}/scripts/` and require Python 3, AWS CLI, and psycopg2.

## Python Command

Read `~/.rds-skill/config.json` and use the `"python"` key as the Python command.
If config doesn't exist yet, try `python3 --version` first, falling back to `python --version`.
Throughout this document, `PYTHON` means the detected Python command.

## First-Time Setup

**You cannot run the setup wizard directly** — it requires interactive terminal input.

Check if `~/.rds-skill/config.json` exists:
- **If it exists:** Read it to confirm the connection details.
- **If it doesn't exist:** Tell the user to run the setup wizard in their terminal:

> Run this in your terminal to configure the Aurora connection:
> ```
> python3 scripts/setup.py
> ```
> (On Windows, use `python` instead of `python3`)

Wait for the user to confirm setup is complete before running any queries.

## Quick Reference

| Task | Script | When to use | Key Args |
|------|--------|-------------|----------|
| **Run SQL** | `query.py` | Any free-form read-only query | `"SELECT ..."` or `--sql-file=PATH` |
| **List schemas** | `schemas.py` | Starting point — see what schemas exist with table counts | |
| **List tables** | `tables.py` | Browse tables, check row counts and sizes before querying | `--schema=NAME` |
| **List columns** | `columns.py` | Understand column types, nullability, indexes | `--schema=NAME --table=NAME` |
| **Search objects** | `search.py` | Find tables or columns when you don't know the exact name | `--pattern=TEXT` |
| **Sample data** | `sample.py` | Quick peek at actual values — always do this before writing queries | `--schema=NAME --table=NAME` |
| **Data profile** | `profile.py` | Per-column stats (nulls, cardinality, min/max/avg) | `--schema=NAME --table=NAME` |
| **Local analytics** | `analyze.py` | Analyze saved results locally without hitting Aurora | `FILE --describe` |

### Common options (all RDS scripts)

| Option | Description |
|--------|-------------|
| `--format=txt\|csv\|json` | Terminal display format (default: txt) |
| `--save-format=txt\|csv\|json` | File save format (default: csv) |
| `--save=PATH` | Save to a specific file path |
| `--no-save` | Don't auto-save to ~/rds-exports/ |
| `--save-sql` | Save the SQL query as a .sql file alongside results |
| `--sql-file=PATH` | Read SQL from a file (query.py only) |
| `--profile=NAME` | Override AWS profile |
| `--host=HOST` | Override cluster endpoint |
| `--database=NAME` | Override database |
| `--db-user=NAME` | Override database user |
| `--timeout=N` | Max wait seconds (default: 120) |
| `--max-rows=N` | Max rows to fetch (default: 1000) |

## Output and File Saving

All query results are **automatically saved** to `~/rds-exports/query-{timestamp}.csv`.
The terminal shows an aligned txt preview (first 200 rows). The saved file defaults to CSV for spreadsheet compatibility.

This means you always have:
- **Inline preview** (200 rows in txt format) — enough to understand the data shape and answer quick questions
- **Full CSV on disk** — for deeper analysis with `analyze.py` or for the user to open in a spreadsheet

`--format` controls terminal display (default: txt). `--save-format` controls the saved file format (default: csv). Use `--save-sql` to also save the SQL query as a matching `.sql` file. Use `--no-save` to skip auto-save. Use `--save=PATH` to save to a specific location.

---

## Defensive Guardrails

These rules protect the cluster from expensive queries. Follow them — but use judgement. If the user explicitly asks for something that bends a rule, explain the trade-off and proceed if they confirm.

- **Never `SELECT *` from tables with >10K rows** — use aggregations, filters, or `sample.py` instead. For smaller tables, `SELECT *` is fine.
- **Always add `LIMIT`** when exploring unfamiliar tables (default LIMIT 100).
- **Check row counts first** — run `tables.py --schema=X` before writing queries so you know what you're dealing with.
- **Prefer aggregations for large tables** — `COUNT`, `SUM`, `AVG` with `GROUP BY` over pulling raw rows.
- **Filter on indexed columns for large tables** — check indexes via `columns.py` (the `indexes` column). Filtering on indexed columns avoids full table scans.
- **Joins are fine** — as long as the join condition is correct and you aggregate/filter the result appropriately.
- **Avoid accidental cross joins** — always include `ON`/`USING`.
- **Prefer `LIMIT` + `ORDER BY`** over unbounded selects when exploring.

**Size awareness:**

| Table size | Approach |
|------------|----------|
| <10K rows | Explore freely, `SELECT *` is fine |
| 10K–1M rows | Add `WHERE` or `LIMIT`, aggregations preferred for full-table queries |
| >1M rows | Always aggregate or filter, never `SELECT *`, use indexed column filters |

---

## SQL Standards

Every SQL query you write must follow these rules.

### Always comment your SQL

Explain the business intent, not just the mechanics.

### Always show the SQL to the user

- **Short queries (<10 lines):** show the full SQL inline with comments
- **Long queries (>10 lines):** save to `~/rds-exports/query-{timestamp}.sql`, show the key parts inline, and reference the saved file

### Use `--sql-file` for complex queries

For long SQL, write it to a file first and execute with `--sql-file`:
```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/query.py --sql-file=~/rds-exports/my_query.sql
```

**Tip — inline multiline SQL without reading/writing in the LLM context:**

*Mac/Linux — heredoc:*
```bash
cat > "/path/to/query.sql" << 'EOSQL'
-- Your SQL here
SELECT ...
EOSQL
PYTHON ${CLAUDE_SKILL_DIR}/scripts/query.py --sql-file="/path/to/query.sql" --save="/path/to/results.csv" --save-sql
```

*Cross-platform — Python:*
```bash
PYTHON -c "
sql = '''
-- Your SQL here
SELECT ...
'''
open('/path/to/query.sql', 'w').write(sql.strip())
"
PYTHON ${CLAUDE_SKILL_DIR}/scripts/query.py --sql-file="/path/to/query.sql" --save="/path/to/results.csv" --save-sql
```

### Formatting conventions

```sql
------------------------------------------------------------------------------------------------------------------------
-- Monthly revenue by region
-- Purpose: Aggregate order line items by region for the last 12 months
-- Filters: Excludes cancelled orders and zero-quantity items
------------------------------------------------------------------------------------------------------------------------
WITH monthly_revenue AS (
    SELECT  r.region_name,
            DATE_TRUNC('month', o.order_date)::DATE                                  AS month_nk,
            SUM(o.quantity)                                                           AS total_quantity,
            SUM(o.line_total)                                                         AS total_revenue,
            COUNT(DISTINCT o.customer_id)                                             AS unique_customers
    FROM    sales.fact_order_lines o
    JOIN    sales.dim_regions r                     USING (region_id)
    WHERE   o.order_date >= NOW() - INTERVAL '12 months'
            AND o.quantity > 0
            AND o.order_status <> 'cancelled'
    GROUP   BY 1, 2
)
SELECT  region_name,
        month_nk,
        total_quantity,
        total_revenue,
        unique_customers,
        ROUND(total_revenue / NULLIF(total_quantity, 0), 2)                          AS revenue_per_unit
FROM    monthly_revenue
ORDER   BY region_name, month_nk
```

Key formatting rules:
- Section headers with `--` dashes above the query
- `SELECT`, `FROM`, `JOIN`, `WHERE`, `GROUP BY`, `ORDER BY` left-aligned
- **One column per line** — each column expression gets its own line with its `AS` alias
- Column aliases aligned at a consistent position
- Inline comments explaining non-obvious business logic
- CTE names that describe the business concept

### PostgreSQL-specific SQL notes

- Use `NOW() - INTERVAL '12 months'` for date arithmetic (not `DATEADD`)
- Use `EXTRACT(epoch FROM timestamp)` for epoch seconds
- Use `DATE_TRUNC('month', col)` for period truncation
- Use `NULLIF(expr, 0)` to avoid divide-by-zero
- `GROUP BY 1, 2, 3` positional references work in PostgreSQL
- Use `ILIKE` for case-insensitive pattern matching
- Use `::TYPE` for casting (e.g. `col::DATE`, `col::TEXT`, `col::NUMERIC`)

---

## Schema Exploration Workflow

Use this graduated approach when exploring an unfamiliar schema. **Don't follow this rigidly** — if the user's intent is clear, skip straight to the relevant step.

1. **Understand the landscape** — `schemas.py` → see what schemas exist
2. **Browse tables** — `tables.py --schema=X` → check row counts and sizes
3. **Understand structure** — `columns.py --schema=X --table=Y` → column types, indexes
4. **Sample first** — `sample.py --limit=5` → see actual data values
5. **Profile if needed** — `profile.py` → nulls, cardinality, min/max per column
6. **Write targeted queries** — now you know enough to write safe, efficient SQL
7. **Analyze locally** — `analyze.py` on saved results for follow-up

**Shortcut:** If the user says "how many orders last month?", don't run 5 discovery scripts. Check the table size, write the query, run it.

---

## Business Analysis Workflows

### Understanding business performance

1. **Start with the headline metric** — total revenue, order count, customer count for the period
2. **Break down by dimensions** — time (daily/weekly/monthly), region, channel, category
3. **Compare periods** — this period vs last period, year-over-year
4. **Identify outliers** — which segments are significantly above or below expectations?

### Root cause analysis

When something looks wrong:
1. **Confirm the anomaly** — is it real? Check the data source
2. **Decompose the metric** — volume vs value issue?
3. **Slice by dimensions** — which region/channel/category drove the change?
4. **Correlate with events** — promotions, price changes, stock-outs, seasonality

### Common analyst patterns

| Pattern | Approach | SQL shape |
|---------|----------|-----------|
| **Trend analysis** | Track metrics over time | `GROUP BY DATE_TRUNC('month', col)` + `SUM`/`COUNT` |
| **Cohort analysis** | Group by first purchase | `MIN(order_date)` per customer, then join back |
| **Top/bottom N** | Best and worst performers | `ORDER BY metric DESC LIMIT N` |
| **YoY comparison** | Year-over-year growth | `LAG()` window or self-join with date shifted 1 year |
| **Funnel analysis** | Conversion at each step | `COUNT(DISTINCT user_id)` per step |
| **Contribution** | Which items drive 80% of revenue | `SUM() OVER (ORDER BY ...)` cumulative |
| **Segmentation** | Group entities by behavior | `CASE WHEN` buckets, then profile each segment |

---

## Script Details

### query.py — Run SQL

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/query.py "SELECT count(1) FROM sales.orders"
PYTHON ${CLAUDE_SKILL_DIR}/scripts/query.py --sql-file=~/rds-exports/my_query.sql
```

### schemas.py — List schemas

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/schemas.py
```
```
schema_name   owner   table_count
-----------   -----   -----------
public        admin   12
sales         etl     45
```

### tables.py — List tables

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/tables.py --schema=sales
```
```
table_name       table_type  row_count  total_size  table_size  index_size
--------------   ----------  ---------  ----------  ----------  ----------
dim_customers    BASE TABLE  250000     120 MB      85 MB       35 MB
fact_orders      BASE TABLE  8500000    4500 MB     3800 MB     700 MB
```

### columns.py — List columns

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/columns.py --schema=sales --table=orders
```
```
pos  column_name   data_type          max_len  is_nullable  column_default  indexes
---  ------------  -----------------  -------  -----------  --------------  -------
1    order_id      INTEGER                     NO           nextval(...)    orders_pkey
2    order_date    TIMESTAMP WITHOUT           NO                           idx_orders_date
3    customer_id   INTEGER                     NO                           idx_orders_cust
```

### search.py — Search objects

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/search.py --pattern=order
PYTHON ${CLAUDE_SKILL_DIR}/scripts/search.py --pattern=revenue --type=column
```

### sample.py — Sample data

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/sample.py --schema=sales --table=orders --limit=5
```

### profile.py — Data profiling

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/profile.py --schema=sales --table=orders
```
```
column_name   data_type  total_rows  null_count  null_pct  distinct_count  min_val     max_val     avg_val
-----------   ---------  ----------  ----------  --------  --------------  ----------  ----------  -------
order_id      INTEGER    8500000     0           0.0       8500000         1           8500000     4250000
order_date    TIMESTAMP  8500000     0           0.0       1825            2022-01-01  2026-12-31
```

### analyze.py — Local analytics (no Aurora)

```bash
PYTHON ${CLAUDE_SKILL_DIR}/scripts/analyze.py ~/rds-exports/query-*.csv --describe
PYTHON ${CLAUDE_SKILL_DIR}/scripts/analyze.py data.csv --sum=revenue
PYTHON ${CLAUDE_SKILL_DIR}/scripts/analyze.py data.csv --group-by=region --sum=sales
PYTHON ${CLAUDE_SKILL_DIR}/scripts/analyze.py data.csv --filter='year=2024' --sort=amount --desc --top=10
PYTHON ${CLAUDE_SKILL_DIR}/scripts/analyze.py data.csv --hist=price
```

---

## Read-Only Guardrails

**Two layers of protection:**

1. **You must validate** — before passing any SQL to `query.py`, verify the first keyword is in the allowlist.

2. **`lib/client.py` enforces** — the script itself rejects non-allowlisted SQL before it reaches Aurora. Hard guardrail, cannot be bypassed.

**Allowed:** `SELECT`, `WITH`, `SHOW`, `EXPLAIN`, `SET`

**Blocked:** `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `COPY`, `GRANT`, `REVOKE`, `CALL`, `EXECUTE`, `VACUUM`, `ANALYZE`

Multi-statement queries (`;` followed by another statement) are also blocked.

---

## How IAM Authentication Works

This skill uses **AWS IAM database authentication** — no passwords or secrets are stored anywhere.

1. Your AWS CLI profile (`de_rds` or similar) provides your identity
2. The skill calls `aws rds generate-db-auth-token` to get a temporary 15-minute token
3. The token is used as the database password over an SSL-encrypted connection (`sslmode=require`)
4. The token expires automatically — no credential rotation needed

**Prerequisites (one-time setup, done by infra):**
- Aurora cluster: `iam_database_authentication_enabled = true`
- DB user: `GRANT rds_iam TO rds_skill_user`
- IAM policy: `rds-db:connect` on the cluster + user ARN
- VPN: connected to corporate VPN to reach the DB endpoint
