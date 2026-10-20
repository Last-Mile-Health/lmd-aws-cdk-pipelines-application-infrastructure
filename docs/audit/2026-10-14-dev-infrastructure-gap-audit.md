# Dev Infrastructure Gap Audit — CDK repo vs. live AWS

> **Status: DRAFT for review.** Produced with AI assistance from read-only AWS API calls and a code review on 2026-10-14. Findings must be verified by the infrastructure owner before any resource is imported, changed or deleted.

| | |
|---|---|
| **Account audited** | Dev — `002190277880`, `us-east-1` (other regions checked for CloudFormation and Amplify: empty) |
| **Repo audited** | `lmd-aws-cdk-pipelines-application-infrastructure` @ `main` (`337eb85`) |
| **Related repo reviewed** | `lmd-portal-services` @ `modeling` (`7b498b8c`) |
| **Not audited** | Deployment account `829553079673` (where the CDK pipelines live), Test `576140831944`, Prod `301323023124` |
| **Method** | CloudFormation stack/resource inventory, CloudFormation drift detection on every stack this repo deploys, and a per-service inventory (App Runner, ECR, Amplify, Cognito, RDS, Redshift, DynamoDB, S3, IAM, Secrets Manager, CodePipeline/CodeBuild, connections, Lambda, etc.) matched against stack resources by exact physical ID |

**Goal stated by the team:** one standard way to provision and deploy — **no console changes; this repo's CDK owns all application infrastructure.**

---

## 0. Act now (security)

| # | Issue | Action |
|---|---|---|
| S1 | **A database password was exposed during this audit.** The Amplify app `lmd-Dev-app` stores `DATABASE_URL` / `DATABASE_URL_READER` as plaintext env vars containing the master credentials for the `lib-okr-data` cluster; an audit query printed one into a working session transcript. **Update (Prod check):** the **Prod** frontend (`lmd-Prod-app`) also points `DATABASE_URL` at this same Dev-account cluster — Prod depends on a Dev database. | **Rotate the `lib-okr-data` master password, updating both the Dev *and* Prod Amplify apps in the same change window** (rotating without updating Prod breaks the production portal). Move connection strings to Secrets Manager. Plan to move Prod's OKR data into the Prod account. |
| S2 | **Databases open to the internet.** `dev-lmd-portal-database` (CDK) allows `5432` from `0.0.0.0/0`, and its instances are publicly accessible. `lib-okr-data`, `n8n` and `lmd-1-0-clone` share an SG (`sg-01e9b0054ffe29759`) allowing `0.0.0.0/0` on 5432, 5439 and 3306; `lmd-1-0-clone` also has an SG open on 80/443/all. | Restrict ingress to App Runner VPC connectors / known CIDRs; set `publicly_accessible=False`. |
| S3 | **Long-lived admin access keys.** IAM users with `AdministratorAccess` **and** active keys: `dev-lmd-portal-user` (183 days, used by `lmd-portal-fast`), `client-nextjs-user` (957 days), `n8n` (478 days), `aa-test` (427 days). Non-admin keys up to 1,608 days old (`git-user`, `git-user2`). | Replace with least-privilege IAM roles (App Runner instance roles, GitHub OIDC); delete unused users; rotate the rest. |
| S4 | **Secrets in plaintext env vars.** `lmd-portal-fast` (19 vars incl. `DB_PASS`, `REDSHIFT_PASS`, `SLACK_API_TOKEN`, AWS keys), `lmd-2-agent` (14 vars incl. LangSmith/Slack/Slite keys, AWS keys) and Amplify `lmd-Dev-app` (~47 vars incl. Cognito client secret, Slack secrets, reCAPTCHA secret, Google service-account key, AWS keys) store secrets in plain configuration. | Move to Secrets Manager and reference via App Runner `environment_secrets` / Amplify secrets. |
| S5 | **CDK code prints a secret.** `lib/cognito_stack.py` emits the user-pool client secret as a CloudFormation **output** (`unsafe_unwrap()`), readable by anyone with `cloudformation:Describe*`. | Remove the output. |

