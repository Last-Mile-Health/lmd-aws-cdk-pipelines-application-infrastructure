# Runbook — Prod database prep for `lmd-portal-services` (`modeling` → `modeling_prod`)

> **Status: DRAFT for review.** Prepared with AI assistance on 2026-10-20 from a code review of `lmd-portal-services` @ `modeling` (`f1172844`), `lmd-portal-UI` @ `main` (`7b1be9c6`) and `lmd-data-pipelines-airflow` @ `ec2-server` (`69a3090`), plus **read-only checks of Prod and Dev Aurora on 2026-10-20** (schemas and row counts only; no row contents read). The infrastructure owner must review and approve every write step.
>
> Related: `docs/plans/2026-10-14-prod-portal-fast-stack-plan.md` (overall plan, blockers B1–B4), `docs/audit/2026-10-14-dev-infrastructure-gap-audit.md`.

---

## 1. Everything `lmd-portal-services` (`modeling`) uses today

| # | Resource | Used for (code) | Dev (live) | Prod status | Prod action |
|---|---|---|---|---|---|
| 1 | **Aurora PG cluster** `<env>-lmd-portal-database` | All Postgres access (`api/database.py`, `api/external_db.py`) | `dev-lmd-portal-database` (CDK) | ✅ available, PG 15.15, 2 instances (checked 10-20) | Reuse. See §2, §4 |
| 2 | ↳ DB `lmd_portal` | Main app DB, Alembic-managed (`DB_NAME`) | ✅ at head `97b0d7775536` | ✅ at `22ba36739202`, no `catalog` schema yet (10-20) | `alembic upgrade head` (Step 4) |
| 3 | ↳ DB `lmd_liberia_okr` | OKR tracker (`OKR_DB_NAME`, `api/configurations.py:65`) | ✅ | ❌ missing (10-20) | Create empty; app creates tables; load reference data (Steps 3, 6) |
| 4 | ↳ DB `lmd_portal_config` | Portal config, home page, temp-credential audit (`PORTAL_CONFIG_DB_NAME`, `:66`) | ✅ | ❌ missing (10-20) | Create empty; app creates tables; load content (Steps 3, 6) |
| 5 | ↳ DB `lmd_datasets` | Data catalog / BI creds (`BI_POSTGRES_DATABASES`, `:59`) | ✅ | ❌ missing (10-20) | Create empty (Step 3) |
| 6 | **Secret** `<env>/aurora-postgres/credentials` | DB host/port/user/pass (one login for all 4 DBs) | ✅ (but `lmd-portal-fast` uses plaintext env vars) | ✅ keys `host, port, username, password, dbname, engine, dbClusterIdentifier` (10-20) | Reference through `environment_secrets` |
| 7 | **Redshift Serverless** | `api/database.py:125`, temp-credential endpoints | `dev-lmd-v2` | `prod-lmd-v2` (public) | Need a user with `CREATEUSER`; password goes in a secret |
| 8 | **S3 upload bucket** | `api/routers/program_data.py:179` (`S3_UPLOAD_BUCKET`) | `dev-lmd-v2-…-raw` | `prod-lmd-v2-301323023124-us-east-1-raw` | Reference (data-lake owned) |
| 9 | **S3 OKR reports bucket** | `api/okr_s3.py` presigned PUT/GET (`OKR_REPORTS_BUCKET`) | `lmd-dp-dev-reports` (console) | ❌ none | Create in CDK |
| 10 | **DynamoDB** `portal_ui_useage_events` | `api/routers/portal_events.py` — `Scan` (parallel segments), `PutItem`, **`BatchWriteItem` (new since 10-14)** | ✅ (orphaned CDK stack) | ✅ exists (orphaned CDK stack) | Reference by name; grant `Scan`, `PutItem`, `BatchWriteItem` |
| 11 | **Bedrock** `us.anthropic.claude-sonnet-4-6` | Notification summaries (`api/resources/utils.py:456`) | ✅ | ✅ authorized | IAM permission only |
| 12 | **Slack** (external) | Notification digests (`SLACK_API_TOKEN`) | plaintext env var | — | Secret |
| 13 | **ECR + App Runner + IAM roles + GitHub OIDC** | Build/run | console `lmd-portal-fast` | ❌ | New `PortalFastStack` (plan §2.2) |

**Not a dependency anymore:** the `okr-data` DynamoDB table (`OKR_TABLE_NAME`) — the `modeling` code doesn't read it. No new env vars are required since 10-14. Two optional settings were added: `PORTAL_EVENTS_DEFAULT_DAYS` (default 90) and `PORTAL_EVENTS_MAX_LIMIT` (default 10000).

