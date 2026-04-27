# Setting Up IAM Authentication on an RDS PostgreSQL Database

## Overview

IAM database authentication allows you to connect to an Amazon RDS PostgreSQL instance using
an AWS IAM token instead of a password. The token is generated on-the-fly using your IAM
identity and is valid for 15 minutes.

There are three steps:

1. **Enable IAM auth on the RDS cluster** — one-time AWS configuration change
2. **Create the database user and grant permissions** — done directly on the database
3. **Attach an IAM policy** — allows the IAM role or user to connect to the database

---

## Prerequisites

- An existing Amazon RDS Aurora PostgreSQL or RDS PostgreSQL cluster
- AWS CLI installed and configured (`aws --version`)
- A superuser connection to the database (e.g. via the master user)
- An IAM role or user that needs access to the database

---

## Step 1: Enable IAM Auth on the Cluster

Run the following AWS CLI command to enable IAM database authentication on your cluster:

```bash
aws rds modify-db-cluster \
  --db-cluster-identifier <your-cluster-identifier> \
  --enable-iam-database-authentication \
  --region <your-region> \
  --apply-immediately
```

For a standalone RDS instance (non-Aurora), use `modify-db-instance` instead:

```bash
aws rds modify-db-instance \
  --db-instance-identifier <your-instance-identifier> \
  --enable-iam-database-authentication \
  --region <your-region> \
  --apply-immediately
```

Verify it is enabled:

```bash
aws rds describe-db-clusters \
  --db-cluster-identifier <your-cluster-identifier> \
  --region <your-region> \
  --query "DBClusters[0].IAMDatabaseAuthenticationEnabled"
```

> **Note:** Enabling IAM auth requires no downtime and takes effect within a few minutes.

If you manage your infrastructure with Terraform, set the following in your cluster resource:

```hcl
iam_database_authentication_enabled = true
```

---

## Step 2: Create the Database User and Grant Permissions

Connect to the database as a superuser and run the following.

### Create the user

```sql
CREATE USER <db_username> WITH LOGIN;
GRANT rds_iam TO <db_username>;
```

`rds_iam` is the built-in RDS role that enables IAM token-based authentication for the user.
Without it, connection attempts using an IAM token will fail.

### Grant permissions per schema

Run the following for each schema the user needs access to:

```sql
GRANT USAGE ON SCHEMA <schema_name> TO <db_username>;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA <schema_name> TO <db_username>;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA <schema_name> TO <db_username>;
ALTER DEFAULT PRIVILEGES IN SCHEMA <schema_name> GRANT ALL PRIVILEGES ON TABLES TO <db_username>;
ALTER DEFAULT PRIVILEGES IN SCHEMA <schema_name> GRANT ALL PRIVILEGES ON SEQUENCES TO <db_username>;
```

Repeat for each schema the user needs access to.

> **Read-only access:** Replace `ALL PRIVILEGES` with `SELECT` in the statements above
> if the user only needs read access.

> **Why sequences?** Required for `INSERT` on tables with auto-increment/serial columns —
> without it inserts will fail even if the user has table-level privileges.

> **Why `ALTER DEFAULT PRIVILEGES`?** Ensures any tables or sequences created in the schema
> in the future automatically inherit the same permissions. Without this, you would need to
> re-run grants every time a new table is added.

### Bulk option — Grant access to all schemas at once

If you need to grant access to every schema in the database, use this loop instead of
running the statements above per schema:

```sql
DO $$
DECLARE
  s text;
BEGIN
  FOR s IN SELECT nspname FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  LOOP
    EXECUTE 'GRANT USAGE ON SCHEMA ' || quote_ident(s) || ' TO <db_username>';
    EXECUTE 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA ' || quote_ident(s) || ' TO <db_username>';
    EXECUTE 'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA ' || quote_ident(s) || ' TO <db_username>';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA ' || quote_ident(s) || ' GRANT ALL PRIVILEGES ON TABLES TO <db_username>';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA ' || quote_ident(s) || ' GRANT ALL PRIVILEGES ON SEQUENCES TO <db_username>';
  END LOOP;
END $$;
```

---

## Step 3: Attach an IAM Policy

The IAM role or user that needs to connect must have an IAM policy allowing the
`rds-db:connect` action on the specific database user and cluster.

### Find the cluster resource ID

The IAM policy ARN requires the cluster **resource ID** (not the cluster identifier or endpoint).
Retrieve it with:

```bash
aws rds describe-db-clusters \
  --region <your-region> \
  --query "DBClusters[*].[DBClusterIdentifier,DbClusterResourceId]" \
  --output table
```

### Add the policy statement

Attach the following statement to the IAM policy for the role or user that needs access:

```json
{
  "Sid": "AllowRDSIAMConnect",
  "Effect": "Allow",
  "Action": "rds-db:connect",
  "Resource": "arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id>/<db_username>"
}
```

To grant access to multiple clusters, add one ARN per cluster:

```json
{
  "Sid": "AllowRDSIAMConnect",
  "Effect": "Allow",
  "Action": "rds-db:connect",
  "Resource": [
    "arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id-1>/<db_username>",
    "arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id-2>/<db_username>"
  ]
}
```

---

## Step 4: Verify

Run the following queries on the database to confirm everything was applied correctly.

### Check schema usage grants

```sql
SELECT nspname AS schema, nspacl
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY nspname;
```

The `nspacl` column should contain `<db_username>=U/...` for each schema the user has access to.

### Check table-level grants per schema

```sql
SELECT table_schema, COUNT(*) AS tables_with_access
FROM information_schema.role_table_grants
WHERE grantee = '<db_username>'
GROUP BY table_schema
ORDER BY table_schema;
```

All schemas the user has access to should appear with a count greater than zero.

---

## Key Notes

- IAM auth defaults to disabled — it must be explicitly enabled on the cluster.
- `GRANT` is additive — it never removes existing permissions for other users.
- `rds_iam` role membership is required — it is the mechanism that maps an IAM token to a database user.
- Use `rds-db:connect` in IAM policies for RDS — this is different from `redshift:GetClusterCredentials` which is Redshift-specific.
- IAM auth tokens expire after **15 minutes** — applications must refresh the token before each connection or connection pool cycle.
- The `rds-db:connect` resource ARN uses the **cluster resource ID** (format: `cluster-XXXXXXXXXX`), not the cluster name or endpoint hostname.