---

## 1. Summary

The Dev environment that the portal actually uses today is **mostly console-built**. The CDK repo deploys a parallel set of resources, several of which are drifted, half-deleted, or unused.

**What the Dev frontend actually calls** (from Amplify `lmd-Dev-app` configuration):

| Role | Resource in use | Managed by |
|---|---|---|
| Frontend hosting | Amplify `lmd-Dev-app` (`d1plpcqrwoe4au`), branch `main` | CDK `Dev-Dev-amplify` — **drifted** (build spec + all env vars set in console) |
| Auth | Cognito `Dev-user-pool2` (`us-east-1_fxWImHBZs`, Google IdP, domain `lmd-app-dev3`) | **Console** |
| API (legacy, `LMD_API_BASE_URL`) | App Runner `lmd-portal-services-cdk` | CDK `Dev-Dev-apprunner` — **drifted** (branch changed to `legacy`, build cmd, env vars) |
| API (new, `NEW_LMD_API_BASE_URL`) | App Runner `lmd-portal-fast` (ECR `lmd-portal-services:modeling`) | **Console** + GitHub Action in `lmd-portal-services` |
| AI agent | App Runner `lmd-2-agent` (ECR `lmd-2-agent:latest`) | **Console** |
| Frontend DB (`DATABASE_URL`) | Aurora PG `lib-okr-data` | **Console** |
| Backend DB | Aurora PG `dev-lmd-portal-database` | CDK `Dev-Dev-aurora-postgres` — **drifted** (engine version, SG rule) |
| Warehouse | Redshift Serverless `dev-lmd-v2` | Data-lake CDK stacks (other repo) |
| Upload bucket | `dev-lmd-v2-002190277880-us-east-1-raw` | Data-lake CDK stacks (other repo) |
| OKR reports bucket | `lmd-dp-dev-reports` | **Console** |
| Events table | DynamoDB `portal_ui_useage_events` | CDK stack `Dev-Dev-portal-ui-useage-events` — **orphaned** (stack no longer in code) |
| Workflow orchestration | MWAA `lmd-dp-airflow-v1-dev-mwaa` | Airflow CDK stacks (other repo) |

**Meanwhile, the CDK-built App Runner services `dev-lmd-application-apprunner-docker` and `dev-lmd-application-agent` are not referenced by the frontend at all**, and the backend one's last deployment rolled back (2026-08-01).

| Gap category | Count |
|---|---|
| Application resources live but **not in any IaC** (console-built) | 20+ (Section 2) |
| Stacks from this repo with **drift** | 6 of 9 (Section 3) |
| Stacks/resources from this repo that are **orphaned, partly deleted or unused** | 5 stacks + 9 retained buckets (Section 4) |
| **Code defects / defined-but-not-deployed** items in this repo | 14 (Section 5) |

---

## 2. Live but not in any IaC (console-built) — application scope

These must be **imported** into CDK, **recreated** by CDK, or **retired**. "Import" = `cdk import` / CloudFormation resource import (verify each resource type supports import before planning on it; stateless services are simpler to recreate).

