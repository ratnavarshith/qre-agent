# Deploying the demo

Phase 4, PR 6. The demo runs on Google Cloud Run from the image in docs/demo-image.md. Every `gcloud` command here is run by hand by the repository owner. The only automation is `.github/workflows/deploy.yml`, which builds, checks, pushes and (when asked) deploys, from `master` only, after a reviewer approves the `demo` environment.

## Settings

| | value | why |
|---|---|---|
| region | `us-central1` | Tier 1 and cheapest. The free-tier page lists no region limit for Cloud Run, but us-central1 is also one of the e2-micro free regions, and Cloud Run's 1 GB/month free egress is "from North America". |
| execution environment | gen2 | a microVM with a real Linux kernel, so the rlimits and the second user behave as in CI (gen1 is gVisor) |
| instances | min 0, max 1 | nothing is billed at zero traffic; max 1 is the hard cap on compute spend and matches the in-memory limits and queue |
| CPU / memory | 1 vCPU, 2 GiB | measured peak 962 MB for one estimate; the worst case of two full sandbox builds plus an estimate is about 2.2 GB by arithmetic and was never measured |
| billing | request-based (the default, CPU throttled outside requests) | free tier applies; the run lives inside the streamed request, so nothing needs CPU between requests |
| request timeout | 360 s | a run is capped at 240 s, plus up to 60 s waiting for a place, plus slack |
| concurrency | 20 | the server takes 2 runs and 6 waiting, plus page, `/limits` and `/healthz` requests |
| `/sandbox-tmp` | in-memory volume, 64 MiB | the broker's TMPDIR; it counts against the 2 GiB |
| image | Artifact Registry `us-central1-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo`, deployed by digest | same region, so pulls are free |
| key | Secret Manager `qre-demo-openrouter-key`, as env var `OPENROUTER_API_KEY`, pinned to version 1 | resolved when an instance starts |
| runtime identity | `qre-demo-run` service account with one role: read that one secret | not the default compute account (it has Editor) |
| deploy identity | `qre-demo-deployer` service account, reached from GitHub Actions through Workload Identity Federation; only `master` of this repo, only from the `demo` GitHub environment | no JSON key anywhere |

## One-time setup (Git Bash)

Set these first. `PROJECT_ID` is yours. `REPO_ID` is the repository's numeric GitHub id: it survives a rename, and a deleted and re-created repo of the same name doesn't get it.

```bash
PROJECT_ID=<your project id>
REGION=us-central1
REPO_ID=$(gh api repos/ratnavarshith/qre-agent --jq .id)
RUN_SA=qre-demo-run@$PROJECT_ID.iam.gserviceaccount.com
DEPLOY_SA=qre-demo-deployer@$PROJECT_ID.iam.gserviceaccount.com
gcloud config set project "$PROJECT_ID"
```

