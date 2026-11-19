#!/usr/bin/env bash
# Create the lmh-lives-saved-tool database and its own login on the portal
# Aurora cluster (Dev or Prod), and store the login in Secrets Manager for the
# app (LivesSavedStack reads <env>/aurora-postgres/lives-saved-tool).
#
# The login owns only its database: it can't create databases or roles, and
# PUBLIC can't connect to the new database. Idempotent: re-running reuses the
# stored password and re-applies the settings.
#
# Usage (from the infra repo root, with fresh creds for the target account):
#   set -a; . ./.aws-temp-creds-dev; set +a
#   bash scripts/create-lives-saved-db.sh            # dry run: checks only
#   bash scripts/create-lives-saved-db.sh --apply    # make the changes
#   ENV=Prod bash scripts/create-lives-saved-db.sh   # Prod (use Prod creds)
set -euo pipefail

ENV=${ENV:-Dev}   # Dev | Prod
case "$ENV" in
  Dev)  EXPECTED_ACCOUNT=002190277880; env_lc=dev ;;
  Prod) EXPECTED_ACCOUNT=301323023124; env_lc=prod ;;
  *) echo "ENV must be Dev or Prod"; exit 1 ;;
esac
export AWS_DEFAULT_REGION=us-east-1
ADMIN_SECRET=${ADMIN_SECRET:-$env_lc/aurora-postgres/credentials}
APP_SECRET=$env_lc/aurora-postgres/lives-saved-tool
APP_ROLE=lmd_lives_saved_app
APP_DB=lmd_lives_saved_tool
APPLY=false; [[ "${1:-}" == "--apply" ]] && APPLY=true

TAGS='[{"Key":"lmd-application:cost-center","Value":"LMDCDKApplicationInfrastructure"},
       {"Key":"lmd-application:environment","Value":"'$ENV'"},
       {"Key":"lmd-application:team","Value":"LMDCDKApplicationAdmin"},
       {"Key":"lmd-application:application","Value":"LMDCDKApplicationInfrastructure"}]'

acct=$(aws sts get-caller-identity --query Account --output text)
[[ "$acct" == "$EXPECTED_ACCOUNT" ]] || { echo "Wrong account $acct (want $ENV $EXPECTED_ACCOUNT)"; exit 1; }

admin=$(aws secretsmanager get-secret-value --secret-id "$ADMIN_SECRET" --query SecretString --output text)
HOST=$(jq -r .host <<<"$admin"); PORT=$(jq -r .port <<<"$admin")
ADMIN_USER=$(jq -r .username <<<"$admin")

# SQL comes on stdin so passwords never appear in process args.
pg_admin() {  # usage: pg_admin <dbname> [psql args] < sql
  PGPASSWORD="$(jq -r .password <<<"$admin")" PGSSLMODE=require \
    psql -X -q -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -U "$ADMIN_USER" -d "$1" "${@:2}"
}

echo "Cluster: $HOST  admin: $ADMIN_USER  account: $acct"
db_exists=$(pg_admin postgres -tA <<<"SELECT count(*) FROM pg_database WHERE datname = '$APP_DB'")
role_exists=$(pg_admin postgres -tA <<<"SELECT count(*) FROM pg_roles WHERE rolname = '$APP_ROLE'")
secret_exists=$(aws secretsmanager list-secrets --filters Key=name,Values="$APP_SECRET" \
  --query "length(SecretList[?Name=='$APP_SECRET'])" --output text)
echo "Database $APP_DB exists: $db_exists   Role $APP_ROLE exists: $role_exists   Secret $APP_SECRET exists: $secret_exists"

$APPLY || { echo "Dry run only. Re-run with --apply to make changes."; exit 0; }

# 1. Password: reuse the stored one, or generate and store a new secret (tagged).
if [[ "$secret_exists" == "1" ]]; then
  APP_PASS=$(aws secretsmanager get-secret-value --secret-id "$APP_SECRET" --query SecretString --output text | jq -r .password)
else
  APP_PASS=$(aws secretsmanager get-random-password --password-length 40 --exclude-punctuation \
    --query RandomPassword --output text)
  jq -n --arg host "$HOST" --arg port "$PORT" --arg u "$APP_ROLE" --arg p "$APP_PASS" --arg db "$APP_DB" \
    '{engine:"postgres", host:$host, port:($port|tonumber), username:$u, password:$p, dbname:$db}' \
  | aws secretsmanager create-secret --name "$APP_SECRET" \
      --description "lmh-lives-saved-tool app login on the $ENV portal Aurora cluster (owns $APP_DB only)" \
      --tags "$TAGS" --secret-string file:///dev/stdin --query ARN --output text
fi

# 2. Role: login, no CREATEDB/CREATEROLE, bounded connections. The admin is
#    made a member so it can create the database owned by this role (PG 15).
pg_admin postgres <<SQL
DO \$\$BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$APP_ROLE') THEN
    CREATE ROLE $APP_ROLE LOGIN;
  END IF;
END\$\$;
ALTER ROLE $APP_ROLE WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT CONNECTION LIMIT 20 PASSWORD '$APP_PASS';
GRANT $APP_ROLE TO $ADMIN_USER;
SQL
unset APP_PASS

# 3. Database (CREATE DATABASE can't run inside a DO block / transaction).
if [[ "$(pg_admin postgres -tA <<<"SELECT count(*) FROM pg_database WHERE datname = '$APP_DB'")" == "0" ]]; then
  pg_admin postgres <<<"CREATE DATABASE $APP_DB OWNER $APP_ROLE;"
fi
pg_admin postgres <<SQL
ALTER DATABASE $APP_DB OWNER TO $APP_ROLE;
REVOKE ALL ON DATABASE $APP_DB FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE $APP_DB TO $APP_ROLE;
SQL
echo "Role, database and grants applied."

# 4. Verify as the app login: can create and drop a table in its database.
app=$(aws secretsmanager get-secret-value --secret-id "$APP_SECRET" --query SecretString --output text)
PGPASSWORD="$(jq -r .password <<<"$app")" PGSSLMODE=require \
  psql -X -q -tA -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -U "$APP_ROLE" -d "$APP_DB" <<'SQL'
CREATE TABLE public.__lives_saved_probe(x int);
DROP TABLE public.__lives_saved_probe;
SELECT 'app login OK: ' || current_user || ' on ' || current_database();
SQL
