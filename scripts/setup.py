#!/usr/bin/env python3
"""Interactive setup wizard for the RDS skill.

Usage:
  setup.py                      # Interactive: add or edit a connection
  setup.py --list               # List configured connections
  setup.py --remove NAME        # Remove a connection
  setup.py --set-default NAME   # Make NAME the default connection
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.client import (
    CONFIG_DIR,
    CONFIG_FILE,
    DEFAULT_WRITE_MODE,
    WRITE_MODES,
    load_config,
    remove_connection,
    save_config,
    save_connection,
    set_default_connection,
)


WRITE_MODE_DESCRIPTIONS = {
    "reject": "Read-only. Block all writes at the script level. Safest — pick this for production.",
    "auto":   "Reads + low-risk writes (INSERT, UPDATE/DELETE-with-WHERE, CREATE) run; the LLM is told to ask before high-risk writes (DROP, TRUNCATE, UPDATE/DELETE without WHERE, ALTER DROP, GRANT, REVOKE).",
    "ask":    "All non-read-only writes require the LLM to ask the user first.",
    "accept": "All writes run without confirmation. Pick only for ad-hoc dev/local connections.",
}


def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def check_prerequisites():
    print("\nChecking prerequisites...")

    v = sys.version.split()[0]
    print(f"  ✓ Python {v}")

    ok, out, _ = run_cmd(["aws", "--version"])
    if ok:
        print(f"  ✓ {out.split()[0]}")
    else:
        print("  ✗ AWS CLI not found")
        print("    Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
        sys.exit(1)

    try:
        import psycopg2
        print(f"  ✓ psycopg2 {psycopg2.__version__}")
    except ImportError:
        print("  psycopg2 not found — installing psycopg2-binary...")
        ok, _, err = run_cmd([sys.executable, "-m", "pip", "install", "--user", "psycopg2-binary", "-q"])
        if not ok:
            ok, _, err = run_cmd([sys.executable, "-m", "pip", "install",
                                   "--break-system-packages", "psycopg2-binary", "-q"])
        if ok:
            print("  ✓ psycopg2-binary installed")
            import site
            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.insert(0, user_site)
        else:
            print(f"  ✗ Failed to install psycopg2-binary: {err}")
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


def prompt_yes_no(message, default=False):
    suffix = "Y/n" if default else "y/N"
    while True:
        v = input(f"  {message} [{suffix}]: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def prompt_write_mode(current=None):
    print("Step 5: Write mode for this connection")
    print("  Controls whether non-read-only SQL is allowed.")
    print()
    for i, mode in enumerate(WRITE_MODES, 1):
        print(f"    {i}. {mode:6} — {WRITE_MODE_DESCRIPTIONS[mode]}")
    print()
    default_choice = current or DEFAULT_WRITE_MODE
    while True:
        choice = prompt(f"Select mode (number or name)", default_choice)
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(WRITE_MODES):
                return WRITE_MODES[idx]
        if choice in WRITE_MODES:
            return choice
        print(f"  Invalid choice. Pick one of: {', '.join(WRITE_MODES)}")


def collect_connection(existing=None):
    """Walk the user through profile / region / cluster / database / user / write_mode steps."""
    conn = dict(existing) if existing else {}

    # Step 1: AWS Profile
    print("Step 1: AWS Profile")
    profiles = list_aws_profiles()
    if profiles:
        print(f"  Available profiles: {', '.join(profiles)}")
    profile_default = conn.get("profile") or (profiles[0] if profiles else "default")
    profile = prompt("Enter profile name", profile_default)
    conn["profile"] = profile

    ok, out, err = run_cmd(["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"])
    if ok:
        identity = json.loads(out)
        arn = identity.get("Arn", "unknown")
        account = identity.get("Account", "unknown")
        name = arn.split("/")[-1] if "/" in arn else arn
        print(f"  ✓ Authenticated as {name} (account {account})")
    else:
        print(f"  ✗ Authentication failed: {err}")
        sys.exit(1)
    print()

    # Step 2: Region
    print("Step 2: AWS Region")
    region = prompt("Region", conn.get("region", "eu-west-1"))
    conn["region"] = region
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
            host = prompt("Enter cluster writer endpoint manually", conn.get("host"))
        else:
            print("  Found:")
            for i, c in enumerate(clusters, 1):
                iam = "✓ IAM auth" if c.get("IAMAuth") else "✗ IAM auth NOT enabled"
                print(f"    {i}. {c['ID']} ({c['Status']}) — {iam}")
                print(f"       {c['Endpoint']}")

            # Default selection: existing host if it matches a known cluster, else "1"
            default_choice = "1"
            if conn.get("host"):
                for i, c in enumerate(clusters, 1):
                    if c["Endpoint"] == conn["host"]:
                        default_choice = str(i)
                        break

            choice = prompt("Select cluster (number or endpoint)", default_choice)
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
                print(f"  ✓ Selected: {cluster['ID']}")
                print(f"  ✓ Endpoint: {host}")
            else:
                host = prompt("Enter cluster writer endpoint manually", conn.get("host"))
    else:
        print(f"  Warning: Could not list clusters: {err}")
        host = prompt("Enter cluster writer endpoint manually", conn.get("host"))

    conn["host"] = host
    conn["port"] = int(prompt("Port", str(conn.get("port", 5432))))
    print()

    # Step 4: Database and user
    print("Step 4: Database")
    conn["database"] = prompt("Database name", conn.get("database", "main"))
    conn["db_user"] = prompt("Database user", conn.get("db_user", "rds_skill_user"))
    print()

    # Step 5: Write mode
    conn["write_mode"] = prompt_write_mode(conn.get("write_mode"))
    print()

    return conn


def test_connection(conn):
    print("Testing connection...")
    print("  Generating IAM auth token...")
    token_cmd = [
        "aws", "rds", "generate-db-auth-token",
        "--hostname", conn["host"],
        "--port", str(conn["port"]),
        "--username", conn["db_user"],
        "--region", conn["region"],
        "--profile", conn["profile"],
    ]
    ok, token, err = run_cmd(token_cmd)
    if not ok or not token:
        print(f"  ✗ Failed to generate IAM auth token: {err}")
        print("  Check that your IAM role has rds-db:connect permission and IAM auth is enabled on the cluster.")
        return False
    print("  ✓ IAM auth token generated")

    print("  Connecting via psycopg2...")
    try:
        import psycopg2
        c = psycopg2.connect(
            host=conn["host"],
            port=conn["port"],
            database=conn["database"],
            user=conn["db_user"],
            password=token,
            sslmode="require",
            connect_timeout=10,
        )
        with c.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version()")
            row = cur.fetchone()
            print(f"  ✓ Connected as {row[0]} to {row[1]}")
            pg_version = row[2].split(",")[0] if row[2] else "unknown"
            print(f"  ✓ {pg_version}")
        c.close()
        return True
    except Exception as e:
        print(f"  ✗ Connection failed: {e}")
        return False


def cmd_list():
    config = load_config()
    connections = config.get("connections", {})
    if not connections:
        print("No connections configured. Run setup.py to add one.")
        return
    default = config.get("default")
    print(f"Connections (config: {CONFIG_FILE}):")
    print()
    name_w = max(len(n) for n in connections) + 2
    print(f"  {'NAME':<{name_w}} {'HOST':<55} {'DATABASE':<15} {'USER':<20} {'WRITE_MODE':<10} DEFAULT")
    print(f"  {'-' * (name_w - 2):<{name_w}} {'-' * 53:<55} {'-' * 13:<15} {'-' * 18:<20} {'-' * 8:<10} -------")
    for name in sorted(connections):
        c = connections[name]
        is_def = "*" if name == default else ""
        host = (c.get("host") or "")[:53]
        db = (c.get("database") or "")[:13]
        user = (c.get("db_user") or "")[:18]
        mode = c.get("write_mode") or DEFAULT_WRITE_MODE
        print(f"  {name:<{name_w}} {host:<55} {db:<15} {user:<20} {mode:<10} {is_def}")


def cmd_remove(name):
    try:
        config = remove_connection(name)
    except KeyError:
        print(f"ERROR: Connection '{name}' not found.")
        sys.exit(1)
    print(f"Removed connection: {name}")
    if config.get("default"):
        print(f"Default is now: {config['default']}")
    elif config.get("connections"):
        print("WARNING: No default connection. Set one with: setup.py --set-default NAME")
    else:
        print("No connections remaining.")


def cmd_set_default(name):
    try:
        set_default_connection(name)
    except KeyError:
        print(f"ERROR: Connection '{name}' not found.")
        sys.exit(1)
    print(f"Default connection set to: {name}")


def cmd_interactive():
    print("=" * 40)
    print("  RDS Aurora PostgreSQL Skill Setup")
    print("=" * 40)

    check_prerequisites()

    config = load_config()
    connections = config.get("connections", {})
    existing_conn = None
    chosen_name = None

    if connections:
        print(f"Existing connections in {CONFIG_FILE}:")
        names = sorted(connections.keys())
        for i, n in enumerate(names, 1):
            marker = " (default)" if n == config.get("default") else ""
            mode = connections[n].get("write_mode", DEFAULT_WRITE_MODE)
            print(f"  {i}. {n} — {connections[n].get('host', '?')}, write_mode={mode}{marker}")
        print()
        print("  a. Add a new connection")
        print("  e. Edit an existing connection")
        print("  q. Quit")
        action = prompt("Choose", "a").lower()
        if action == "q":
            return
        if action == "e":
            choice = prompt("Which one? (number or name)", "1")
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(names):
                    chosen_name = names[idx]
            elif choice in names:
                chosen_name = choice
            if not chosen_name:
                print(f"ERROR: '{choice}' not found.")
                sys.exit(1)
            existing_conn = connections[chosen_name]
            print(f"\nEditing connection: {chosen_name}")
        else:
            chosen_name = None  # will prompt below
        print()

    if not chosen_name:
        default_name = "main" if not connections else None
        while True:
            name = prompt("Connection name", default_name) if default_name else prompt("Connection name")
            if not name:
                print("  Name is required.")
                continue
            if name in connections:
                if prompt_yes_no(f"'{name}' already exists. Overwrite?", default=False):
                    existing_conn = connections[name]
                    chosen_name = name
                    break
                continue
            chosen_name = name
            break
        print()

    conn = collect_connection(existing_conn)

    if not test_connection(conn):
        print()
        if not prompt_yes_no("Save anyway?", default=False):
            sys.exit(1)
    print()

    set_default = False
    if not connections:
        set_default = True  # First connection — automatic default
    elif chosen_name != config.get("default"):
        set_default = prompt_yes_no(f"Set '{chosen_name}' as the default connection?", default=False)

    # Persist Python interpreter path at the top level (used by SKILL.md so the agent picks the right one)
    config = load_config()
    config["python"] = sys.executable
    save_config(config)
    save_connection(chosen_name, conn, set_default=set_default)

    print(f"Saved connection '{chosen_name}' to {CONFIG_FILE}")
    print()

    py_cmd = "python3" if sys.platform == "darwin" else "python"
    scripts_dir = Path(__file__).resolve().parent
    print("Setup complete! Try:")
    if set_default or len(load_config().get("connections", {})) == 1:
        print(f"  {py_cmd} {scripts_dir / 'query.py'} \"SELECT current_user, current_database()\"")
    else:
        print(f"  {py_cmd} {scripts_dir / 'query.py'} --connection {chosen_name} \"SELECT current_user, current_database()\"")
    print(f"  {py_cmd} {scripts_dir / 'setup.py'} --list")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="List configured connections")
    parser.add_argument("--remove", metavar="NAME", help="Remove a connection")
    parser.add_argument("--set-default", dest="set_default", metavar="NAME", help="Set NAME as the default connection")
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.remove:
        cmd_remove(args.remove)
    elif args.set_default:
        cmd_set_default(args.set_default)
    else:
        cmd_interactive()


if __name__ == "__main__":
    main()