| Resource | Type | Used by / evidence | Recommendation |
|---|---|---|---|
| `lmd-portal-fast` | App Runner service | Frontend `NEW_LMD_API_BASE_URL`; deploys from ECR `:modeling` on every push | **Recreate in CDK** (stateless). Add instance role; drop static keys; HTTP health check `/api/health`; custom autoscaling (see 6.3). Update frontend URL. |
| `lmd-2-agent` | App Runner service | Frontend `LMD_AI_AGENT_BASE_URL`; no instance role; static AWS keys | **Recreate in CDK**, or repoint the existing CDK `AgentAppRunnerStack` service and retire this one. |
| `lmd-portal-services` | ECR repo | Image source for `lmd-portal-fast`; 100+ images, no lifecycle policy, no scan-on-push, mutable tags | **Import** (or create new in CDK). Add lifecycle + scan-on-push. |
| `lmd-2-agent` | ECR repo | Image source for `lmd-2-agent`; 67 images | **Import** or replace. |
| `dev-lmd-application-apprunner-docker`, `dev-lmd-application-agent` | ECR repos | Created by hand for the CDK stacks (stacks *import* them by name) | **Create in CDK**, or keep import-by-name but document the bootstrap step. |
| `AppRunnerECRAccessRole` | IAM role | ECR pull role for console App Runner services | Replace with CDK-defined access roles. |
| `dev_github_connection`, `test`, + one connection named after an individual | App Runner GitHub connections | Source connections for source-code App Runner services | Consolidate to one CDK-referenced connection; delete personal/test ones. |
| `backend-github-integration` | CodeConnections connection | Referenced by `configuration.py` `CODESTAR_CONNECTION_ARN` for Dev | Connections need a one-time console handshake; **record the ARN in config** (already done) and note the manual step in the runbook. Acceptable exception. |
| `strict-conf` | App Runner autoscaling config (min 1/max 1) | Used by `lmd-portal-services-cdk` (attached outside CDK → drift) | Define in CDK. |
| `Dev-user-pool2` (`us-east-1_fxWImHBZs`) | Cognito user pool (39 users), domain `lmd-app-dev3`, Google IdP, client `Dev-app-client` | **The live auth pool** for the frontend | **Import into CDK** (user pools hold user accounts — do not recreate). Then fix `CognitoStack` to match. |
| `lib-okr-data` | Aurora PostgreSQL 17.7 | Frontend `DATABASE_URL` / reader | **Import into CDK** (stateful). Rotate password (S1); restrict SG (S2). |
| `lmd-dp-dev-reports` | S3 bucket | Backend `OKR_REPORTS_BUCKET`; CORS for browser PUT (CORS has malformed origins `https://*.https://…`) | **Import into CDK**; fix CORS origins; add lifecycle for pending uploads. |
| `okr-data` | DynamoDB (PITR off) | Set as `OKR_TABLE_NAME` on `lmd-portal-fast`, but the `modeling` code no longer reads it | Confirm unused → export & retire; else import. |
| `agent_checkpoints` | DynamoDB (PK/SK, empty, PITR off) | Likely agent memory | Confirm owner (agent) → import into agent stack. |
| `lmd-backend-service-build-project` | CodeBuild project | No builds on record; created 2024-02-27 | Retire. |
| `lmd-portal-services-test` | ECR repo | Last push 2024-03-25 | Retire. |
| `lmd-2-portal-static-contents` | S3 bucket | Unknown — likely portal assets | Confirm owner → import or retire. |
| `dev-lmd-portal-user` (+ its secret) | IAM user with admin | Keys used by `lmd-portal-fast` | CDK-managed (`Dev-Dev-iam-user`) but **the pattern itself is the gap** — remove `IamUserStack` once services use roles. |
| Amplify env vars (~47) | App configuration | All set in console | Define non-secret vars in `AmplifyStack`; secrets via Secrets Manager. |
| Bedrock model access (`us.anthropic.claude-sonnet-4-6`) | Account setting | Used by backend notification summaries | Account-level; document as prerequisite (cannot be fully CDK-managed). |

