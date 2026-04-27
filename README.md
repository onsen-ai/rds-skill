# RDS Aurora PostgreSQL Skill

Read-only Aurora PostgreSQL exploration and business analysis via AWS IAM authentication. No passwords or secrets — your AWS IAM identity is your credential.

## Requirements

- Python 3.8+
- AWS CLI (configured with an SSO profile that has `rds-db:connect` permission)
- psycopg2-binary (installed automatically by `setup.py`)
- VPN access to the Aurora cluster endpoint

## Setup

```bash
python3 scripts/setup.py
```

The wizard will:
1. Check prerequisites and install psycopg2-binary if missing
2. List available AWS profiles and verify your identity
3. Discover Aurora PostgreSQL clusters in your account
4. Test the connection using IAM auth token generation
5. Save config to `~/.rds-skill/config.json`

## Usage

```bash
# Run a query
python3 scripts/query.py "SELECT current_user, current_database()"

# List schemas
python3 scripts/schemas.py

# List tables in a schema
python3 scripts/tables.py --schema=public

# Explore columns
python3 scripts/columns.py --schema=public --table=orders

# Sample data
python3 scripts/sample.py --schema=public --table=orders --limit=10

# Search for tables or columns
python3 scripts/search.py --pattern=customer

# Profile a table
python3 scripts/profile.py --schema=public --table=orders

# Analyze saved results locally
python3 scripts/analyze.py ~/rds-exports/query-*.csv --describe
```

## How It Works

Authentication uses [AWS IAM database authentication](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/UsingWithRDS.IAMDBAuth.html):

1. `aws rds generate-db-auth-token` generates a temporary 15-minute token from your IAM identity
2. psycopg2 connects using the token as the password with `sslmode=require`
3. No passwords or secrets are stored anywhere

## Infrastructure Requirements

These must be set up once by your infra team:

| Requirement | How |
|---|---|
| IAM auth enabled on cluster | `iam_database_authentication_enabled = true` in Terraform |
| DB user with IAM role | `GRANT rds_iam TO rds_skill_user;` |
| IAM policy | `rds-db:connect` on the cluster + user ARN |
| VPN | Connected to corporate VPN |
