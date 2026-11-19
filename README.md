# LMD Application Infrastructure (AWS CDK Pipelines)

This repo defines the **AWS infrastructure for the Last Mile Data (LMD) portal applications** as code (AWS CDK, Python) and deploys it through **CDK Pipelines**. It is the single place where application infrastructure is created and changed. If a resource the portal depends on isn't defined here (or in one of the data-platform repos listed below), treat that as a gap to fix, not a pattern to copy.

> **New here?** Read [How it fits together](#how-it-fits-together), [Rules we work by](#rules-we-work-by) and [Making a change](#making-a-change) before your first PR. Ask before running anything that writes to a Prod account.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Accounts and pipelines](#accounts-and-pipelines)
- [Live services](#live-services)
- [What gets deployed (and what doesn't)](#what-gets-deployed-and-what-doesnt)
- [Repositories this repo supports](#repositories-this-repo-supports)
- [App deployment guide](#app-deployment-guide) ← **start here to understand how each app ships**
- [Branches and protection](#branches-and-protection)
- [The FastAPI service (`PortalFastStack`)](#the-fastapi-service-portalfaststack)
- [The Lives Saved Tool (`LivesSavedStack`)](#the-lives-saved-tool-livessavedstack)
- [Rules we work by](#rules-we-work-by)
- [Local setup](#local-setup)
- [Making a change](#making-a-change)
- [Common tasks](#common-tasks)
- [Repo layout](#repo-layout)
- [Known gaps](#known-gaps)
- [Further reading](#further-reading)

---

## How it fits together

```
 GitHub: this repo                                  GitHub: app repos
 ─────────────────                                  ─────────────────
 main  ──webhook──►  Dev  infra pipeline  ─┐        lmd-portal-services  (API)
 prod  ──webhook──►  Prod infra pipeline  ─┤        lmd-portal-UI        (frontend)
                     (Deployment account)  │        lmd-2-agent          (AI agent)
                                           │               │
              cdk synth → self-update →    │               │ push to an environment branch
              deploy stage stacks          │               ▼
                                           │        GitHub Action / CodePipeline builds an image
                                           ▼               │
                ┌──────────── Dev / Prod account ──────────▼───────────┐
                │ ECR repos, App Runner services, IAM roles, secrets,   │
                │ S3 buckets, Aurora PostgreSQL (one cluster), ...      │
                └───────────────────────────────────────────────────────┘
```

- **Infrastructure deploys from this repo.** A merge to `main` deploys to Dev; a merge to `prod` deploys to Prod.
- **Applications deploy by image.** The app repos build Docker images; App Runner picks up each new image automatically. Infra and app releases are separate.
- The pipelines live in a separate **Deployment** account and deploy into the target accounts through the CDK bootstrap trust (`cdk bootstrap --trust`).

## Accounts and pipelines

| Environment | AWS account | Pipeline (in Deployment account) | Branch of this repo |
|---|---|---|---|
| Deployment | `829553079673` | — (hosts the pipelines) | — |
| Dev | `002190277880` | `dev-lmd-application-infrastructure-pipeline` | `main` |
| Test | `576140831944` | not set up (placeholders in `lib/configuration.py`) | `test` |
| Prod | `301323023124` | `prod-lmd-application-infrastructure-pipeline` | `prod` |

All resources are in **`us-east-1`**. Per-environment values (VPC, CodeConnections ARN, secret ARNs, feature flags) live in `lib/configuration.py`.

Each pipeline runs: **Source** (GitHub webhook) → **Build** (`cdk synth --context aurora_only=true`) → **UpdatePipeline** (the pipeline updates itself; it restarts once if its own definition changed) → **deploy stage** (one CloudFormation deploy per stack).

To watch a pipeline you need credentials for the **Deployment** account; to inspect what it deployed you need the Dev or Prod account. Locally these are kept as `.aws-temp-creds-deployment`, `.aws-temp-creds-dev` and `.aws-temp-creds-prod` (short-lived, git-ignored; load with `set -a; . ./.aws-temp-creds-dev; set +a`).

## Live services

What runs today (checked 2026-11-18). All App Runner URLs are `https://<id>.us-east-1.awsapprunner.com`.

| Service | Dev (`002190277880`) | Prod (`301323023124`) | Built from | Defined in |
|---|---|---|---|---|
| **FastAPI** (`lmd-portal-services`) | `lmd-portal-fast` · `cpajhzmcfs` · image `lmd-portal-services:main` | `prod-lmd-portal-fast` · `mr67vamkpf` · image `prod-lmd-portal-services:prod` | branches `main` / `prod` (GitHub Action → ECR) | Dev: console (not in CDK yet). Prod: `PortalFastStack` |
| **Legacy Flask API** (`lmd-portal-services`) | `lmd-portal-services-cdk` · `p8wutvgugr` | `lmd-portal-services-cdk` · `3pf72ntfe3` | branches `legacy-dev` / `legacy-prod` (App Runner builds from source) | `AppRunnerStack` (frozen) |
| **AI agent used by the portal** (`lmd-2-agent`) | `lmd-2-agent` · `5gygvejpie` | **none** (the Prod portal also calls the Dev agent) | image `lmd-2-agent:latest` | console (not in CDK) |
| AI agent (CDK) | `dev-lmd-application-agent` · `j2uiy5wazq` | `prod-lmd-application-agent` · `k39hgjhtpr` | image `<env>-lmd-application-agent:latest` (its CodePipeline) | `AgentAppRunnerStack` — **not used by the portal** |
| (paused) | — | `lmd-portal-services` · `bcv2gn4pfr` | — | console |
| **Lives Saved Tool** (`lmh-lives-saved-tool`) | EC2 `dev-lmh-lives-saved` · https://dev.livessaved.lastmilehealthdata.org | **Planned, not deployed yet** (will be EC2 `prod-lmh-lives-saved` · https://livessaved.lastmilehealthdata.org; see [Prod rollout](#the-lives-saved-tool-livessavedstack)) | branches `main` / `prod` (GitHub Action → ECR → SSM deploy) | `LivesSavedStack` |

The portal (Amplify) is configured in the console with `LMD_API_BASE_URL` / `NEXT_PUBLIC_LMD_API` → legacy API, `NEW_LMD_API_BASE_URL` → FastAPI, and `NEXT_PUBLIC_LMD_AI_AGENT_BASE_URL` → `lmd-2-agent`.

## What gets deployed (and what doesn't)

`lib/pipeline_deploy_stage.py` defines the stacks per environment. The synth step passes **`--context aurora_only=true`** (`lib/pipeline_stack.py`), which, despite its name, **excludes** the older stacks from the deploy stage:

| Stack (`<Env>-<Env>-…`) | File | Deployed by the pipeline? | Notes |
|---|---|---|---|
| `agent-apprunner` | `agent_apprunner_stack.py` | **Yes** | `<env>-lmd-application-agent` App Runner service (code from `lmd-2-agent`) + its CodePipeline. Its instance role can only read `<env>/aurora-postgres/agent-readonly` (see [Databases](#databases)) |
| `portal-fast` | `portal_fast_stack.py` | **Yes (Prod only)** | The FastAPI service. See below |
| `lives-saved` | `lives_saved_stack.py` | **Yes** (Dev live; Prod on the next `main` → `prod` promotion) | Lives Saved Tool on EC2. See [The Lives Saved Tool](#the-lives-saved-tool-livessavedstack) |
| `apprunner` | `apprunner_stack.py` | No (frozen) | **Legacy Flask API** (`lmd-portal-services`, branch `legacy-dev` / `legacy-prod`). Live and used by the frontend. The code no longer matches the running service, so it must not be redeployed until it does |
| `aurora-postgres` | `aurora_postgres_stack.py` | No (frozen) | The shared Aurora PostgreSQL cluster and its secret `<env>/aurora-postgres/credentials` |
| `amplify`, `cognito`, `beanstalk`, `iam-user` | … | No (frozen) | Older stacks; not representative of the live setup |

**Do not remove `aurora_only=true`** from the synth step. Without it, every frozen stack is synthesized and deployed, which would overwrite the live legacy API. Changing which stacks deploy should be an explicit, reviewed change (a per-environment stack list), not a flag flip.

## Repositories this repo supports

| Repo | What it is | How it deploys | Infra here |
|---|---|---|---|
| **`lmd-portal-services`** | Backend API (two apps in one repo, on different branches) | **FastAPI** (`main` → Dev `:main`, `prod` → Prod `:prod`): GitHub Action builds the image → ECR → App Runner auto-deploys. **Legacy Flask API** (`legacy-dev` → Dev, `legacy-prod` → Prod): App Runner builds from source. See [Branches and protection](#branches-and-protection) | `PortalFastStack` (Prod FastAPI); `AppRunnerStack` (legacy, frozen). Dev's FastAPI (`lmd-portal-fast`) predates this repo and is not yet in CDK |
| **`lmd-portal-UI`** | Next.js portal | AWS Amplify (Dev app: branch `main`; Prod app: branch `prod`) | `AmplifyStack` (frozen); Amplify env vars are currently set in the console |
| **`lmd-2-agent`** | AI agent service | App Runner (image). The portal uses the console-built `lmd-2-agent` service in Dev for both environments | `AgentAppRunnerStack` (the CDK-managed agent services, not yet the one the portal uses) |
| **`lmh-lives-saved-tool`** | Lives Saved modeling tool (React web + FastAPI) | `main` → Dev, `prod` → Prod: GitHub Action builds both images → ECR → SSM Run Command restarts docker compose on the instance | `LivesSavedStack` |
| **`lmd-data-pipelines-airflow`** and the data-lake CDK repos | Airflow, Redshift Serverless, data-lake VPC and S3 zones | Their own pipelines | **Not here.** This repo only *references* them (e.g. the Redshift endpoint, the raw upload bucket, the data-lake VPC) by name/ARN in `lib/configuration.py` |

### Databases

All application databases live on **one Aurora PostgreSQL cluster per environment** (`<env>-lmd-portal-database`) and share **one login** (`<env>/aurora-postgres/credentials` in Secrets Manager):

| Database | Schema owner |
|---|---|
| `lmd_portal` | `lmd-portal-services` Alembic migrations (shared with the legacy API) |
| `lmd_liberia_okr`, `lmd_portal_config` | `lmd-portal-services` creates its tables at startup (`api/main.py` `_ensure_external_tables`) |
| `lmd_datasets` | Airflow (`lmd-data-pipelines-airflow`), created lazily |
| `lmd_lives_saved_tool` | `lmh-lives-saved-tool` Alembic migrations (run when the api container starts). **Own login** `lmd_lives_saved_app`, see below |

**Lives Saved Tool login.** `scripts/create-lives-saved-db.sh` creates the database `lmd_lives_saved_tool`, owned by the role `lmd_lives_saved_app` (no CREATEDB/CREATEROLE, 20 connections; `PUBLIC` can't connect), and stores the login in `<env>/aurora-postgres/lives-saved-tool`. Idempotent, and a dry run unless you pass `--apply`.

Database creation and migrations are deliberate, reviewed steps, not something the pipeline does. See the DB runbook in [Further reading](#further-reading).

**Agent read-only login.** The AI agent should not use the shared admin login. `scripts/create-agent-readonly-user.sh` creates the Postgres role `lmd_agent_ro` (read-only by default, `lmd_portal` and `lmd_liberia_okr` only, 10 connections, 60 s statement timeout, no temp tables) and stores its login in `<env>/aurora-postgres/agent-readonly`. It's idempotent and a dry run unless you pass `--apply`; re-run it after new schemas are added. It has been run in Dev and Prod (2026-10-26). The agent reads the secret named by its `DB_SECRET_ID` env var; wiring that into `lmd-2-agent` is still open (see [Known gaps](#known-gaps)).

## App deployment guide

How each app gets from a Git push to a running service, checked against the live accounts on 2026-11-19. Each app section answers: where it runs, what triggers a deploy, how to release to Prod, how to check it, and how to roll back.

### Quick reference: "I want to ship…"

| I changed… | Merge to | Goes live on | Then, for Prod |
|---|---|---|---|
| The portal frontend (`lmd-portal-UI`) | `main` | https://dev.lastmilehealthdata.org | PR `main` → `prod` → https://lastmilehealthdata.org |
| The FastAPI (`lmd-portal-services`) | `main` | Dev `lmd-portal-fast` | PR `main` → `prod` (run migrations first if any) |
| The legacy Flask API (`lmd-portal-services`) | `legacy-dev` | Dev `lmd-portal-services-cdk` | PR `legacy-dev` → `legacy-prod` |
| The AI agent (`lmd-2-agent`) | `main` | Dev `lmd-2-agent` (used by **both** portals) | — (no separate Prod agent yet) |
| The Lives Saved Tool (`lmh-lives-saved-tool`) | `main` | https://dev.livessaved.lastmilehealthdata.org | PR `main` → `prod` (Prod not rolled out yet) |
| Infrastructure (this repo) | `main` | Dev account (Dev pipeline) | PR `main` → `prod` (Prod pipeline) |

Always merge by PR (see [Branches and protection](#branches-and-protection)). App deploys and infra deploys are independent. A new environment variable or permission for an app is an infra change here, not an app change.

### 1. Portal frontend: `lmd-portal-UI` (Next.js on AWS Amplify)

| | Dev | Prod |
|---|---|---|
| Amplify app | `lmd-Dev-app` (Dev account) | `lmd-Prod-app` (Prod account) |
| Branch → URL | `main` → https://dev.lastmilehealthdata.org | `prod` → https://lastmilehealthdata.org and https://www.lastmilehealthdata.org |
| Other branches built | — | `prod-test` and `games` also auto-build (Amplify preview URLs only, not on the custom domain) |

- **Deploy trigger:** Amplify watches the branch and builds and deploys on every push (Amplify's own build, no GitHub Action).
- **Release to Prod:** PR `main` → `prod`.
- **Configuration:** Amplify environment variables, set **in the console** per app/branch:
  - `LMD_API_BASE_URL` and `NEXT_PUBLIC_LMD_API` point to the legacy API.
  - `NEW_LMD_API_BASE_URL` points to the FastAPI.
  - `LMD_API_SERVICE_TOKEN` must equal that environment's FastAPI service token.
  - `NEXT_PUBLIC_LMD_AI_AGENT_BASE_URL` points to `lmd-2-agent`.

  `NEXT_PUBLIC_*` values are baked in at build time, so changing one needs a rebuild.
- **Check / roll back:** Amplify console → the app → branch → build history. "Redeploy this version" on an earlier build rolls back.
- **Infra here:** `AmplifyStack` is frozen and doesn't match the live apps (they and their env vars are console-managed). See [Known gaps](#known-gaps).

### 2. Portal API (FastAPI): `lmd-portal-services`, branches `main` / `prod` (App Runner, image)

| | Dev | Prod |
|---|---|---|
| App Runner service | `lmd-portal-fast` · https://cpajhzmcfs.us-east-1.awsapprunner.com/api | `prod-lmd-portal-fast` · https://mr67vamkpf.us-east-1.awsapprunner.com/api |
| Image | ECR `lmd-portal-services:main` (Dev account) | ECR `prod-lmd-portal-services:prod` (Prod account), also `:prod-<sha>` |
| Built by | `.github/workflows/docker-push-ecr.yml` on push to `main` (AWS access via repo secrets) | `.github/workflows/docker-push-ecr-prod.yml` on push to `prod` (GitHub OIDC, GitHub environment `prod`, no keys) |
| Defined in | Console (not in CDK yet) | `PortalFastStack` |
| Migrations | Run at startup (`RUN_MIGRATIONS_ON_STARTUP` defaults to true) | **Not** at startup (`RUN_MIGRATIONS_ON_STARTUP=false`); run them as a release step |

- **Deploy trigger:** the workflow pushes the image; App Runner has auto-deploy on and rolls out each new image. If the new version fails `/api/health`, App Runner keeps the old one running.
- **Release to Prod:** PR `main` → `prod`. If the release adds Alembic revisions, take a snapshot and run `python -m api.bootstrap` against Prod **before** merging (DB runbook). To release only part of `main`, cut a `release/…` branch from `prod` and cherry-pick (as done for #154 in PR #160).
- **Check:** `curl https://<service>/api/health` returns 200. Every other route needs the `X-Service-Token` header (see [The FastAPI service](#the-fastapi-service-portalfaststack)).
- **Roll back (Prod):** re-point `:prod` at a previous `prod-<sha>` image; App Runner redeploys.
- **Config changes:** Prod env vars and secrets are in `PortalFastStack` (PR here). Dev's are in the console until Dev is codified.

### 3. Legacy Flask API: `lmd-portal-services`, branches `legacy-dev` / `legacy-prod` (App Runner, source)

| | Dev | Prod |
|---|---|---|
| App Runner service | `lmd-portal-services-cdk` · https://p8wutvgugr.us-east-1.awsapprunner.com/api | `lmd-portal-services-cdk` · https://3pf72ntfe3.us-east-1.awsapprunner.com/api |
| Source | branch `legacy-dev` | branch `legacy-prod` |

- **Deploy trigger:** App Runner is connected to GitHub, and **builds from source** on every push to its branch. There's no image or workflow.
- **Release to Prod:** PR `legacy-dev` → `legacy-prod`.
- **Still in use:** the portal's `LMD_API_BASE_URL` points here. Keep changes minimal until it's retired.
- **Infra here:** `AppRunnerStack` is **frozen**. Its code doesn't match the running services, so don't deploy it (that's why `aurora_only=true` exists).

### 4. AI agent: `lmd-2-agent` (App Runner, image)

There are two sets of agent services. Only the first is used.

| | The one the portals use | CDK-managed (not used yet) |
|---|---|---|
| Service | `lmd-2-agent` · https://5gygvejpie.us-east-1.awsapprunner.com (Dev account, used by **both** Dev and Prod portals) | `dev-lmd-application-agent` · `j2uiy5wazq` (Dev), `prod-lmd-application-agent` · `k39hgjhtpr` (Prod) |
| Built by | `lmd-2-agent` repo `.github/workflows/deploy.yml` on push to `main`: builds and pushes ECR `lmd-2-agent:<sha>` and `:latest` (AWS access via static keys in repo secrets) | CodePipeline `<env>-lmd-application-agent-pipeline`, from `lmd-2-agent` `main` (Dev) / `prod` (Prod) through CodeConnections, CodeBuild → ECR `<env>-lmd-application-agent:latest` |
| Deploy | App Runner auto-deploys `:latest` | App Runner auto-deploys `:latest` |
| Defined in | Console | `AgentAppRunnerStack` |

- **Ship a change:** merge to `lmd-2-agent` `main`. It goes live for **both** portals at once, because there's no separate Prod agent yet.
- **Planned:** move the portals to the CDK-managed agents (Prod gets its own), switch the agent to the read-only DB login, remove the static keys. See [Known gaps](#known-gaps).

### 5. Lives Saved Tool: `lmh-lives-saved-tool` (EC2, docker compose)

| | Dev | Prod |
|---|---|---|
| Where | EC2 `dev-lmh-lives-saved` · https://dev.livessaved.lastmilehealthdata.org | **Planned, not deployed** (`prod-lmh-lives-saved` · https://livessaved.lastmilehealthdata.org) |
| Branch | `main` | `prod` (not created yet) |

- **Deploy trigger:** the app repo's `.github/workflows/deploy.yml` runs on every push to `main` / `prod`:
  1. tests;
  2. OIDC login as `<env>-lmh-lives-saved-github-deploy`;
  3. builds and pushes the `api` and `web` images;
  4. an SSM Run Command runs `deploy/deploy.sh` on the instance;
  5. a public `/api/health` check.

  PRs run the tests only.
- **Release to Prod:** PR `main` → `prod` in the app repo. The first Prod release needs the infra promotion first (see [Prod rollout](#the-lives-saved-tool-livessavedstack)).
- **Check:** `curl https://<domain>/api/health` returns `{"status":"ok", "database":"ok"}`. Logs are in CloudWatch `/lmh-lives-saved/<env>`. For a shell, `aws ssm start-session --target <instance id>` (no SSH).
- **Roll back:** re-run the last good Deploy run in the app repo, or merge a revert.
- Full details: [The Lives Saved Tool](#the-lives-saved-tool-livessavedstack).

### 6. Databases (Aurora PostgreSQL)

One cluster per environment (`dev-` / `prod-lmd-portal-database`), shared by the portal APIs, Airflow, the agent (read-only login) and the Lives Saved Tool (own login). Databases aren't deployed by any pipeline: creating a database or login is a script run by hand (`scripts/`), and schema changes are app migrations. See [Databases](#databases).

### 7. This repo (infrastructure)

Merge to `main` → the Dev infra pipeline (Deployment account) deploys the Dev stacks. PR `main` → `prod` → the Prod infra pipeline. Only the stacks listed in [What gets deployed](#what-gets-deployed-and-what-doesnt) deploy. See [Making a change](#making-a-change).

### 8. Leftovers you may run into

These still exist but aren't part of how anything ships today:

- **Prod `Prod-pipeline` (CodePipeline → Elastic Beanstalk `Prod-portal-services`):** created by the frozen `BeanstalkStack`. It still watches `lmd-portal-services` `main` and **fails on every push** (5 failures on 2026-11-18). Ignore the failure emails until it's removed; it doesn't affect the App Runner services.
- **Prod App Runner `lmd-portal-services` (`bcv2gn4pfr`):** paused, image `lmd-portal-services:latest`. Not used.
- **ECR repos and artifact buckets of the retired `apprunner-docker` stack.**

## Branches and protection

**This repo:** `main` (→ Dev) and `prod` (→ Prod). Work on `feature/…`, `fix/…`, `docs/…` branches and merge by PR.

**`lmd-portal-services`** has four long-lived branches (restructured 2026-11-18):

| Branch | App | Deploys to | Was |
|---|---|---|---|
| `main` (default) | FastAPI | Dev `lmd-portal-fast` (`:main`) | `modeling` |
| `prod` | FastAPI | Prod `prod-lmd-portal-fast` (`:prod`) | `modeling_prod` |
| `legacy-dev` | Legacy Flask API | Dev `lmd-portal-services-cdk` | `legacy` |
| `legacy-prod` | Legacy Flask API | Prod `lmd-portal-services-cdk` | `legacy_prod` |

FastAPI flow: feature branch → PR to `main` (Dev) → PR `main` → `prod`. Legacy flow: PR to `legacy-dev` → PR `legacy-dev` → `legacy-prod`. The previous heads of every branch (including the old July-era `main` and `prod`) are kept as tags `archive/<branch>-2026-11-18`.

**Protection is not enforced yet.** The GitHub org is on the Free plan, which doesn't allow branch protection or rulesets on private repos. Until the org moves to a plan that does (e.g. GitHub Team, possibly via GitHub's nonprofit offer), **never push directly to these branches**; always merge by PR. The intended rules for `main`, `prod`, `legacy-dev`, `legacy-prod` (and this repo's `main`, `prod`): PR required with 1 approval, no force-push, no deletion, admins may bypass in an emergency.

## The FastAPI service (`PortalFastStack`)

`lib/portal_fast_stack.py` is the reference for how a service should be defined here. Per environment it creates:

- **ECR repo** `<env>-lmd-portal-services` (scan on push, retained; untagged images expire after 30 days, last 30 commit-tagged images kept).
- **GitHub OIDC provider + deploy role.** Only the app repo's GitHub environment `<env>` can assume it, and only to push to that ECR repo. **No access keys.**
- **App Runner access role** (pull from ECR) and **instance role** (only the AWS calls the app makes).
- **Secrets** passed to the app only through App Runner `environment_secrets`: the Aurora secret (by full ARN), the existing Redshift password secret, generated `SECRET_KEY` / `JWT_SECRET_KEY` / `SERVICE_TOKEN`, and a Slack token placeholder set by CLI.
- **OKR reports bucket** (private, TLS only, versioned, CORS for the portal origins).
- **App Runner service** (pass 2 only): one instance (the app runs an in-process scheduler), HTTP health check `/api/health`, X-Ray, `NOTIFICATIONS_JOB_ENABLED=false` while the legacy API sends Slack digests, `RUN_MIGRATIONS_ON_STARTUP=false`.

**Two-pass bootstrap.** App Runner can't be created before an image exists in the ECR repo the stack creates. So `PORTAL_FAST_CREATE_SERVICE` is `False` for the first deploy (everything but the service), then the app repo pushes its first image, then it is flipped to `True`.

**Service token.** The FastAPI requires the `X-Service-Token` header on **every** route except `GET /api/health`; `/docs` and `/openapi.json` are public (use **Authorize** in `/docs` to try routes). The portal sends it server-side from `LMD_API_SERVICE_TOKEN`. Each environment has its own token in Secrets Manager (`<env>/lmd-portal-fast/service-token`); the API and the portal must hold the same value.

## The Lives Saved Tool (`LivesSavedStack`)

`lib/lives_saved_stack.py` runs `lmh-lives-saved-tool` on **one EC2 instance per environment** (plan: `docs/plans/2026-11-19-lives-saved-ec2-plan.md`).

> **Status:** **Dev is live** (since 2026-11-19). **Prod is planned, not deployed**: it was held back on purpose. The stack is enabled for Prod in `configuration.py` but hasn't been promoted to this repo's `prod` branch.

```
https://<domain> → Elastic IP → EC2 t3.small (public subnet; only 80/443 open, no SSH)
    caddy (Let's Encrypt TLS) → web (nginx: SPA, proxies /api) → api (FastAPI) → Aurora lmd_lives_saved_tool
```

- **Instance:** Amazon Linux 2023 (AMI pinned in `configuration.py` so a new AMI never replaces it unexpectedly), IMDSv2 only, encrypted 30 GB gp3 root volume (tagged). User data installs only Docker and Compose; the app arrives with each deploy. Admin access: **SSM Session Manager** (`aws ssm start-session --target <instance id>`).
- **Instance role:** pull its two ECR repos, read `<env>/aurora-postgres/lives-saved-tool`, write logs to `/lmh-lives-saved/<env>` (30 days), SSM.
- **ECR repos** `<env>-lmh-lives-saved-api` / `-web` (retained; images tagged `<branch>` and `<branch>-<sha>`).
- **GitHub deploy role** `<env>-lmh-lives-saved-github-deploy`: only the app repo's GitHub environment `dev` / `prod` can assume it; it can push those two repos and send SSM commands to that one instance. Dev's stack creates the account's GitHub OIDC provider; Prod reuses the one `PortalFastStack` created.
- **DNS:** Prod's `livessaved.lastmilehealthdata.org` is an A record created by the stack. Dev's `dev.livessaved.lastmilehealthdata.org` is in the **Prod** account's zone, so the Dev stack can't create it; it was set once by CLI to Dev's Elastic IP (see Common tasks).

**Deploy flow** (app repo `.github/workflows/deploy.yml`): push to `main` (Dev) or `prod` (Prod) → tests → build and push both images → SSM Run Command copies `deploy/` to `/opt/lmh-lives-saved` and runs `deploy.sh` (writes `.env` from the DB secret, `docker compose pull && up --wait`, checks `/api/health` through nginx) → the workflow checks `https://<domain>/api/health`.

**Bootstrap order (per environment):** run `scripts/create-lives-saved-db.sh --apply` → the stack deploys (pipeline) → (Dev) set the DNS record → push the app branch.

**Prod rollout (when it's time).** These are already done: the Prod database, login and secret (`prod/aurora-postgres/lives-saved-tool`), and the app repo's `prod` GitHub environment (deployable from `prod` only). What's left, in this order:

1. **Infra:** PR `main` → `prod` here. The Prod pipeline creates `Prod-Prod-lives-saved`, including the DNS record.
2. **App:** create `prod` from `main` in `lmh-lives-saved-tool` and push. The Deploy workflow deploys to Prod. If this runs before step 1 has finished, the deploy job fails at the AWS login; re-run it after step 1.
3. Check https://livessaved.lastmilehealthdata.org/api/health.

> ⚠️ **Any `main` → `prod` promotion of this repo now deploys this stack**, even one made for an unrelated change. If Prod should stay off for longer, set `LIVES_SAVED_ENABLED: False` for Prod in `configuration.py` first.

## Rules we work by

1. **No console changes.** Every application resource is defined in code and deployed by the pipeline. If you had to change something by hand (an incident, a one-off bootstrap step), write it down in the PR/runbook and codify it in a follow-up. Console edits are how this repo drifted (see Known gaps).
2. **Tag everything.** Every stack is passed to `tag(stack, target_environment)` (`lib/tagging.py`), which adds the four `lmd-application:*` tags (cost-center, environment, team, application). Anything created by CLI gets the same four tags explicitly.
3. **No static AWS keys and no IAM users for applications.** Services use App Runner instance roles; GitHub Actions use OIDC roles.
4. **Secrets live in Secrets Manager.** Never in code, never in plain env vars for new services, never printed in logs, PRs or chat. Reference them by full ARN. (Amplify env vars are a known exception: they're plain text in the console.)
5. **Stateful resources are retained and imported, never recreated.** ECR repos, buckets, databases, user pools: `RemovalPolicy.RETAIN`, and import existing ones rather than replacing them. Some property changes replace a resource in CloudFormation (e.g. turning on Aurora storage encryption). Check the diff for "replace" before merging.
6. **Least privilege.** Grant the exact actions and resources a service uses. Wildcard resources need a reason in a code comment.
7. **Each environment is independent.** Separate tokens, secrets and roles per environment. Nothing in Prod should depend on Dev.
8. **Additive database changes**, a snapshot before any Prod schema change, and migrations as a deliberate step (`python -m api.bootstrap` in `lmd-portal-services`), not at service start.
9. **Fix forward.** Don't revert a merged pipeline change to "undo" it. Pipeline settings are now owned by CloudFormation, and a revert can remove settings the pipeline depends on. Write a fix, diff it, merge it.
10. **The legacy API stays as it is** until it is retired. Changes to it are limited to codifying exactly what runs today.

## Local setup

Prerequisites: Python 3.9+, Node.js (for the CDK CLI), the AWS CLI, Docker (optional, for local Postgres in app repos).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
npm install -g aws-cdk

# AWS credentials: use short-lived credentials for the account you need
# (Deployment for pipeline stacks, Dev/Prod for stage stacks). Keep any
# credentials file out of git; `.aws-temp-creds-*` is already ignored.

# Synthesize / diff exactly as the pipeline does. cdk runs `python app.py`,
# so put the venv first on PATH.
export PATH="$PWD/.venv/bin:$PATH"
ENV=Prod cdk synth --context aurora_only=true
ENV=Prod cdk diff 'ProdLMDCDKApplicationInfrastructurePipeline' \
                  'ProdLMDCDKApplicationInfrastructurePipeline/Prod/*' --context aurora_only=true

# Tests (CDK assertions)
.venv/bin/python -m pytest tests/ -q
```

`ENV` selects which pipeline is synthesized (`Dev`, `Test`, `Prod`). `pre-commit` runs flake8 if you install it (`pre-commit install`).

## Making a change

1. **Branch from `main`** (`feature/…`, `fix/…`, `docs/…`).
2. **Write the change and its tests** in `tests/` (CDK assertions on the synthesized template, both bootstrap passes if relevant).
3. **Check it against the live accounts** before opening a PR:
   - `cdk diff` for the pipeline stack **and** the stage stacks of the environments it affects (with `--context aurora_only=true`).
   - `cdk diff` compares against the *deployed template*, not live resources. For anything touching the pipeline (CodeBuild projects, build images, synth commands), also compare the live resource settings with the synthesized template. A console edit invisible to `cdk diff` broke the Dev pipeline once.
4. **PR to `main`** with the diff summary. Merging deploys to **Dev**. Watch the Dev pipeline to green and check that nothing unexpected changed.
5. **Promote to Prod** with a PR from `main` to `prod` once Dev is confirmed. Capture a before/after snapshot of the Prod stacks and services for anything non-trivial.
6. **Expect** an extra pipeline restart when the pipeline itself changes, and `CDKMetadata`-only updates on unchanged stacks when the CDK CLI version moves. Both are harmless.

Commits and PRs: describe *why*, include the evidence (diff, tests), and keep each PR to one concern.

## Common tasks

**Release the FastAPI to Prod:** merge `main` into `prod` in `lmd-portal-services` (PR). The Prod GitHub workflow builds `:prod`, and App Runner deploys it. If the release has Alembic revisions, run the migration step first (DB runbook).

**Roll back the FastAPI:** re-point `:prod` at the previous image (each image is also tagged `prod-<commit sha>`; images from before 2026-11-18 are tagged `modeling_prod-<commit sha>`). App Runner redeploys. App Runner also rolls back by itself if the health check fails.

**Add an environment variable to the FastAPI:** add it to `environment_variables` (non-secret) or `environment_secrets` (secret, by ARN) in `PortalFastStack`, diff, PR. Don't set it in the console.

**Set or rotate a secret value:** `aws secretsmanager put-secret-value --secret-id <name> --secret-string file://<file>` from a private temp file, then redeploy the service so it picks the value up. For the service token, update the portal's Amplify `LMD_API_SERVICE_TOKEN` (per branch) to the same value.

**Lives Saved Tool: point Dev's DNS at its instance** (once, or if the Elastic IP changes): with Prod credentials, UPSERT an A record `dev.livessaved.lastmilehealthdata.org` → the Dev stack's `PublicIp` output in hosted zone `Z06020672MB24JCV66DHV`, TTL 300.

**Lives Saved Tool: roll back:** in the app repo, re-run the last good Deploy run (a re-run uses that run's commit), or merge a revert PR. Images `<branch>-<sha>` stay in ECR.

**Lives Saved Tool: shell on the instance:** `aws ssm start-session --target <InstanceId output>`; the app is in `/opt/lmh-lives-saved` (`docker compose ps`, `docker compose logs api`). Logs are also in CloudWatch `/lmh-lives-saved/<env>`.

**Add a new service:** copy the shape of `PortalFastStack` (own ECR repo, OIDC deploy role, access/instance roles, secrets via `environment_secrets`, health check, autoscaling, `tag()`), gate it with a config flag per environment, and add it in `pipeline_deploy_stage.py` **outside** the `aurora_only` block.

## Repo layout

```
app.py                      # entry point: one PipelineStack per environment (ENV selects)
cdk.json
lib/
  configuration.py          # accounts, regions, per-environment values and feature flags
  pipeline_stack.py         # the CDK Pipeline (source, synth, self-update, deploy stage)
  pipeline_deploy_stage.py  # which stacks deploy per environment
  tagging.py                # the four lmd-application:* tags
  portal_fast_stack.py      # FastAPI service (reference pattern)
  lives_saved_stack.py      # Lives Saved Tool on EC2
  agent_apprunner_stack.py
  apprunner_stack.py, aurora_postgres_stack.py, amplify_stack.py,
  cognito_stack.py, beanstalk_stack.py, iam_user_stack.py   # frozen (not deployed)
  prerequisites/            # one-time account bootstrap scripts (cdk bootstrap, GitHub token secret)
scripts/                    # operational scripts run by hand (e.g. create-agent-readonly-user.sh)
tests/                      # CDK assertion tests
docs/                       # plans, runbooks, audits
resources/                  # docs from the original AWS sample this repo started from (outdated)
```

## Known gaps

Tracked in more detail in the audit and plan under `docs/`:

- **Several stacks are frozen** (legacy API, Aurora, Amplify, Cognito, …) and their code doesn't match what's running. They need to be codified as-is before they can be deployed again.
- **`aurora_only` is misnamed** (it excludes Aurora). It should become an explicit per-environment stack list.
- **Dev's FastAPI (`lmd-portal-fast`) and agent were built in the console**, and Amplify env vars are set in the console.
- **The agent the portal uses (`lmd-2-agent`, Dev account) isn't in CDK, and the Prod portal calls it too.** Prod needs its own agent, and the portal should point at the CDK-managed one.
- **The agent doesn't use the read-only login yet.** `lmd-2-agent` still connects with `DB_URI` and has static AWS keys in its env vars (which take precedence over the instance role). To finish: read the login via `DB_SECRET_ID`, set it on the service, remove the static keys.
- **Leftovers of the retired `apprunner-docker` stack** (removed 2026-11-18): its ECR repos `<env>-lmd-application-apprunner-docker` and pipeline artifact buckets were retained. Delete them once nobody needs the old images.
- **No branch protection** on any repo (GitHub Free plan). See [Branches and protection](#branches-and-protection).
- **The Test environment** isn't set up.
- **Both Aurora clusters' security groups allow 5432 from anywhere** (`0.0.0.0/0`). Access is password-only today; it should be narrowed to the services that need it.
- **Dev's Lives Saved DNS record** is set by CLI (it lives in the Prod account's zone).
- **Static AWS keys in GitHub:** `lmd-2-agent`'s deploy workflow and `lmd-portal-services`'s Dev workflow (`docker-push-ecr.yml`) use access keys stored as repo secrets. Move both to OIDC roles like the Prod workflow and the Lives Saved Tool, then delete the keys.
- **The old Beanstalk pipeline** (`Prod-pipeline`, Prod account) fails on every push to `lmd-portal-services` `main`. Remove it together with the `Prod-portal-services` Beanstalk environment once confirmed unused.
- **Amplify apps and env vars are console-managed** (`AmplifyStack` is frozen), including the service token the portal sends to the FastAPI.
- **Synth-time AWS calls** in `apprunner_stack.py` (looks up a secret while synthesizing) should go.

## Further reading

- `docs/plans/2026-10-14-prod-portal-fast-stack-plan.md`: design and rollout of `PortalFastStack`, decisions and risks.
- `docs/runbooks/2026-10-20-prod-database-prep.md`: creating the Prod databases, migrations, snapshots, tagging CLI-created resources.
- `docs/audit/2026-10-14-dev-infrastructure-gap-audit.md`: what was console-built or drifted and the target state.
- `lmd-portal-services`: `DEV_DOCS.md` and `dev-docs/` (API design docs, incl. the service-token design).
- AWS docs: [CDK Pipelines](https://docs.aws.amazon.com/cdk/v2/guide/cdk_pipeline.html), [App Runner](https://docs.aws.amazon.com/apprunner/latest/dg/what-is-apprunner.html), [GitHub OIDC for AWS](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services).