**Other unmanaged resources outside app scope** (need an owner decision, not necessarily this repo): RDS `n8n`, `lmd-1-0-clone`; ECR `n8n`; EC2 `LMD Data Tools` (`i-0836cf29590175032`); Route 53 zones `dev.` / `airbyte.` / `n8n.lastmilehealthdata.org`; ACM certs for airbyte/n8n; 1 CloudFront distribution; Redshift Serverless namespace/workgroup `default`; Cognito pool `data-lake`; Lambda `ifi-pipeline-kobo-fetcher-dev`; Glue job `sick_childtest`; DynamoDB `ifi-pipeline-metadata-dev`, `lmd-dp-airflow-v1-dev-pipeline-metadata`; S3 buckets `lmd-dp-airflow-v1-dev-{assets,raw,processed,curated}`, `lmd-1-0-data-backups`, `lmd2-test-data`, `lmd20redshiftdata`, `lmd-anthena-query-results`, `lmd-codepipeline-artifacts-bucket`; ~15 `sqlworkbench!*` secrets plus `DatabaseProdSecrets`, `Quicksight`, `lmd20`, `lmd-20-dev`, Kobo/DHIS2/Google Drive secrets; 22 SES email identities (not listed here).

---

## 3. Deployed by this repo, but drifted (CloudFormation drift detection)

| Stack | Resource | Drift | Meaning |
|---|---|---|---|
| `Dev-Dev-apprunner` | App Runner `lmd-portal-services-cdk` | Source branch, build command, env vars, autoscaling | Someone retargeted it in the console to branch **`legacy`** and set env vars by hand. It is the frontend's **live legacy API**. The next CDK change to this resource would revert it and **break the legacy API**. |
| `Dev-Dev-apprunner-docker` | App Runner `dev-lmd-application-apprunner-docker` | Env var values | Values filled in by hand (CDK has `""`). Any CDK update to the service resets them. |
| `Dev-Dev-agent-apprunner` | App Runner `dev-lmd-application-agent` | Env var values | Same as above. |
| `Dev-Dev-aurora-postgres` | `dev-lmd-portal-database` | `EngineVersion` (live 15.15 vs code `VER_15_8`); SG ingress (extra `/32` rule) | Minor-version auto-upgrade + a hand-added SG rule. Update code to match. |
| `Dev-Dev-amplify` | Amplify app | `BuildSpec` | Build spec edited in console; also env vars and a second app (see 4). |
| `Dev-Dev-cognito` | User pool client, domain, Google IdP | **DELETED** | The CDK user pool `us-east-1_L1Ealinxy` no longer exists; its client/domain/IdP are gone. Stack is a shell. The live pool is console-built `Dev-user-pool2`. |
| `Dev-Dev-iam-user`, `Dev-Dev-portal-ui-useage-events` | — | In sync | — |

---

## 4. Orphaned, partly deleted, or unused (from this repo)

| Item | Status | Recommendation |
|---|---|---|
| Stack `Dev-Dev-serverless-backend` | **Not in current code** (commented out). MySQL instance, Lambda and permissions **deleted out-of-band**; still holds a Cognito pool, API Gateway `fileuploadapi`, DynamoDB `data_upload_metadata`, S3 `devs3uploadbucket`, a secret. CDK Pipelines does **not** delete stacks removed from code. | Confirm nothing uses `fileuploadapi` / bucket / table → export data → delete stack. |
| Stack `Dev-Dev-portal-ui-useage-events` | **Not in current code**, but its table `portal_ui_useage_events` **is live and used** by the backend. | Re-add the table to CDK (same logical ID / or import into a new stack) so it is owned again. |
| Stack `Dev-Dev-cognito` | Pool deleted (Section 3). | Rebuild `CognitoStack` around an **imported** `Dev-user-pool2`, then remove the dead stack. |
| App Runner `dev-lmd-application-apprunner-docker` | CDK-managed, **not used by the frontend**, last deploy **rolled back** (2026-08-01). Builds from `lmd-portal-services` `main`. | Decide: make this *the* backend service (and retire `lmd-portal-fast`) or delete it. Do not run both. |
| App Runner `dev-lmd-application-agent` | CDK-managed, **not used by the frontend** (frontend uses console `lmd-2-agent`). | Same decision as above for the agent. |
| Amplify app `d1gfzcw5a606s8` | Referenced by hardcoded Cognito callback URLs in `cognito_stack.py` and `configuration.py`, and by leftover EventBridge rule/SNS topic — **app no longer exists**. | Remove references; delete leftover rule/topic. |
| 9 retained S3 artifact buckets | `dev-dev-beanstalk-builtinfunctionidpipelineartifac-*` ×6, `dev-dev-ecs-express-*` ×1, older `dev-dev-apprunner-docker-*` ×2, `dev-dev-agent-apprunner-*` ×2 | Left behind by deleted stacks (bucket names show the `id` bug in 5.6). Empty and delete. |
| Unused secrets | `/Applify/GitHubToken`, `Applify/GitHubToken` (typos), `/AppRunner/GithubConnection` (never accessed) | Delete after confirming. |

