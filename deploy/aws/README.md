# Deploying FixBridge AI on AWS (database stays on Neon)

```
            ┌──────────── ALB (HTTPS, ACM cert) ────────────┐
 browser ──►│                                                │
            └──► ECS Fargate service "api"  (2+ tasks)       │
                 ECS Fargate service "worker" (1+ tasks) ────┼──► Neon Postgres + pgvector
                 ECS scheduled task "crawler" (nightly)      │      (same region, e.g. us-east-2)
                        │                                    │
                        └──► S3 bucket (private, SSE-KMS) ◄──┘    Secrets Manager: keys & DB URLs
                                                                   CloudWatch Logs: all containers
```

All three run from **one image**; only the command differs:

| Service | Command |
|---|---|
| api | default (`uvicorn app.main:app ...`) |
| worker | `python -m scripts.worker` |
| crawler (scheduled) | `python -m scripts.crawl_manuals --max-pdfs 500 --max-minutes 240` |

## Steps

1. **Neon:** create the database in the AWS region you'll deploy to, enable `vector` (the migration does `CREATE EXTENSION IF NOT EXISTS vector`). Copy the *pooled* connection string to `DATABASE_URL` and the *direct* one to `MIGRATION_DATABASE_URL`.
2. **S3:** create a private bucket (Block Public Access on) and a KMS key. Put their names in `task-definition.json` and `iam-task-policy.json`.
3. **Secrets Manager:** create the secret `fixbridge/prod` with keys `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `FIXBRIDGE_SECRET`, `MEDIA_ENCRYPTION_KEY` (a Fernet key; see `.env.example`), `OPENAI_API_KEY` / `GEMINI_API_KEY`, `SERPAPI_API_KEY`. Keep `MEDIA_ENCRYPTION_KEY` backed up; media can't be decrypted without it.
4. **IAM:** task role = `iam-task-policy.json`. Execution role = `AmazonECSTaskExecutionRolePolicy` + `secretsmanager:GetSecretValue` on `fixbridge/prod`.
5. **Build and push the image:**
   ```bash
   aws ecr create-repository --repository-name fixbridge-ai
   ```
   ```bash
   docker build -t fixbridge-ai . && docker tag fixbridge-ai REPLACE_ACCOUNT.dkr.ecr.REPLACE_REGION.amazonaws.com/fixbridge-ai:latest
   ```
   ```bash
   docker push REPLACE_ACCOUNT.dkr.ecr.REPLACE_REGION.amazonaws.com/fixbridge-ai:latest
   ```
6. **ECS:** register `task-definition.json` for the API service behind an ALB (target health check `/health`; use `/health/ready` for monitoring). Register copies with `command` overridden for the worker and the crawler (EventBridge Scheduler for the crawler). Migrations run automatically on start.
7. **Seed (optional):** run a one-off task with command `python -m scripts.seed` to load the sample knowledge. Skip this in production once real manuals are loaded.

## Notes
- The rate limiter and AI circuit breakers are in memory, so they apply per task. Move them to Redis/ElastiCache if you need them shared.
- `/api/dev/login` only exists when `DEV_AUTH=true`; never set that in production.
- Keep the ECS tasks and Neon in the same region. Each request does several queries, and cross-region latency adds up.