**Alembic:** the 3 revisions Prod is missing (`8c06871dfc2e`, `e5fafc911c61`, `97b0d7775536`) are additive and confined to the new `catalog` schema, so they are safe for the legacy Prod API (`legacy_prod`) that shares `lmd_portal`.

---

## 2. Who owns each database's schema (verified against Dev 2026-10-20)

| DB | Database created by | Tables created by | Dev contents (row counts) | Data comes from |
|---|---|---|---|---|
| `lmd_portal` | Manual (CDK only creates `master`) | **`lmd-portal-services` Alembic** | — | App |
| `lmd_liberia_okr` | Manual / airflow `scripts/okr/bootstrap_db.sh` | **`lmd-portal-services` at every startup** (`api/main.py:34` `_ensure_external_tables`: `CREATE SCHEMA IF NOT EXISTS okr` + `create_all`, plus index/column fix-ups). Runs whatever `RUN_MIGRATIONS_ON_STARTUP` is set to | `okr` schema: `OkrObjective` 16, `Okr` 80, `Milestone` 1, `MonthlyUpdate` 158, `OkrReport` 39 | Reference rows: entered in the portal (Dev). Monthly values: airflow `lib_okr_pipeline` |
| `lmd_portal_config` | Manual | **`lmd-portal-services` at every startup** (same function) | `PortalConfig` 1, `HeroSlide` 3, `ReachAtAGlanceMeta` 1, `ReachCountryIndicator` 11, `DataExport` 2, `TempDbCredential` 5 | Admin pages in the portal; `PortalConfig` also from `lmd-portal-UI` `yarn db:seed:portal-config` |
| `lmd_datasets` | Airflow `scripts/db/bootstrap_datasets_db.sh` | Airflow, lazily (`app_meta` schema + per-program schemas) | `app_meta` 5 tables, `public` 1 test-form table | Data Hub forms. **May contain personal or health information** |

**So Prod doesn't need any Prisma or hand-written DDL.** It needs the empty databases to exist before the new service first starts. If a database is missing, startup fails on the `create_all` connection. The Prisma schemas in `lmd-portal-UI` are a client-side mirror only (the portal-config one covers 1 of 6 tables). No `_prisma_migrations` table exists in either Dev DB.

### Findings from the Dev check (outside this deploy, but worth fixing)

- **Airflow OKR pipeline points at the wrong schema.** `config/pipelines/lib_okr_pipeline.yml` sets `schema: public`, and `dags/operators/rds_load.py` queries `public."Okr"`. In Dev the OKR tables exist only in `okr`, so the weekly monthly-value updates can't find their OKRs. Fix: `schema: okr` in the airflow repo (data-platform owner). Check the DAG's recent runs in Dev to confirm.
- **Agent tables in the OKR database.** `lmd_liberia_okr.public` holds LangGraph-style tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`, `query_cache`). They probably come from `lmd-2-agent` pointed at the wrong database (Dev also has `lmd_agent_memory` and `lmd_agent_cache`). Don't copy them to Prod. Check with the agent owner.

---

## 3. Steps

Steps 1–2 are read-only or snapshot-only. Steps 3–6 write to the Prod DB; do them in a planned window, after owner sign-off. Steps 3 and 4 must be done **before** plan pass 2 (the App Runner service is created then). Step 5 is that first start, and Step 6 needs the tables it creates.

### Setup (every session)

```bash
set -a; . ./.aws-temp-creds-prod; set +a
aws sts get-caller-identity --query Account --output text   # must print 301323023124

export AWS_DEFAULT_REGION=us-east-1
export SECRET=prod/aurora-postgres/credentials
# psql against Prod without printing the password to the terminal
pg() {  # usage: pg <dbname> -c "SQL"
  local s; s=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text)
  PGPASSWORD="$(jq -r .password <<<"$s")" PGSSLMODE=require \
    psql -X -v ON_ERROR_STOP=1 -h "$(jq -r .host <<<"$s")" -p "$(jq -r .port <<<"$s")" \
    -U "$(jq -r .username <<<"$s")" -d "$1" "${@:2}"
}
# For read-only checks, prefix with: PGOPTIONS="-c default_transaction_read_only=on"