---

## 5. Gaps and defects in this repo's code

| # | File | Gap |
|---|---|---|
| 5.1 | `lib/pipeline_deploy_stage.py` | No stack for the services actually used (`lmd-portal-fast`, `lmd-2-agent`, Cognito pool, `lib-okr-data`, reports bucket, events table, `agent_checkpoints`). |
| 5.2 | `lib/pipeline_deploy_stage.py` | `aurora_only` flag name is inverted: `true` *skips* Aurora and deploys only App Runner stacks. Rename (e.g. `apprunner_only`). |
| 5.3 | `lib/apprunner_stack.py` | Uses constant names, not values (`f"{GITHUB_REPOSITORY_OWNER_NAME}/{GITHUB_REPOSITORY_NAME}"` → the literal names), points at the **infra repo** not the services repo, start command `application:application` no longer matches FastAPI (`application:app`), service name not env-prefixed, and calls Secrets Manager via boto3 **at synth time**. Live service has drifted far from it (Section 3). |
| 5.4 | `lib/apprunner_docker_stack.py`, `lib/agent_apprunner_stack.py` | Env vars hardcoded to `""` (any CDK update wipes console values); no `environment_secrets`; health check `/` (backend serves `/api/health`); default autoscaling (max 25 → duplicate in-process scheduler jobs); ~95% duplicated code; Flask-era env vars; ECR repos imported, not created. |
| 5.5 | `lib/cognito_stack.py` | Prints client secret as output (S5); callback/logout URLs hardcoded to a deleted Dev Amplify app for **every** env; Google client ID hardcoded; `ADMIN_EMAIL` personal address as SES sender. |
| 5.6 | `lib/beanstalk_stack.py` | Uses Python builtin `id` in construct IDs (`<built-in function id>` → bucket names `…builtinfunctionid…`); branch hardcoded `main`; deployed stack **does not exist in Dev** though code includes it — pipeline state for this stack unknown (check Deployment account). Likely obsolete. |
| 5.7 | `lib/aurora_postgres_stack.py` | Public subnets + `0.0.0.0/0:5432` + `publicly_accessible=True`; no deletion protection; storage **not encrypted**; backup retention 1 day; engine version pinned below live. |
| 5.8 | `lib/iam_user_stack.py` | Creates an **admin IAM user with static keys** for applications. Should be replaced by instance roles. |
| 5.9 | `lib/amplify_stack.py` | No env vars, no secrets, no custom domain; Test gets an app with no branch; build spec drifted. |
| 5.10 | `lib/pipeline_stack.py` | Infra pipeline CodeBuild role has `kms:*`, `ec2:*`, `vpc:*`, `secretsmanager:*`, `sts:AssumeRole *`; build image `STANDARD_5_0` (old); GitHub auth via PAT secret `/DataLake/GitHubToken` rather than a CodeConnection. |
| 5.11 | `lib/configuration.py` | Test `VPC_ID` and `CODESTAR_CONNECTION_ARN` are placeholders (Test deploy will fail); Dev `VPC_ID` points at a VPC owned by the **data-lake** stack (cross-repo dependency, undocumented); `ADMIN_EMAIL` personal address. |
| 5.12 | Repo | No env-var/secret contract per service; no runbook; `README.md` empty; `resources/` docs describe the original AWS data-lake sample, not this app. |
| 5.13 | Repo | No tests (`cdk synth` snapshot / assertions) and no PR check — drift and broken stacks are only found after deploy. |
| 5.14 | Cross-repo | `lmd-portal-services` deploys `lmd-portal-fast` via its own GitHub Action (`docker-push-ecr.yml`, branch `modeling` only, repo-level AWS vars) — a second, undocumented deployment path. |

