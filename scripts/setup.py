#!/usr/bin/env python3
"""Interactive setup wizard for the RDS skill."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import CONFIG_DIR, CONFIG_FILE, save_config


def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def check_prerequisites():
    print("\nChecking prerequisites...")

    v = sys.version.split()[0]
    print(f"  \u2713 Python {v}")

    ok, out, _ = run_cmd(["aws", "--version"])
    if ok:
        print(f"  \u2713 {out.split()[0]}")
    else:
        print("  \u2717 AWS CLI not found")
        print("    Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
        sys.exit(1)

    try:
        import psycopg2
        print(f"  \u2713 psycopg2 {psycopg2.__version__}")
    except ImportError:
        print("  psycopg2 not found — installing psycopg2-binary...")
        # Try --user first (works on externally-managed Python 3.11+/macOS Homebrew)
        ok, _, err = run_cmd([sys.executable, "-m", "pip", "install", "--user", "psycopg2-binary", "-q"])
        if not ok:
            # Fall back to --break-system-packages if --user also fails
            ok, _, err = run_cmd([sys.executable, "-m", "pip", "install",
                                   "--break-system-packages", "psycopg2-binary", "-q"])
        if ok:
            print("  \u2713 psycopg2-binary installed")
            # Ensure the user site-packages path is on sys.path for the rest of this run
            import site
            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.insert(0, user_site)
        else:
            print(f"  \u2717 Failed to install psycopg2-binary: {err}")
            print("    Try one of:")
            print("      pip install --user psycopg2-binary")
            print("      brew install libpq && pip install --user psycopg2-binary")
            sys.exit(1)

    print()


def list_aws_profiles():
    profiles = []
    for config_file in [Path.home() / ".aws" / "credentials", Path.home() / ".aws" / "config"]:
        if config_file.exists():
            with open(config_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("[") and line.endswith("]"):
                        name = line[1:-1].replace("profile ", "")
                        if name not in profiles:
                            profiles.append(name)
    return profiles


def prompt(message, default=None):
    if default:
        user_input = input(f"  {message} [{default}]: ").strip()
        return user_input or default
    else:
        user_input = input(f"  {message}: ").strip()
        return user_input


def main():
    print("=" * 40)
    print("  RDS Aurora PostgreSQL Skill Setup")
    print("=" * 40)

    check_prerequisites()

    config = {}

    # Step 1: AWS Profile
    print("Step 1: AWS Profile")
    profiles = list_aws_profiles()
    if profiles:
        print(f"  Available profiles: {', '.join(profiles)}")
    profile = prompt("Enter profile name", profiles[0] if profiles else "default")
    config["profile"] = profile

    ok, out, err = run_cmd(["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"])
    if ok:
        identity = json.loads(out)
        arn = identity.get("Arn", "unknown")
        account = identity.get("Account", "unknown")
        name = arn.split("/")[-1] if "/" in arn else arn
        print(f"  \u2713 Authenticated as {name} (account {account})")
    else:
        print(f"  \u2717 Authentication failed: {err}")
        sys.exit(1)
    print()

    # Step 2: Region
    print("Step 2: AWS Region")
    region = prompt("Region", "eu-west-1")
    config["region"] = region
    print()

    # Step 3: Aurora cluster discovery
    print("Step 3: Aurora PostgreSQL Cluster")
    print("  Discovering Aurora clusters...")
    ok, out, err = run_cmd([
        "aws", "rds", "describe-db-clusters",
        "--profile", profile, "--region", region,
        "--query", "DBClusters[?Engine=='aurora-postgresql'].{ID:DBClusterIdentifier,Endpoint:Endpoint,Status:Status,IAMAuth:IAMDatabaseAuthenticationEnabled}",
        "--output", "json"
    ])

    host = None
    if ok:
        clusters = json.loads(out)
        if not clusters:
            print("  No Aurora PostgreSQL clusters found. Check your profile/region.")
            host = prompt("Enter cluster writer endpoint manually")
        else:
            print("  Found:")
            for i, c in enumerate(clusters, 1):
                iam = "\u2713 IAM auth" if c.get("IAMAuth") else "\u2717 IAM auth NOT enabled"
                print(f"    {i}. {c['ID']} ({c['Status']}) — {iam}")
                print(f"       {c['Endpoint']}")

            choice = prompt("Select cluster (number or endpoint)", "1")
            cluster = None
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(clusters):
                    cluster = clusters[idx]
            if not cluster:
                for c in clusters:
                    if c["ID"] == choice or c["Endpoint"] == choice:
                        cluster = c
                        break
            if cluster:
                if not cluster.get("IAMAuth"):
                    print(f"\n  WARNING: IAM database authentication is NOT enabled on {cluster['ID']}.")
                    print("  Enable it in Terraform: iam_database_authentication_enabled = true")
                    print("  Setup will continue but connections will fail until IAM auth is enabled.\n")
                host = cluster["Endpoint"]
                print(f"  \u2713 Selected: {cluster['ID']}")
                print(f"  \u2713 Endpoint: {host}")
            else:
                host = prompt("Enter cluster writer endpoint manually")
    else:
        print(f"  Warning: Could not list clusters: {err}")
        host = prompt("Enter cluster writer endpoint manually")

    config["host"] = host
    config["port"] = int(prompt("Port", "5432"))
    print()

    # Step 4: Database and user
    print("Step 4: Database")
    config["database"] = prompt("Database name", "main")
    config["db_user"] = prompt("Database user", "rds_skill_user")
    print()

    # Step 5: Test connection
    print("Step 5: Testing connection...")
    print("  Generating IAM auth token...")
    token_cmd = [
        "aws", "rds", "generate-db-auth-token",
        "--hostname", config["host"],
        "--port", str(config["port"]),
        "--username", config["db_user"],
        "--region", config["region"],
        "--profile", config["profile"],
    ]
    ok, token, err = run_cmd(token_cmd)
    if not ok or not token:
        print(f"  \u2717 Failed to generate IAM auth token: {err}")
        print("  Check that your IAM role has rds-db:connect permission and IAM auth is enabled on the cluster.")
        sys.exit(1)
    print("  \u2713 IAM auth token generated")

    print("  Connecting via psycopg2...")
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=config["host"],
            port=config["port"],
            database=config["database"],
            user=config["db_user"],
            password=token,
            sslmode="require",
            connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version()")
            row = cur.fetchone()
            print(f"  \u2713 Connected as {row[0]} to {row[1]}")
            pg_version = row[2].split(",")[0] if row[2] else "unknown"
            print(f"  \u2713 {pg_version}")
        conn.close()
    except Exception as e:
        print(f"  \u2717 Connection failed: {e}")
        sys.exit(1)
    print()

    config["python"] = sys.executable
    save_config(config)
    print(f"Saved to {CONFIG_FILE}")

    py_cmd = "python3" if sys.platform == "darwin" else "python"
    scripts_dir = Path(__file__).resolve().parent
    print()
    print("Setup complete! Try:")
    print(f"  {py_cmd} {scripts_dir / 'query.py'} \"SELECT current_user, current_database()\"")
    print()


if __name__ == "__main__":
    main()