# Standard tags (lib/tagging.py) for anything created outside CDK
LMD_TAGS_RDS="Key=lmd-application:cost-center,Value=LMDCDKApplicationInfrastructure \
Key=lmd-application:environment,Value=Prod \
Key=lmd-application:team,Value=LMDCDKApplicationAdmin \
Key=lmd-application:application,Value=LMDCDKApplicationInfrastructure"
```

### One cluster, one login, standard tags

- All four databases (`lmd_portal`, `lmd_liberia_okr`, `lmd_portal_config`, `lmd_datasets`) live in the **one** cluster `prod-lmd-portal-database`. They all use the **one** login in `prod/aurora-postgres/credentials` (CDK-managed, stack `Prod-Prod-aurora-postgres`). No new cluster, secret or user is created for them. The new service gets `DB_HOST`/`DB_PORT`/`DB_USERNAME`/`DB_PASS` from that secret, and the app swaps only the database name (`api/config.py` `_database_url_with_name`).
- **Tags:** every AWS resource uses the 4 `lmd-application:*` tags from `lib/tagging.py` (checked on the live cluster, instances, secret, automated snapshots and `portal_ui_useage_events`, 2026-10-20):

  | Key | Prod value |
  |---|---|
  | `lmd-application:cost-center` | `LMDCDKApplicationInfrastructure` |
  | `lmd-application:environment` | `Prod` |
  | `lmd-application:team` | `LMDCDKApplicationAdmin` |
  | `lmd-application:application` | `LMDCDKApplicationInfrastructure` |

  - **CDK resources** (`PortalFastStack`: ECR, roles, OIDC provider, secrets, bucket, autoscaling config, App Runner) get them via `tag(portal_fast_stack, target_environment)` in `lib/pipeline_deploy_stage.py`, like every other stack.
  - **Resources created by CLI** in this runbook (only the manual snapshot) pass `--tags $LMD_TAGS_RDS`.
  - **Postgres databases** inside the cluster are not AWS resources and can't be tagged. They are covered by the cluster's tags (cost shows up under the cluster).
  - Secret **values** set with `put-secret-value` don't create a resource; the secret itself is CDK-created and tagged.

### Step 1 — Verify Prod (read-only) ✅ done 2026-10-20

| Check | Result |
|---|---|
| Cluster | `available`, PG 15.15, 2 instances, **unencrypted, no deletion protection, 1-day backups** |
| Databases | `lmd_agent_memory`, `lmd_portal`, `master`, `postgres`, `rdsadmin` |
| `lmd_portal` Alembic | `22ba36739202`; no `catalog` schema |
| Snapshots | Automated daily only (latest 2026-10-20 07:38 UTC), 1-day retention |

Re-run just before the write window if more than a few days pass.

### Step 2 — Manual snapshot (required before any write)

Manual snapshots are kept until deleted, unlike the 1-day automated ones.

```bash
SNAP=prod-lmd-portal-pre-modeling-$(date +%Y%m%d-%H%M)
aws rds create-db-cluster-snapshot --db-cluster-identifier prod-lmd-portal-database --db-cluster-snapshot-identifier $SNAP \
  --tags $LMD_TAGS_RDS
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP
aws rds describe-db-cluster-snapshots --db-cluster-snapshot-identifier $SNAP --query 'DBClusterSnapshots[0].TagList' --output text   # expect the 4 tags
```

### Step 3 — Create the three empty databases

```bash
for db in lmd_liberia_okr lmd_portal_config lmd_datasets; do
  pg postgres -tA -c "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
    && echo "$db exists" || pg postgres -c "CREATE DATABASE $db"
done
```

(Same result as the airflow `okr/bootstrap_db.sh` and `db/bootstrap_datasets_db.sh` scripts. If you use those instead, pass `ENV_FILE=/dev/null POSTGRES_INSTANCE_SECRET_NAME=$SECRET` so a local `.env` pointing at Dev isn't picked up.)

### Step 4 — `lmd_portal` migrations

From a controlled machine on `modeling` @ the release commit, with the DB env vars taken from the secret and **`RUN_MIGRATIONS_ON_STARTUP=false`** in App Runner:

```bash
cd ../lmd-portal-services
python -m api.bootstrap --current        # expect 22ba36739202
alembic upgrade 22ba36739202:head --sql | less   # review the DDL first (offline mode)
python -m api.bootstrap                  # upgrade to head
python -m api.bootstrap --current        # expect 97b0d7775536
```

### Step 5 — First service start (plan pass 2) creates the OKR and portal-config tables

Check the App Runner logs for `[startup] Ensuring lmd_liberia_okr tables exist...` and `...lmd_portal_config tables exist...` with no error, then:

```bash
pg lmd_liberia_okr -c '\dt okr.*'        # expect the 5 OKR tables
pg lmd_portal_config -c '\dt'            # expect the 6 portal-config tables
```

### Step 6 — Reference and content data (decisions D7, D11)

The new tables start empty: the OKR tracker and the home page will show nothing until this step is done. Options per table:

| Table(s) | Options | Notes |
|---|---|---|
| `okr."OkrObjective"`, `okr."Okr"`, `okr."Milestone"` | Copy from Dev, or have admins enter them in Prod | Airflow only *updates* values for OKRs that already exist (looked up by `okrId`), so these must come first |
| `okr."MonthlyUpdate"` | Copy from Dev, or let the (fixed) airflow pipeline backfill | |
| `okr."OkrReport"` | **Don't copy blindly**: rows point at files in the Dev bucket `lmd-dp-dev-reports` | Copy the files to the Prod bucket too, or start empty |
| `PortalConfig`, `HeroSlide`, `ReachAtAGlanceMeta`, `ReachCountryIndicator` | Copy from Dev, or `yarn db:seed:portal-config` (`PortalConfig` only) + admin entry | Check with the portal owner that Dev content is right for Prod; hero slides may point at Dev image URLs |
| `TempDbCredential`, `DataExport` | **Never copy** | Audit trail of Dev credential issuing / exports; Prod starts empty |

If copying, run a table-limited, data-only dump from Dev **into the tables the app already created** (Step 5). Use `--disable-triggers` only if foreign keys block the load order. Example:

```bash
pg_dump --data-only --no-owner -d lmd_liberia_okr \
  -t 'okr."OkrObjective"' -t 'okr."Okr"' -t 'okr."Milestone"' -t 'okr."MonthlyUpdate"' > okr_ref.sql   # Dev creds
