#!/usr/bin/env bash
# Create a read-only Postgres login for the agent on the portal Aurora cluster
# (Dev or Prod), limited to the databases in RO_DBS, and store its credentials in
# Secrets Manager. Idempotent: re-running reuses the stored password and re-applies
# the grants (re-run after new schemas are added; new tables in existing schemas
# are covered by default privileges).
#
# Usage (from the infra repo root, with fresh creds for the target account):
#   set -a; . ./.aws-temp-creds-dev; set +a
#   bash scripts/create-agent-readonly-user.sh            # dry run: checks only
#   bash scripts/create-agent-readonly-user.sh --apply    # make the changes
#   ENV=Prod bash scripts/create-agent-readonly-user.sh   # Prod (use Prod creds)
set -euo pipefail

ENV=${ENV:-Dev}   # Dev | Prod
case "$ENV" in
  Dev)  EXPECTED_ACCOUNT=002190277880; env_lc=dev ;;
  Prod) EXPECTED_ACCOUNT=301323023124; env_lc=prod ;;
  *) echo "ENV must be Dev or Prod"; exit 1 ;;
esac
export AWS_DEFAULT_REGION=us-east-1
ADMIN_SECRET=${ADMIN_SECRET:-$env_lc/aurora-postgres/credentials}
RO_SECRET=${RO_SECRET:-$env_lc/aurora-postgres/agent-readonly}
RO_ROLE=${RO_ROLE:-lmd_agent_ro}
RO_DBS=(lmd_portal lmd_liberia_okr)
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
pg_ro() {  # usage: pg_ro <dbname> < sql   (as the read-only role)
  PGPASSWORD="$(jq -r .password <<<"$ro")" PGSSLMODE=require \
    psql -X -q -tA -h "$HOST" -p "$PORT" -U "$RO_ROLE" -d "$1"
}

USER_SCHEMAS="n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'"

echo "Cluster: $HOST  admin: $ADMIN_USER  account: $acct"
all_dbs=$(pg_admin postgres -tA <<<"SELECT datname FROM pg_database
  WHERE datallowconn AND NOT datistemplate AND datname <> 'rdsadmin' ORDER BY 1")
echo "All databases: $(tr '\n' ' ' <<<"$all_dbs")"
for db in "${RO_DBS[@]}"; do
  grep -qx "$db" <<<"$all_dbs" || { echo "Database $db not found"; exit 1; }
  echo "--- $db: schemas / tables by owner"
  pg_admin "$db" -tA <<<"SELECT n.nspname || ': ' || count(c.oid) || ' tables, owner ' ||
      coalesce(string_agg(DISTINCT pg_get_userbyid(c.relowner), ','), '-')
    FROM pg_namespace n LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','p','v','m','f')
    WHERE $USER_SCHEMAS GROUP BY n.nspname ORDER BY 1"
done
role_exists=$(pg_admin postgres -tA <<<"SELECT count(*) FROM pg_roles WHERE rolname = '$RO_ROLE'")
secret_exists=$(aws secretsmanager list-secrets --filters Key=name,Values="$RO_SECRET" \
  --query "length(SecretList[?Name=='$RO_SECRET'])" --output text)
echo "Role $RO_ROLE exists: $role_exists   Secret $RO_SECRET exists: $secret_exists"

$APPLY || { echo "Dry run only. Re-run with --apply to make changes."; exit 0; }

# 1. Password: reuse the stored one, or generate and store a new secret (tagged).
if [[ "$secret_exists" == "1" ]]; then
  RO_PASS=$(aws secretsmanager get-secret-value --secret-id "$RO_SECRET" --query SecretString --output text | jq -r .password)
else
  RO_PASS=$(aws secretsmanager get-random-password --password-length 40 --exclude-punctuation \
    --query RandomPassword --output text)
  jq -n --arg host "$HOST" --arg port "$PORT" --arg u "$RO_ROLE" --arg p "$RO_PASS" \
    --arg dbs "${RO_DBS[*]}" \
    '{engine:"postgres", host:$host, port:($port|tonumber), username:$u, password:$p,
      dbname:"lmd_portal", allowed_dbs:($dbs|split(" "))}' \
  | aws secretsmanager create-secret --name "$RO_SECRET" \
      --description "Read-only login for the agent on the $ENV portal Aurora cluster (lmd_portal, lmd_liberia_okr)" \
      --tags "$TAGS" --secret-string file:///dev/stdin --query ARN --output text
fi

# 2. Role: login only, read-only by default, bounded connections and query time.
pg_admin postgres <<SQL
DO \$\$BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$RO_ROLE') THEN
    CREATE ROLE $RO_ROLE LOGIN;
  END IF;
END\$\$;
ALTER ROLE $RO_ROLE WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT CONNECTION LIMIT 10 PASSWORD '$RO_PASS';
ALTER ROLE $RO_ROLE SET default_transaction_read_only = on;
ALTER ROLE $RO_ROLE SET statement_timeout = '60s';
ALTER ROLE $RO_ROLE SET idle_in_transaction_session_timeout = '5min';
SQL
unset RO_PASS

# 3. Per-database grants: CONNECT, USAGE on each user schema, SELECT on existing
#    tables/views/sequences, and default SELECT on future tables created by the
#    admin user (the app creates its tables as that user). Tables the admin can't
#    grant on (owned by another role) are reported, not fatal.
for db in "${RO_DBS[@]}"; do
  pg_admin "$db" <<SQL
GRANT CONNECT ON DATABASE "$db" TO $RO_ROLE;
REVOKE TEMPORARY ON DATABASE "$db" FROM $RO_ROLE;
DO \$\$DECLARE s text; t record;
BEGIN
  FOR s IN SELECT n.nspname FROM pg_namespace n WHERE $USER_SCHEMAS LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO $RO_ROLE', s);
    EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO $RO_ROLE', s);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO $RO_ROLE', '$ADMIN_USER', s);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON SEQUENCES TO $RO_ROLE', '$ADMIN_USER', s);
    FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = s AND c.relkind IN ('r','p','v','m','f') LOOP
      BEGIN
        EXECUTE format('GRANT SELECT ON %I.%I TO $RO_ROLE', s, t.relname);
      EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'skipped %.% (not owned by admin)', s, t.relname;
      END;
    END LOOP;
  END LOOP;
END\$\$;
SQL
done
echo "Role and grants applied."

# 4. Verify as the new role: reads allowed DBs, can't write, can't read other DBs.
ro=$(aws secretsmanager get-secret-value --secret-id "$RO_SECRET" --query SecretString --output text)
READABLE="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE $USER_SCHEMAS AND c.relkind IN ('r','p','v','m','f') AND has_table_privilege(c.oid, 'SELECT')"
for db in $all_dbs; do
  readable=$(pg_ro "$db" <<<"$READABLE" 2>&1 || true)
  if printf '%s\n' "${RO_DBS[@]}" | grep -qx "$db"; then
    write=$(pg_ro "$db" <<<"CREATE TABLE public.__agent_ro_probe(x int);" 2>&1 || true)
    echo "ALLOWED $db: readable tables=$readable  write attempt -> ${write:0:80}"
  else
    echo "OTHER   $db: readable tables=${readable:0:80}   (expect 0 or a connect error)"
  fi
done
