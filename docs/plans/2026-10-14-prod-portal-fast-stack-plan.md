# Plan — CDK stack to deploy `lmd-portal-services` (`modeling_prod`) to Prod

> **Status: DRAFT plan for review.** Nothing here has been built or deployed. Prod and Deployment accounts checked read-only on 2026-10-14 and 2026-10-20 (AI-assisted). Findings must be verified by the infrastructure owner. Related: `docs/audit/2026-10-14-dev-infrastructure-gap-audit.md`.

## 0. Step 0 results — Prod verified 2026-10-14 (read-only)

### Blockers (must clear before anything is promoted to the infra `prod` branch)

| # | Blocker | Evidence | Fix |
|---|---|---|---|
| **B1** | **Update 2026-10-20 — risk is real but currently dormant (see B6).** The live Prod pipeline synthesizes with `aurora_only=true`, so `Prod-Prod-apprunner` is **not in the Prod stage** and a merge to `prod` does not touch the legacy service today. Everything below happens the moment that flag is lost. Keep the fix, but it is no longer the first PR — B6 is. Original finding: **Promoting to `prod` would break the live Prod legacy API.** `cdk diff` on `Prod-Prod-apprunner` (service `lmd-portal-services-cdk`, the API the Prod frontend uses via `LMD_API_BASE_URL`) would (a) change the source repo to the **infra repo** (bug in `apprunner_stack.py`), (b) push the template branch `prod` over the live branch `legacy_prod` (set in console), and (c) swap the instance-role managed policy to `arn:aws:iam::aws:policy/AWSAppRunnerServicePolicyForECRAccess`, **which does not exist** (correct ARN has `service-role/`). | `cdk diff` + live `describe-service` + `iam get-policy` | **Separate PR first:** make `apprunner_stack.py` reproduce the live service exactly (repo `lmd-portal-services`, branch per env — Prod `legacy_prod`, Dev `legacy`; build/start commands; port 5005; 1 vCPU / 2 GB; correct policy ARN). Re-run `cdk diff` until `Prod-Prod-apprunner` shows only intended changes. Note: CDK would also reset its env vars (12 set in console, incl. secrets) — move them to Secrets Manager in the same PR or exclude the stack from the Prod stage until migrated. |
| **B2** | ✅ **Resolved 2026-10-20.** Pipeline `prod-lmd-application-infrastructure-pipeline` (Deployment `829553079673`, GitHub source `prod` branch) is **green**: last run 2026-07-24, commit `d5ef05e` (content identical to `main`). The Prod stage deploys **only** `Prod-agent-apprunner` and `Prod-apprunner-docker`. That explains why `Prod-Prod-apprunner` (2025-04), `-aurora-postgres` (2026-04), `-amplify`, `-cognito`, `-beanstalk` and `-iam-user` haven't changed since. | `codepipeline get-pipeline-state`, `list-pipeline-executions` | — |
| **B3** | **Prod frontend isn't wired for the new API.** Prod Amplify has no `NEW_LMD_API_BASE_URL` (Dev does) — the Prod frontend branch may not call the new API yet. | Amplify env var names | Confirm with frontend owner which `lmd-portal-UI` release talks to the `modeling` API; schedule it with step 8. |
| **B4** | **Databases the `modeling` code needs are missing in Prod.** Prod Aurora has `lmd_portal` (+ `lmd_agent_memory`); **`lmd_liberia_okr`, `lmd_portal_config`, `lmd_datasets` do not exist.** In Dev they exist but are **not** Alembic-managed (schema origin unknown — likely frontend/Prisma or migration scripts). | `pg_database` listing on both clusters | Owner to identify the schema source for each DB (decision D6) and script their creation + data migration. OKR / portal-config / datasets features will fail in Prod until then. **2026-10-20 (verified against Dev):** `lmd-portal-services` creates the OKR/portal-config schema + tables itself at startup (`api/main.py` `_ensure_external_tables`); airflow owns `lmd_datasets`. Prod needs only empty DBs before pass 2, then reference data. Steps in `docs/runbooks/2026-10-20-prod-database-prep.md`. |
| **B5** | **Duplicate Slack notifications once both APIs run in Prod.** Legacy (`legacy_prod`, `api/__init__.py:42`) and FastAPI (`modeling`, `api/scheduler.py:65`) both run "send grouped notifications" at **09:00 and 13:00 UTC** against the same `lmd_portal.notifications` table. Each one reads `is_processed = false` rows, sends, then marks them processed, so two jobs firing in the same minute will both send the same messages. | Code review 2026-10-20 | Keep legacy as the only sender (no change to legacy). Small `modeling` change: a `NOTIFICATIONS_JOB_ENABLED` setting (default `true`) that skips only that job; set it to `false` in Prod until legacy is retired. Temp-credential cleanup and freshness jobs are FastAPI-only and keep running. **Update 2026-10-20:** a **third** possible sender already runs in Prod. `prod-lmd-application-apprunner-docker` (CDK stack `Prod-Prod-apprunner-docker`, built from `lmd-portal-services` branch **`prod`** @ `eb729723`, an older FastAPI) has the same job. Its env var was entered as the *name* `NOTIFICATIONS_TABLE_NAME=notifications_test`, so the app falls back to the real `notifications` table. A CloudWatch check of the 2026-10-19 09:00/13:00 windows was inconclusive (0 lines everywhere). A wider 7-day count of lines matching "notification" (counts only, contents not read) found **~16 for legacy** (≈ its 2 runs/day) and **~210 for `prod-lmd-application-apprunner-docker`**. That service is running notification code, but whether it sends duplicates, logs per message, or errors is **unverified**. Check a redacted log sample or ask Slack recipients. Disable its notification job or pause the service before go-live. |
| **B6** | **Prod (and Dev) pipeline relies on a console edit.** The synth CodeBuild project `ProdLMDCDKApplicationInfras-lFQUnpwiasLv` runs `cdk synth --context aurora_only=true`. The CloudFormation template and `lib/pipeline_stack.py:141` (both `main` and `prod`) say `cdk synth --verbose`. The flag was in code in `8d172b7` (2026-04-02) and later removed; the live project kept it. This flag is the only thing keeping legacy, Aurora, Amplify, Cognito and Beanstalk out of the Prod stage. Any change to the synth step in code (commands, CDK Pipelines upgrade) will overwrite it and deploy **all** stacks (→ B1 breaks legacy). | `codebuild batch-get-projects` vs `cloudformation get-template` | **First PR:** put `--context aurora_only=true` back into `pipeline_stack.py` (no behavior change vs live; `cdk diff` on the Deployment stack should show no change to the project), per env, with a comment. Later: rename the flag (audit 5.2) and replace it with an explicit list of stacks per env. **2026-10-20: done on branch `fix/b6-codify-aurora-only-synth-flag` (`b0ab9d5`), PR pending review.** `cdk diff`: both pipeline stacks change only the synth buildspec + config hash; Prod stage stacks have no differences. Also found: without the flag, synth currently **fails** (`apprunner_stack.py` synth-time lookup of a `github_arn` secret that doesn't exist in the Deployment account), so losing the flag would break the pipeline rather than silently redeploy legacy. That's accidental, not a safeguard. |

### Consequences of B6 for this plan (2026-10-20)

- `PortalFastStack` **must** be created outside the `if not aurora_only:` block (it already was), next to `apprunner-docker` / `agent-apprunner`. Merging to `prod` then deploys: `Prod-agent-apprunner`, `Prod-apprunner-docker`, and the new `Prod-portal-fast`. Run `cdk diff` on the first two as well; their CDK code hardcodes env vars to `""` (audit 5.4), so any template change to them would wipe their console values.
- `AuroraPostgresStack` is **not synthesized** in Prod, so it can't hand its secret object to the new stack. Reference the secret by its **full ARN** from config instead: `arn:aws:secretsmanager:us-east-1:301323023124:secret:prod/aurora-postgres/credentials-dGfxiQ` (`Secret.from_secret_complete_arn`; App Runner needs the full ARN). The ARN is not secret.
- Aurora hardening (deletion protection, backups) **cannot go through CDK** while `aurora_only=true`. Use a reviewed `aws rds modify-db-cluster` (accepting drift), and update `aurora_postgres_stack.py` to match so a future full deploy doesn't revert it.

### Security findings during the 2026-10-20 check (act now)

| # | Issue | Action |
|---|---|---|
| S6 | The App Runner service `prod-lmd-application-agent` has an env var whose **name** contains the full LangSmith API key (`LANGSMITH_API_KEY=…` entered as the name), so the key appears even in name-only listings. It was printed in an AI-assisted working session. | **Rotate the LangSmith key**; fix the variable; move it to Secrets Manager. |
| S7 | The Prod Amplify app `lmd-Prod-app` stores `PIPELINE_API_PASSWORD` (admin user of the pipeline API on a raw IP, `PIPELINE_API_BASE_URL`) as a plaintext env var. Its value was printed in an AI-assisted working session by an overly broad filter. | **Rotate that password**; move it to Secrets Manager / Amplify secrets. |
| — | Prod frontend's AI-agent URL host (`NEXT_PUBLIC_LMD_AI_AGENT_BASE_URL`) is **none** of the 4 App Runner services listed in Prod `us-east-1`. Its source is unknown (another account/region?). | Identify before any agent work; out of scope here. |

### Coexistence rule (owner requirement, 2026-10-20)

The FastAPI service is **added alongside** the legacy Prod API, not a replacement:

| | Legacy (unchanged) | FastAPI (new) |
|---|---|---|
| App Runner service | `lmd-portal-services-cdk` (stack `Prod-Prod-apprunner`), RUNNING | `prod-lmd-portal-fast` (new `PortalFastStack`) |
| Branch | `legacy_prod` (source-code build) | `modeling_prod` (image → ECR → auto-deploy) |
| Frontend env var | `LMD_API_BASE_URL` — **unchanged** | `NEW_LMD_API_BASE_URL` — added (B3) |
| Database | `lmd_portal` | `lmd_portal` (shared) + `lmd_liberia_okr`, `lmd_portal_config`, `lmd_datasets` |
| Slack notifications | ✅ sender | ❌ disabled (B5) |

Changes may touch the legacy service only to **codify it exactly as it runs today** (B1), so a pipeline run can't revert it. Shared-DB changes must stay additive: the 3 pending Alembic revisions only add the `catalog` schema, and the startup `create_all` only touches the three new databases. Retiring legacy is a separate, later decision.

**Third backend in Prod:** `prod-lmd-application-apprunner-docker` (CDK, services branch `prod`, FastAPI from 2026-07) is RUNNING but **not used by the Prod frontend**, whose `LMD_API_BASE_URL` points at legacy. Decision D12 below: retire it once `prod-lmd-portal-fast` is live, or make it *the* FastAPI service instead of adding a new one. Don't run three.

### Other findings that change the design

| Item | Prod state | Plan impact |
|---|---|---|
| CDK bootstrap | v14, trusts Deployment account | OK. |
| Aurora `prod-lmd-portal-database` | PG 15.15, **public**, SG `0.0.0.0/0:5432`, **unencrypted**, no deletion protection, 1-day backups. Secret `prod/aurora-postgres/credentials` exists (CDK). | Use as planned. Hardening is a separate change (audit S2). |
| `lmd_portal` migrations | At `22ba36739202` (`main` head). `modeling` adds 3 revisions (`8c06871dfc2e`, `e5fafc911c61`, `97b0d7775536`) — **all additive, confined to the new `catalog` schema.** | Safe for the legacy API (`legacy_prod`) that shares the DB. Still take a snapshot before step 4. |
| Redshift | Serverless `prod-lmd-v2` (public) | `REDSHIFT_HOST=prod-lmd-v2.301323023124.us-east-1.redshift-serverless.amazonaws.com`. Need a user with `CREATEUSER` (owner to confirm). |
| Upload bucket | `prod-lmd-v2-301323023124-us-east-1-raw` (data-lake stack) | `S3_UPLOAD_BUCKET`. |
| Events table | `portal_ui_useage_events` **already exists** (orphaned CDK stack `Prod-Prod-portal-ui-useage-events`, used by the legacy API) | **Reuse it** (reference by name) — do **not** create `prod_portal_ui_useage_events` (#9 below). Re-owning the orphan stack is an audit item. |
| OKR reports bucket | None in Prod | Create (#8) as planned. |
| GitHub OIDC provider | **None** | Create (#2). |
| Bedrock `us.anthropic.claude-sonnet-4-6` | **Authorized** | Summaries will work once the role allows it. |
| Prod frontend domain | Amplify `lmd-Prod-app`, custom domain `lastmilehealthdata.org` (+ `www`) | CORS origins: `https://lastmilehealthdata.org`, `https://www.lastmilehealthdata.org`. |
| ECR / App Runner name collisions | Existing: `lmd-portal-services` repo + **PAUSED** console service `lmd-portal-services`; CDK `prod-lmd-application-*` services | No collision with `prod-lmd-portal-services` / `prod-lmd-portal-fast`. Paused console service = cleanup item. |
| VPC / NAT | Data-lake VPC `vpc-00ca90a9e4328ba87` (Prod `VPC_ID`) has 3 NAT gateways | A VPC connector (D2 v2) is feasible later without new NAT. |
| Beanstalk | `Prod-Prod-beanstalk` deployed; env `Prod-portal-services` Ready/Grey; an `awseb-…` stack in `CREATE_FAILED` | Not touched by this plan; candidate for retirement. |
| Frontend DB | **Prod frontend's `DATABASE_URL` points to `lib-okr-data` in the *Dev* account** | Cross-environment dependency (see audit S1 update). Out of scope here but must be planned. |

## 1. Goal

Run the `modeling` line of `lmd-portal-services` in **Prod (`301323023124`)** the same way Dev's `lmd-portal-fast` works —

```
push to modeling_prod ─► GitHub Action builds Dockerfile ─► ECR (Prod) :modeling_prod ─► App Runner auto-deploys
```

— but with **every AWS resource defined in this repo's CDK** and deployed only by the Prod CDK pipeline (no console), and without Dev's problems (admin static keys, plaintext secrets, TCP health check, 25-instance autoscaling, duplicate scheduler jobs).

## 2. Design

### 2.1 Responsibilities

| Concern | Owner |
|---|---|
| All AWS resources (ECR, IAM, OIDC trust, App Runner, autoscaling, secrets shells, bucket, table) | **This repo** — new `PortalFastStack` |
| Building & pushing the image | `lmd-portal-services` GitHub Action (assumes a CDK-created role via OIDC — no keys) |
| Rolling out the image | App Runner auto-deploy on new `:modeling_prod` |
| Secret **values** (Slack token, Redshift password, …) | Set once by CLI (`aws secretsmanager put-secret-value`), never in code or env vars |

### 2.2 New stack: `lib/portal_fast_stack.py` → `PortalFastStack`

Generic per environment, enabled for Prod only via config. Names below use `prod`.

| # | Resource | Key settings |
|---|---|---|
| 1 | **ECR repo** `prod-lmd-portal-services` | Created by CDK; `RemovalPolicy.RETAIN`; scan-on-push; lifecycle: keep last 20 tagged, expire untagged after 7 days; tags mutable (App Runner tracks a moving tag) |
| 2 | **GitHub OIDC provider** `token.actions.githubusercontent.com` | Create if absent; if Step 0 finds one, import by ARN (`OpenIdConnectProvider.from_open_id_connect_provider_arn`) — only one per account is allowed |
| 3 | **GitHub deploy role** `prod-lmd-portal-fast-github-deploy` | Trust: `repo:Last-Mile-Health/lmd-portal-services:environment:prod` (aud `sts.amazonaws.com`). Permissions: `ecr:GetAuthorizationToken` + push/pull on repo #1 only |
| 4 | **App Runner access role** | `build.apprunner.amazonaws.com`; pull on repo #1 only |
| 5 | **App Runner instance role** | Least privilege: S3 get/put on upload bucket; S3 put/put-tagging/get/delete on reports bucket #8; DynamoDB `Scan`/`PutItem`/`BatchWriteItem` on table #9 (`BatchWriteItem` needed since `modeling` @ `f1172844`); `bedrock:InvokeModel` on `us.anthropic.claude-sonnet-4-6` inference profile + underlying foundation model; `secretsmanager:GetSecretValue` on secrets #7 and the Aurora secret only. **No IAM user, no static keys.** |
| 6 | **Autoscaling config** `prod-lmd-portal-fast` | min 1 / **max 1** (in-process APScheduler must run once — see 2.4); raise only after the scheduler is made single-leader |
| 7 | **Secrets** | `prod/lmd-portal-fast/secret-key`, `prod/lmd-portal-fast/jwt-secret-key`, `prod/lmd-portal-fast/service-token` — **generated by CDK**. `prod/lmd-portal-fast/external` — JSON shell with keys `REDSHIFT_PASS`, `SLACK_API_TOKEN`, values filled by CLI. DB credentials come from the existing CDK Aurora secret `prod/aurora-postgres/credentials` (fields `host`, `port`, `username`, `password`). |
| 8 | **S3 bucket** `prod-lmd-portal-okr-reports-301323023124` | Block public access; SSE; enforce TLS; **CORS** for Prod frontend origin(s) only (PUT/GET); lifecycle to expire objects still tagged *pending* after 1 day; versioning on; `RETAIN` |
| 9 | **DynamoDB** `portal_ui_useage_events` (existing) | **Referenced, not created** (`Table.from_table_name`) — already exists in Prod (Step 0). Grant `Scan`/`PutItem` only. |
| 10 | **App Runner service** `prod-lmd-portal-fast` | Source ECR #1 tag `modeling_prod`, auto-deploy on; port 5005; 1 vCPU / 3 GB (Dev parity); **HTTP health check `/api/health`**; observability (X-Ray) on; autoscaling #6; env + `environment_secrets` per 2.3 |
| 11 | **Outputs** | ECR URI, deploy-role ARN, service URL (inputs for GitHub env + frontend) |

**Bootstrap problem:** App Runner cannot be created until an image exists in a repo the stack itself creates. Handle with a config flag, deployed in two passes:
`PORTAL_FAST_CREATE_SERVICE: False` (pass 1: everything except #10) → first image pushed → `True` (pass 2: service).

### 2.3 Runtime configuration contract (Prod)

**Plain env vars** (non-secret, in code/config):

| Var | Value |
|---|---|
| `ENVIRONMENT` | `Prod` |
| `AWS_REGION` | `us-east-1` |
| `DB_ENGINE` | `postgresql+psycopg` |
| `DB_NAME` | `lmd_portal` |
| `REDSHIFT_HOST` / `REDSHIFT_PORT` / `REDSHIFT_USER` / `REDSHIFT_DB` / `REDSHIFT_SSLMODE` | Prod values from Step 0 |
| `S3_UPLOAD_BUCKET` | Prod raw bucket (data-platform owned — from Step 0) |
| `OKR_REPORTS_BUCKET` | bucket #8 |
| `PORTAL_EVENTS_TABLE_NAME` | table #9 |
| `NOTIFICATIONS_TABLE_NAME` | `notifications` (Postgres table) |
| `RUN_MIGRATIONS_ON_STARTUP` | `false` (see 2.4) |
| `SCHEDULER_ENABLED` | `true` |
| `WEB_CONCURRENCY` | `1` (needs app change, 2.4) |
| `CORS_ORIGINS` | `["https://<prod frontend domain>"]` |
| `DB_CREDENTIALS_REQUIRE_TOKEN` / `DB_CREDENTIALS_REQUIRE_REQUESTED_BY` | `true` if the Prod frontend sends them (decision D4) |

**Secrets** (`environment_secrets`): `DB_HOST`, `DB_PORT`, `DB_USERNAME`, `DB_PASS` (Aurora secret fields), `SECRET_KEY`, `JWT_SECRET_KEY`, `SERVICE_TOKEN`, `REDSHIFT_PASS`, `SLACK_API_TOKEN`.

**Dropped vs Dev:** `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `OKR_TABLE_NAME` (unused by `modeling` code), Flask vars.

Note: App Runner needs the **full** secret ARN. `AuroraPostgresStack` isn't synthesized in Prod (`aurora_only=true`, B6), so import the secret with `Secret.from_secret_complete_arn` using the ARN stored in `configuration.py` (Prod: `…:secret:prod/aurora-postgres/credentials-dGfxiQ`). Reference fields as `<arn>:host::`, `:port::`, `:username::`, `:password::`. All 4 databases share this one cluster and login.

### 2.4 Changes required in `lmd-portal-services` (separate PR, branch off `modeling`)

1. `gunicorn-cfg.py`: `workers = int(os.getenv("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))` — so Prod can run exactly one worker → one scheduler.
2. `.github/workflows/docker-push-ecr.yml`:
   - `on.push.branches: [modeling, modeling_prod]`
   - `environment: ${{ github.ref_name == 'modeling_prod' && 'prod' || 'dev' }}`
   - remove the access-key fallback for `prod` (OIDC role only)
3. `api/config.py` + `api/scheduler.py`: `NOTIFICATIONS_JOB_ENABLED: bool = True`; skip only the `send_grouped_notifications` job when false (B5). Prod env: `NOTIFICATIONS_JOB_ENABLED=false` while legacy runs.
4. Migrations: with `RUN_MIGRATIONS_ON_STARTUP=false`, run `python -m api.bootstrap` deliberately per release (decision D3). Longer term: wrap migrations + scheduler in a Postgres advisory lock so instances can scale beyond 1.

### 2.5 Changes in this repo

| File | Change |
|---|---|
| `lib/portal_fast_stack.py` | New stack (2.2) |
| `lib/configuration.py` | Per-env block: `PORTAL_FAST_ENABLED`, `PORTAL_FAST_CREATE_SERVICE`, `PORTAL_FAST_BRANCH_TAG` (`modeling_prod`), frontend origins, Redshift host/user/db, upload bucket, existing OIDC provider ARN (optional) |
| `lib/pipeline_stack.py` | **First, separate PR (B6):** synth command `cdk synth --context aurora_only=true --verbose`, matching the live CodeBuild project. `cdk diff` of the Deployment pipeline stacks must show the project's buildspec in line with live |
| `lib/configuration.py` (also) | `AURORA_SECRET_ARN` per env (full ARN) |
| `lib/pipeline_deploy_stage.py` | Instantiate `PortalFastStack` when `PORTAL_FAST_ENABLED`, **outside** the `aurora_only` branch (next to `apprunner-docker`) so it deploys with the flag on; pass the secret ARN from config; `tag(portal_fast_stack, target_environment)` so every resource gets the 4 standard `lmd-application:*` tags (`lib/tagging.py`) |
| `tests/test_portal_fast_stack.py` | CDK assertions: every taggable resource has the 4 `lmd-application:*` tags, no IAM users/access keys, health check HTTP `/api/health`, max instances 1, no plaintext secret env vars, bucket blocks public access, RETAIN on stateful resources |
| `docs/runbooks/portal-fast.md` | Bootstrap, secret-value setup, release, rollback, migration steps |

## 3. Rollout

| Step | What | Gate |
|---|---|---|
| **0-sec. Rotate** | S6 (LangSmith key), S7 (pipeline-API password) — independent of this plan, do now | Rotated |
| **0a. Fix B6** | PR: codify `aurora_only=true` in `pipeline_stack.py`; `cdk diff` against the Deployment account | Owner sign-off on diff |
| **0b. Fix B1** (can follow later; dormant while B6 holds) | PR: make `apprunner_stack.py` match live Prod/Dev legacy services; `cdk diff` clean for `Prod-Prod-apprunner` | Owner sign-off on diff |
| **0. Verify Prod** (read-only) — *done 2026-10-14 and 2026-10-20 (Prod + Deployment); remaining: Redshift user, OIDC provider re-check, `cdk diff`* | Prod CDK pipeline exists and its last run is green; `prod-lmd-portal-database` + `prod/aurora-postgres/credentials` exist; databases `lmd_portal`, `lmd_liberia_okr`, `lmd_portal_config`, `lmd_datasets` exist (or plan to create); Prod Redshift endpoint + a user with `CREATEUSER`; Prod raw bucket name; existing OIDC provider; Bedrock model access for `us.anthropic.claude-sonnet-4-6`; **`cdk diff` with `-c aurora_only=true` for every stack in the Prod stage** (`Prod-agent-apprunner`, `Prod-apprunner-docker`, new `Prod-portal-fast`) | Owner sign-off on the diff |
| 1. Build | Implement 2.5 on a feature branch; `ENV=Prod cdk synth`; tests pass | PR review |
| 2. Pass 1 deploy | Merge to `main`, then promote to `prod` with `PORTAL_FAST_CREATE_SERVICE=False` → Prod pipeline creates ECR, roles, OIDC, secrets, bucket, table | Pipeline green |
| 3. Secrets & GitHub | Put secret values via CLI; create GitHub environment `prod` (vars `AWS_REGION`, `ECR_REPOSITORY`; secret `AWS_ROLE_TO_ASSUME` = deploy-role output) — via `gh` CLI script, recorded in runbook | Values present |
| 4. Database prep | Create missing databases on Prod Aurora; run `python -m api.bootstrap` once against Prod (from a controlled machine/CI job) | `alembic current` = head |
| 5. App repo | Merge 2.4 changes into `modeling`; `git switch -c modeling_prod origin/modeling && git push -u origin modeling_prod` → first image lands in Prod ECR | Image `:modeling_prod` exists |
| 6. Pass 2 deploy | Flip `PORTAL_FAST_CREATE_SERVICE=True`, promote to `prod` → App Runner service created | Service RUNNING |
| 7. Verify | `GET /api/health`; logs show one scheduler start; S3 presigned upload from frontend origin; DynamoDB event write; Slack digest (single); Bedrock summary or graceful fallback | Owner sign-off |
| 8. Frontend | Point the Prod frontend's API URL at the new service (should itself move into `AmplifyStack` — audit gap 5.9) | Smoke test |

**Release after go-live:** merge `modeling` → `modeling_prod` (PR) → Action pushes image → App Runner deploys. Run migrations first if the release contains Alembic revisions.

**Rollback:** re-point `:modeling_prod` at the previous image digest (`aws ecr put-image` with the old manifest) → App Runner redeploys; App Runner also auto-rolls back if the health check fails. Infra rollback = revert the CDK commit on `prod`.

## 4. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| D1 | Branch name | `modeling_prod` (repo uses `modeling`, one "l") |
| D2 | Network egress | v1: default public egress (parity with Dev; Prod Aurora is currently public). v2: VPC connector + NAT, then close Aurora to the internet (audit S2). |
| D3 | Who runs migrations | v1: manual-but-scripted step per release (runbook). v2: advisory-lock-guarded startup migration or a CDK-defined CodeBuild migration job. |
| D4 | Lock down the API in Prod (`SERVICE_TOKEN`, `DB_CREDENTIALS_REQUIRE_*`, CORS) | Yes, if the Prod frontend already sends the token; otherwise ship the frontend change first. These endpoints create database users on Prod data. |
| D5 | Bedrock summaries in Prod | Optional — code falls back to raw text. Include permission; enable model access if wanted. Notification text can include approver emails — confirm this is acceptable under data policy. |
| D6 | Who owns the OKR / portal-config databases in Prod (schemas not created by this app's Alembic) | Resolved in code 2026-10-20 (services app creates them at startup); confirm with owners. DB decisions D7–D11 are in `docs/runbooks/2026-10-20-prod-database-prep.md`. |
| D12 | Fate of `prod-lmd-application-apprunner-docker` (unused FastAPI on services branch `prod`) | Retire after `prod-lmd-portal-fast` is live (or reuse it instead of a new service — but its CodePipeline-from-`prod` model differs from the chosen GitHub Action + `modeling_prod`). Disable its notification job or pause it before go-live (B5). |

## 5. Risks

- **The Prod stage's scope depends on a console edit (B6).** Until it's codified, any synth-step change in code deploys every Prod stack (Amplify, Cognito, Beanstalk, Aurora, IAM user, legacy App Runner). Fix B6 first.
- `Prod-apprunner-docker` and `Prod-agent-apprunner` redeploy on every merge to `prod`. Their console-set env vars survive only while their templates don't change.
- Prod state verified 2026-10-14/20; re-check anything older than a week before a write step.
- Real program data in Prod: temporary-credential endpoints, CORS and migrations need explicit review (D3, D4).