---

## 6. Recommended target state ("CDK owns everything")

### 6.1 Principles
1. **Every AWS resource the portal uses is defined in this repo**, per environment, deployed only by the CDK pipeline. Console is read-only (enforce with IAM/SCP once migrated).
2. **Apps deploy by image, infra deploys by CDK.** CDK defines ECR, App Runner, roles, and the build pipeline; a push to the app repo's environment branch builds an image; App Runner auto-deploys it.
3. **No static AWS keys in apps.** App Runner instance roles with least privilege; GitHub → AWS via OIDC if GitHub Actions are kept.
4. **No secrets in env vars or code.** Secrets Manager + `environment_secrets`; a documented env contract per service.
5. **Stateful resources are imported, never recreated** (Cognito pools, Aurora clusters, DynamoDB tables, S3 buckets with data).

### 6.2 Decisions needed (owner)
| Decision | Options |
|---|---|
| Build pipeline for app images | (a) CDK-defined CodePipeline per service (already in repo; fully AWS-side), or (b) GitHub Actions + CDK-defined OIDC role. Pick **one** for all services. |
| Branch → environment mapping for `lmd-portal-services` | Today: CDK uses `main`/`test`/`prod`; live Dev uses `modeling`. Choose the canonical branches. |
| Which backend/agent services survive | Keep the CDK services (`dev-lmd-application-*`) and retire console ones, or vice-versa. |
| Fate of legacy API `lmd-portal-services-cdk` (branch `legacy`) | Keep (codify current state) until frontend stops using `LMD_API_BASE_URL`, then delete. |
| Ownership of shared data resources | Redshift, raw bucket, MWAA, data-lake VPC belong to data-platform repos — document as cross-stack inputs (SSM parameters / exports), not duplicate. |

### 6.3 Suggested phases
1. **Contain (now):** S1–S5; snapshot `lib-okr-data` and `dev-lmd-portal-database`; enable PITR on app DynamoDB tables.
2. **Codify what's live (no behavior change):** import `Dev-user-pool2`, `lib-okr-data`, `lmd-dp-dev-reports`, `portal_ui_useage_events`, `agent_checkpoints`, ECR repos; update `aurora_postgres_stack.py` and `apprunner_stack.py` to match live (stop the drift before the next pipeline run reverts it).
3. **Rebuild stateless services in CDK:** one reusable App Runner construct (service, ECR, roles, autoscaling, secrets, health check, observability) used for backend + agent; cut the frontend over; delete console services and `dev-lmd-portal-user`.
4. **Clean up:** delete orphaned stacks/buckets/secrets (Section 4), obsolete Beanstalk/serverless code.
5. **Guardrails:** `cdk diff` + synth tests on PRs; scheduled drift detection with alerts; restrict console write access.
6. **Repeat for Test and Prod** using the same stacks — Prod should be built only after Dev is codified.

---

## 7. Limits of this audit
- Read-only; one account (Dev) and region `us-east-1` in depth. Deployment, Test and Prod accounts not inspected — the CDK pipeline state (e.g., whether the Beanstalk stack is failing) is unknown.
- IAM roles/policies were only reviewed for app-related services; SES, CloudFront, Route 53 and data-platform resources were inventoried, not assessed.
- "Unmanaged" means not found as a physical resource in any CloudFormation stack in this account/region; a resource could still be managed by Terraform or another tool not checked.
- Drift detection covers properties CloudFormation supports for drift; some App Runner/Amplify properties may not be reported.
- All findings should be verified by the infrastructure owner; resource counts and dates reflect 2026-10-14.