**APIs**

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
```

**Artifact Registry**, keeping only the 2 newest images. That holds storage near the 0.5 GB free tier. The cleanup runs about once a day.

```bash
gcloud artifacts repositories create qre-demo --repository-format=docker --location="$REGION" --description="qre-agent demo image"
cat > cleanup.json <<'EOF'
[
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any"}},
  {"name": "keep-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}}
]
EOF
gcloud artifacts repositories set-cleanup-policies qre-demo --location="$REGION" --policy=cleanup.json --dry-run
gcloud artifacts repositories set-cleanup-policies qre-demo --location="$REGION" --policy=cleanup.json --no-dry-run
```

**The secret.**
- You paste the key at a hidden prompt. `printf %s` adds no newline, the key never appears on the command line or in history, and `unset` drops it afterwards.
- Don't pipe it from PowerShell: that adds a CRLF. `spend.api_key` strips surrounding whitespace anyway, as a guard, since a newline in an HTTP header breaks every call.

```bash
gcloud secrets create qre-demo-openrouter-key --replication-policy=automatic
read -rsp "OpenRouter demo key: " KEY; echo
printf %s "$KEY" | gcloud secrets versions add qre-demo-openrouter-key --data-file=-
unset KEY
```

**The runtime service account.** It can read that one secret and nothing else. Cloud Run writes its logs without any role on the account.

```bash
gcloud iam service-accounts create qre-demo-run --display-name="qre-agent demo (runtime)"
gcloud secrets add-iam-policy-binding qre-demo-openrouter-key --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor
```

**The deploy service account.**
- It can push to this one repository and act as the runtime account when deploying.
- `run.developer` on the service is added after the service exists (step 3).
- It never needs the secret: Cloud Run reads it as the runtime account.

```bash
gcloud iam service-accounts create qre-demo-deployer --display-name="qre-agent demo (GitHub deploy)"
gcloud artifacts repositories add-iam-policy-binding qre-demo --location="$REGION" --member="serviceAccount:$DEPLOY_SA" --role=roles/artifactregistry.writer
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser
```

**Workload Identity Federation.** GitHub's OIDC token is accepted only from this repository (by id), from `refs/heads/master`, in a job that uses the `demo` environment. Only those tokens can act as the deploy account.

```bash
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc qre-agent --location=global --workload-identity-pool=github \
  --display-name="qre-agent" --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref,attribute.environment=assertion.environment" \
  --attribute-condition="assertion.repository_id == '$REPO_ID' && assertion.ref == 'refs/heads/master' && assertion.environment == 'demo'"
POOL=$(gcloud iam workload-identity-pools describe github --location=global --format='value(name)')
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/$POOL/attribute.repository_id/$REPO_ID"
gcloud iam workload-identity-pools providers describe qre-agent --location=global --workload-identity-pool=github --format='value(name)'
```

The last command prints the provider name for the workflow.

**On GitHub** (Settings, Environments, or `gh`):
1. Create the environment `demo`. Make yourself a required reviewer, so every deploy waits for your click, and allow only the `master` branch.
2. Add these repository **variables**, not secrets, since none of them is sensitive:
   - `GCP_PROJECT`
   - `GCP_REGION=us-central1`
   - `GCP_WIF_PROVIDER` (the printed name)
   - `GCP_DEPLOY_SA=$DEPLOY_SA`

## Deploying

`.github/workflows/deploy.yml` (Actions > Deploy > Run workflow) runs on `workflow_dispatch` only, in the `demo` environment, and only from `master`. Its steps:

1. Build the image with the CI `image` job's layer cache.
2. Run `scripts/smoke_image.py` and the sandbox self-check on it, with no network and a dummy key.
3. Authenticate to Google through Workload Identity Federation (`google-github-actions/auth@v3`, short-lived token, no key file).
4. Push the image to `$REGION-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo:<commit sha>`.
5. With `deploy: true`, run `gcloud run deploy qre-demo --image=<repo>@<digest>`. This changes only the image.

The job summary has the digest, the image size and the deployed revision. The first run uses `deploy: false`, because the service doesn't exist until the first deploy below.

## First deploy (by hand, after the first push)

`DIGEST` is the `sha256:...` from the workflow summary.

```bash
gcloud run deploy qre-demo --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo@$DIGEST" \
  --execution-environment=gen2 --cpu=1 --memory=2Gi \
  --min-instances=0 --max-instances=1 --concurrency=20 --timeout=360 --port=8080 \
  --service-account="$RUN_SA" \
  --set-secrets=OPENROUTER_API_KEY=qre-demo-openrouter-key:1 \
  --add-volume=name=sandbox-tmp,type=in-memory,size-limit=64Mi \
  --add-volume-mount=volume=sandbox-tmp,mount-path=/sandbox-tmp \
  --ingress=all --allow-unauthenticated --labels=app=qre-demo
gcloud run services add-iam-policy-binding qre-demo --region="$REGION" --member="serviceAccount:$DEPLOY_SA" --role=roles/run.developer
gcloud run services describe qre-demo --region="$REGION" --format='value(status.url)'
```

- `--allow-unauthenticated` makes it public, which is what a demo is.
- From then on, deploys go through the workflow with `deploy: true`.
- **Permissions caveat:** Google's deploy page says `run.developer` on the service, `serviceAccountUser` on the runtime account and `artifactregistry.reader` on the repository are enough. If the workflow's deploy step is refused anyway, the error names the missing permission. I'd rather add that one than a project-wide role.
- **Undo:** `gcloud run services delete qre-demo --region="$REGION"` removes everything that runs. The repository, secret and service accounts cost nothing at this size, but can be deleted the same way.

## Cost at zero traffic (per month)

| item | expected | basis |
|---|---|---|
| Cloud Run | $0 | min instances 0 and request-based billing: an idle service bills nothing. Under traffic, the free tier (180,000 vCPU-s, 360,000 GiB-s, 2M requests) covers roughly 6,000 runs, far more than the $5 key pays for (about 190 at the eval mix) |
| Artifact Registry | $0 to about $0.05 | 0.5 GB free. The image is 857 MB uncompressed, maybe 0.35 to 0.45 GB compressed (not measured), and two kept. The overage rate of about $0.10 per GB-month is from memory, because Google's pricing page didn't render for me |
| Secret Manager | $0 | 1 active version (6 free), a few reads per cold start (10,000 free) |
| Logging | $0 | one line per run, far under the 50 GiB free |
| network | $0 | pulls in the same region; the page and results are kilobytes (1 GB free from North America) |
| IAM, WIF, GitHub Actions | $0 | public repository |
| **total** | **$0.00 to about $0.05** | plus the OpenRouter key, which is capped at $5 by OpenRouter, not by Google |

The $1 budget alert only emails, and it doesn't stop billing. `max-instances=1` is what bounds Cloud Run spend. The free-tier figures above are from Google's free-tier page; the pricing page didn't render, so I couldn't check the per-unit rates.

## Post-deploy checks, in order

Do these before sharing the URL. Checks 1 to 3 cost nothing. Checks 4 and 5 make real LLM calls, about $0.01 in all.

1. **It's up and configured as planned.**
   - `gcloud run services describe qre-demo --region=$REGION` shows gen2, 2Gi, max 1, timeout 360, the volume and the secret reference.
   - `curl $URL/healthz` returns the versions and table hashes. `curl -I $URL/` returns 200 with the Content-Security-Policy header.
   - In the logs (`gcloud run services logs read qre-demo --region=$REGION`):
     - expected: the broker line ("sandbox broker listening ... as uid 10002") and the server's "sandbox: build_circuit runs behind the broker";
     - not expected: the entrypoint warning that `/sandbox-tmp` couldn't be given to the sandbox user. If it appears, the volume came up root-owned and was made world-writable.
2. **The real `/key` reply** (docs/demo-api.md, check 2). `curl $URL/limits` shows `accepting_runs: true` and `reason: null`. A wrong field name or shape would show as `budget_unknown`.
3. **The sandbox limits on gen2** (check 3).
   - Cloud Run has no `exec`, so the image has a self-check: `qre-entrypoint selfcheck` (`src/qre_agent/selfcheck.py`). The entrypoint starts the broker exactly as for the server, then runs the checks as `app` instead of the server. They:
     - builds through it as the sandbox user;
     - runs a 1.6 GB memory bomb (expect `MemoryError`);
     - fills `/sandbox-tmp` (expect the write to stop at 64 MiB);
     - prints the results.
   - Run it once as a Cloud Run job with the same image, 2 GiB and volume (jobs always run on gen2). It takes about a minute of compute, cents at most:

     ```bash
     gcloud run jobs create qre-selfcheck --region="$REGION" --image="$REGION-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo@$DIGEST" \
       --args=selfcheck --cpu=1 --memory=2Gi --task-timeout=300 --max-retries=0 --service-account="$RUN_SA" \
       --add-volume=name=sandbox-tmp,type=in-memory,size-limit=64Mi --add-volume-mount=volume=sandbox-tmp,mount-path=/sandbox-tmp
     gcloud run jobs execute qre-selfcheck --region="$REGION" --wait
     gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="qre-selfcheck"' --limit=50 --format='value(textPayload)'
     gcloud run jobs delete qre-selfcheck --region="$REGION" --quiet
     ```

     Every line should start with `ok` and the last should say `selfcheck passed`. CI runs the same check in Docker on every image build.
4. **One real run** from the page, with the example prompt.
   - It should end in a result with a verify badge.
   - Compare the run's `cost_usd` in the log with the drop in the key's usage on OpenRouter's site. They should agree within about 20%, the threshold the spend guard warns at.
5. **The client address** (check 1).
   - Send one real run with `X-Forwarded-For: 1.2.3.4`.
   - Then call `GET /limits` three times: with no header, with `1.2.3.4`, and with `5.6.7.8`.
   - All three showing `runs_left.hour` of 3 means the address is the real one.
   - If only the `1.2.3.4` call shows 3, the header is client-controlled. Then the per-address limits are spoofable until `QRE_PROXY_HOPS` is fixed, though the daily cap still bounds cost.
6. **Out of credit** (check 4).
   - Make a second OpenRouter key with a $0 limit, and add it as version 2 of the secret.
   - Deploy a revision that takes no traffic: `gcloud run deploy qre-demo --region=$REGION --image=<same digest> --set-secrets=OPENROUTER_API_KEY=qre-demo-openrouter-key:2 --no-traffic --tag=oocheck`.
   - On its tag URL, `/limits` should say `budget_exhausted` and `POST /estimate` should get 503.
   - Then remove the tag (`gcloud run services update-traffic qre-demo --region=$REGION --remove-tags=oocheck`), disable version 2 (`gcloud secrets versions disable 2 --secret=qre-demo-openrouter-key`) and delete that key at OpenRouter.
7. **Cold start.** Leave it idle for 20 minutes, then time the first `curl $URL/healthz`. In CI the container was ready in 3 s; on gen2 with a 2 GiB image pull it will be longer. Record the number.

## Not verified before the first deploy

- **Per-unit prices:** the Artifact Registry and Secret Manager overage rates come from memory, because the pricing pages didn't render. The free-tier amounts come from Google's free-tier page.
- **Deploy permissions:** I don't know whether `run.developer` bound on the service alone is enough for `gcloud run deploy` to follow its operation to the end.
- **Permissions inside the container on gen2:** I assumed gen2 lets the entrypoint use `setpriv` and `chown` as root, as Docker does. Check 1 (the log lines) and check 3 test that.
