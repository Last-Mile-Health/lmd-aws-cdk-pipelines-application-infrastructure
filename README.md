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
- [Branches and protection](#branches-and-protection)
- [The FastAPI service (`PortalFastStack`)](#the-fastapi-service-portalfaststack)
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
| Older FastAPI build | `dev-lmd-application-apprunner-docker` · `fpsuu7hkmz` | `prod-lmd-application-apprunner-docker` · `2ut9zyyifn` | nothing: its CodePipeline's Build stage is disabled (it used to build `lmd-portal-services` `main` / `prod`) | `AppRunnerDockerStack` — **not used by the portal**, to be retired |
| (paused) | — | `lmd-portal-services` · `bcv2gn4pfr` | — | console |

The portal (Amplify) is configured in the console with `LMD_API_BASE_URL` / `NEXT_PUBLIC_LMD_API` → legacy API, `NEW_LMD_API_BASE_URL` → FastAPI, and `NEXT_PUBLIC_LMD_AI_AGENT_BASE_URL` → `lmd-2-agent`.

## What gets deployed (and what doesn't)

`lib/pipeline_deploy_stage.py` defines the stacks per environment. The synth step passes **`--context aurora_only=true`** (`lib/pipeline_stack.py`), which, despite its name, **excludes** the older stacks from the deploy stage:

| Stack (`<Env>-<Env>-…`) | File | Deployed by the pipeline? | Notes |
|---|---|---|---|
| `agent-apprunner` | `agent_apprunner_stack.py` | **Yes** | `<env>-lmd-application-agent` App Runner service (code from `lmd-2-agent`) + its CodePipeline. Its instance role can only read `<env>/aurora-postgres/agent-readonly` (see [Databases](#databases)) |
| `apprunner-docker` | `apprunner_docker_stack.py` | **Yes** | Older FastAPI build of `lmd-portal-services` + its CodePipeline. Unused; to be retired. The pipeline's **Build stage is disabled** (`transition_to_enabled=False`, since 2026-11-18) so it no longer builds `lmd-portal-services` `main` / `prod`, which are now the current FastAPI branches |
| `portal-fast` | `portal_fast_stack.py` | **Yes (Prod only)** | The FastAPI service. See below |
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
| **`lmd-data-pipelines-airflow`** and the data-lake CDK repos | Airflow, Redshift Serverless, data-lake VPC and S3 zones | Their own pipelines | **Not here.** This repo only *references* them (e.g. the Redshift endpoint, the raw upload bucket, the data-lake VPC) by name/ARN in `lib/configuration.py` |

### Databases

All application databases live on **one Aurora PostgreSQL cluster per environment** (`<env>-lmd-portal-database`) and share **one login** (`<env>/aurora-postgres/credentials` in Secrets Manager):

| Database | Schema owner |
|---|---|
| `lmd_portal` | `lmd-portal-services` Alembic migrations (shared with the legacy API) |
| `lmd_liberia_okr`, `lmd_portal_config` | `lmd-portal-services` creates its tables at startup (`api/main.py` `_ensure_external_tables`) |
| `lmd_datasets` | Airflow (`lmd-data-pipelines-airflow`), created lazily |

Database creation and migrations are deliberate, reviewed steps, not something the pipeline does. See the DB runbook in [Further reading](#further-reading).

**Agent read-only login.** The AI agent should not use the shared admin login. `scripts/create-agent-readonly-user.sh` creates the Postgres role `lmd_agent_ro` (read-only by default, `lmd_portal` and `lmd_liberia_okr` only, 10 connections, 60 s statement timeout, no temp tables) and stores its login in `<env>/aurora-postgres/agent-readonly`. It's idempotent and a dry run unless you pass `--apply`; re-run it after new schemas are added. It has been run in Dev and Prod (2026-10-26). The agent reads the secret named by its `DB_SECRET_ID` env var; wiring that into `lmd-2-agent` is still open (see [Known gaps](#known-gaps)).

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
  apprunner_docker_stack.py, agent_apprunner_stack.py
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
- **The `apprunner-docker` services** (older FastAPI build) are unused. Their pipelines' Build stage is disabled, but the services, pipelines and ECR repos still exist. Retire them (remove the stack from `pipeline_deploy_stage.py`, after checking what CloudFormation would delete).
- **No branch protection** on any repo (GitHub Free plan). See [Branches and protection](#branches-and-protection).
- **The Test environment** isn't set up.
- **Synth-time AWS calls** in `apprunner_stack.py` (looks up a secret while synthesizing) should go.

## Further reading

- `docs/plans/2026-10-14-prod-portal-fast-stack-plan.md`: design and rollout of `PortalFastStack`, decisions and risks.
- `docs/runbooks/2026-10-20-prod-database-prep.md`: creating the Prod databases, migrations, snapshots, tagging CLI-created resources.
- `docs/audit/2026-10-14-dev-infrastructure-gap-audit.md`: what was console-built or drifted and the target state.
- `lmd-portal-services`: `DEV_DOCS.md` and `dev-docs/` (API design docs, incl. the service-token design).
- AWS docs: [CDK Pipelines](https://docs.aws.amazon.com/cdk/v2/guide/cdk_pipeline.html), [App Runner](https://docs.aws.amazon.com/apprunner/latest/dg/what-is-apprunner.html), [GitHub OIDC for AWS](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services).