# review okr_ref.sql, then with Prod creds:
pg lmd_liberia_okr < okr_ref.sql
```

Store the dump outside the repo and delete it afterwards.

### Step 7 — Verify

```bash
pg postgres -c "SELECT datname FROM pg_database WHERE datname LIKE 'lmd_%' ORDER BY 1"
pg lmd_liberia_okr -c 'SELECT (SELECT count(*) FROM okr."OkrObjective") obj, (SELECT count(*) FROM okr."Okr") okrs'
```

Then `GET /api/health`; open the OKR tracker, portal config, home page and data catalog; confirm the legacy Prod API (`legacy_prod`) still works.

**Rollback:** each new DB can be dropped independently (`DROP DATABASE …`) without touching `lmd_portal`. For `lmd_portal`, use `python -m api.bootstrap --downgrade 22ba36739202`; as a last resort, restore the Step 2 snapshot to a new cluster.

---

## 4. Database risks to decide on **before** Prod holds Data Hub data

Confirmed 2026-10-20: Prod Aurora is **unencrypted, without deletion protection, with 1-day backups**. On 10-14 it was also publicly accessible with `0.0.0.0/0:5432` (audit S2 / 5.7). `lmd_datasets` may hold personal or health information, so check local data protection requirements with the country teams.

| Change | How | Risk |
|---|---|---|
| Deletion protection on, backup retention ≥ 7 days | `deletion_protection=True`, `backup=rds.BackupProps(retention=Duration.days(7))` in `aurora_postgres_stack.py` | In-place, low. **Can't ship through the pipeline today:** Prod synthesizes with `aurora_only=true` (plan B6), so `Prod-Prod-aurora-postgres` isn't deployed. Apply it with a reviewed `aws rds modify-db-cluster --deletion-protection --backup-retention-period 7`, and update the CDK code to match |
| Encryption at rest | **Do NOT just set `storage_encrypted=True`**: CloudFormation will **replace the cluster** (new, empty DB). Needs an explicit migration: encrypted snapshot copy → restore → import into CDK → cut over the legacy API and new service | Cheapest **now**, while `lmd_datasets` is still empty |
| Close public access | VPC connector for App Runner (plan D2 v2), then restrict the SG | Airflow and BI users also connect, so list every client first |
| Engine version in code | Code pins `VER_15_8`, live is 15.15. Bump the code to match when touching this stack (check the enum exists in the installed CDK version) | Avoids a failed downgrade attempt if the property is ever updated |
| Least-privilege DB login | The app uses the master user for all 4 DBs (also needed today: temp-credential endpoints create roles) | Later improvement |

---

## 5. Decisions needed

| # | Decision | Owner |
|---|---|---|
| D6 | Confirm the schema ownership in §2 (services app creates OKR/portal-config tables; the UI Prisma schemas are a mirror) | Services + frontend leads |
| D7 | Prod OKR reference data: copy from Dev or enter fresh? What happens to `OkrReport` files? | OKR / Liberia program data owner |
| D8 | Fix the airflow OKR pipeline schema (`public` → `okr`) | Data platform |
| D9 | Is there a Prod Airflow that writes `lmd_datasets` / `lmd_liberia_okr` in the Prod account? Its `POSTGRES_INSTANCE_SECRET_NAME` must point at Prod Aurora, not Dev | Data platform |
| D10 | Encrypt Prod Aurora before go-live? (§4) | Infrastructure owner |
| D11 | Prod home page / portal-config content: copy from Dev or enter fresh? | Portal owner |
| — | Agent checkpoint tables in Dev `lmd_liberia_okr.public` (§2) | Agent owner |
| — | Prod frontend `DATABASE_URL` points at Dev's `lib-okr-data` (audit S1). Separate from this work, but must be planned | Infrastructure + frontend |
