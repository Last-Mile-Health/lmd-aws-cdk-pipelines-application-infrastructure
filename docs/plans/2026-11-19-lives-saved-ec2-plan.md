# Plan: deploy lmh-lives-saved-tool on EC2 (2026-11-19)

Status: approved 2026-11-19; implemented in `lib/lives_saved_stack.py`,
`scripts/create-lives-saved-db.sh` and the app repo's `deploy/` +
`.github/workflows/deploy.yml`.

## Decisions

| Topic | Decision |
|---|---|
| Hosting | One EC2 instance per environment (`t3.small`, Amazon Linux 2023, pinned AMI) running docker compose: caddy, web (nginx), api (FastAPI) |
| TLS | Caddy with Let's Encrypt on the instance (chosen over ALB + ACM to save ~$16–20/month per environment). Instance in a public subnet with an Elastic IP; only 80/443 open; no SSH (SSM Session Manager instead) |
| Domains | Dev `dev.livessaved.lastmilehealthdata.org`, Prod `livessaved.lastmilehealthdata.org`. Both are in the Prod account's `lastmilehealthdata.org` zone: Prod's record is in the stack, Dev's is set once by CLI |
| Database | `lmd_lives_saved_tool` on the existing `<env>-lmd-portal-database` Aurora cluster, owned by its own login `lmd_lives_saved_app` (secret `<env>/aurora-postgres/lives-saved-tool`), created by `scripts/create-lives-saved-db.sh` |
| Pipelines | GitHub Actions in the app repo. `main` → Dev, `prod` → Prod, no manual approval. OIDC deploy role per environment (no static keys); images to ECR; SSM Run Command runs `deploy/deploy.sh` on the instance |
| Tagging | `tag()` on the stack (four `lmd-application:*` tags), propagated to the EBS volume; the DB secret is tagged by the script |

## Rollout

Per environment, Dev first:

1. `scripts/create-lives-saved-db.sh` dry run, then `--apply`.
2. Merge the infra PR (Dev) / promote `main` → `prod` (Prod). The pipeline creates `<Env>-<Env>-lives-saved`.
3. Dev only: UPSERT the A record `dev.livessaved.lastmilehealthdata.org` → Dev `PublicIp` output (Prod account, zone `Z06020672MB24JCV66DHV`).
4. App repo: create GitHub environments `dev` (branch `main` only) and `prod` (branch `prod` only); merge the deploy PR to `main` (Dev deploy); later create `prod` from `main` (Prod deploy).
5. Check `https://<domain>/api/health` returns 200.

## Notes

- Approximate cost per environment: t3.small ~$15/month, public IPv4 ~$3.60/month, plus small ECR/logs/Secrets Manager charges. Figures are list-price estimates, not quotes.
- The instance holds no state except Caddy's certificates (re-issued automatically if the instance is replaced; Let's Encrypt allows 5 duplicate certificates per week).
- Existing risk, out of scope: both Aurora security groups allow 5432 from 0.0.0.0/0.
