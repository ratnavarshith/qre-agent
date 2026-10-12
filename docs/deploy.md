# Deploying the demo

Phase 4, PR 6, updated after the first deploy (2026-10-12). The demo runs on Google Cloud Run from the image in docs/demo-image.md. Every `gcloud` command here is run by hand by the repository owner. The only automation is `.github/workflows/deploy.yml`, which builds, checks, pushes and (when asked) deploys, from `master` only, after a reviewer approves the `demo` environment.

## Settings

| | value | why |
|---|---|---|
| region | `us-central1` | Tier 1 and cheapest. The free-tier page lists no region limit for Cloud Run, but us-central1 is also one of the e2-micro free regions, and Cloud Run's 1 GB/month free egress is "from North America". |
| execution environment | gen2 | a microVM with a real Linux kernel, so the rlimits and the second user behave as in CI (gen1 is gVisor). The self-check on Cloud Run reported kernel 6.9.12 |
| instances | min 0, max 1 | nothing is billed at zero traffic; max 1 is the hard cap on compute spend and matches the in-memory limits and queue |
| CPU / memory | 1 vCPU, 2 GiB | measured peak 962 MB for one estimate; the worst case of two full sandbox builds plus an estimate is about 2.2 GB by arithmetic and was never measured |
| startup CPU boost | on (gcloud's default) | extra CPU for the few seconds an instance starts, billed for those seconds; negligible at this traffic |
| billing | request-based (the default, CPU throttled outside requests) | free tier applies; the run lives inside the streamed request, so nothing needs CPU between requests |
| request timeout | 360 s | a run is capped at 240 s, plus up to 60 s waiting for a place, plus slack |
| concurrency | 20 | the server takes 2 runs and 6 waiting, plus page, `/limits` and `/health` requests |
| `/sandbox-tmp` | in-memory volume, 64 MiB | the broker's TMPDIR; it counts against the 2 GiB |
| image | Artifact Registry `us-central1-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo`, deployed by digest | same region, so pulls are free. 332 MB compressed (857 MB uncompressed) |
| key | Secret Manager `qre-demo-openrouter-key`, as env var `OPENROUTER_API_KEY`, pinned to version **2** | resolved when an instance starts. Version 1 was a bad paste and was destroyed (see The secret) |
| runtime identity | `qre-demo-run` service account with one role: read that one secret | not the default compute account (it has Editor) |
| deploy identity | `qre-demo-deployer` service account, reached from GitHub Actions through Workload Identity Federation; only `master` of this repo, only from the `demo` GitHub environment | no JSON key anywhere |

**Paths ending in `z`.** Cloud Run reserves some URL paths ending in `z` ([known issues](https://cloud.google.com/run/docs/known-issues)): a request for `/healthz` gets Google's own 404 and never reaches the container. The public health check is `/health` (GET or HEAD); `/healthz` answers the same, but only inside the container (the smoke test).

## Git Bash on Windows

Git Bash rewrites any argument that looks like a Unix path before handing it to a Windows program. `mount-path=/sandbox-tmp` reaches gcloud as a Windows path, and Cloud Run refuses it ("should be a valid unix absolute path"). Exempt that one argument:

```bash
MSYS2_ARG_CONV_EXCL="--add-volume-mount" gcloud run deploy ...
```

Don't use `MSYS_NO_PATHCONV=1` instead: it also stops the rewriting gcloud's own launcher needs, and gcloud then can't find itself. Every command below that passes `--add-volume-mount` has the prefix. PowerShell doesn't rewrite paths, so the same commands work there without it.

## One-time setup (Git Bash)

Set these first, and keep the window open: later commands use them. `REPO_ID` is the repository's numeric GitHub id: it survives a rename, and a deleted and re-created repo of the same name doesn't get it.

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

**Artifact Registry**, keeping only the 2 newest images. That holds storage near the 0.5 GB free tier. The cleanup runs about once a day. The policy file goes to a temp path, not the repository.

```bash
gcloud artifacts repositories create qre-demo --repository-format=docker --location="$REGION" --description="qre-agent demo image"
POLICY=$(mktemp)
cat > "$POLICY" <<'EOF'
[
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any"}},
  {"name": "keep-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}}
]
EOF
gcloud artifacts repositories set-cleanup-policies qre-demo --location="$REGION" --policy="$POLICY" --dry-run
gcloud artifacts repositories set-cleanup-policies qre-demo --location="$REGION" --policy="$POLICY" --no-dry-run
rm "$POLICY"
```

**The secret.** Run these **one line at a time**, not pasted as a block. A pasted block feeds the following lines to `read` as if they were the key: the first deploy's version 1 stored 370 characters of pasted text instead of a 73-character key, and was destroyed.

1. Create the secret:
   ```bash
   gcloud secrets create qre-demo-openrouter-key --replication-policy=automatic
   ```
2. Read the key at a hidden prompt. Paste with **Shift+Insert** or right-click > Paste (Ctrl+V doesn't paste in Git Bash), then press Enter. Nothing shows while you paste; that's on purpose:
   ```bash
   read -rsp "OpenRouter demo key: " KEY; echo
   ```
3. Check the length before storing it. An `sk-or-v1-` key is **73** characters. On any other number, `unset KEY` and go back to step 2:
   ```bash
   echo ${#KEY}
   ```
4. Store it and forget it. `printf %s` adds no newline, and the key never appears on the command line or in history:
   ```bash
   printf %s "$KEY" | gcloud secrets versions add qre-demo-openrouter-key --data-file=-; unset KEY
   ```
5. Check what was stored, without printing it (expect `73`, then `sk-or-v1-`; use the version number step 4 printed):
   ```bash
   gcloud secrets versions access 2 --secret=qre-demo-openrouter-key | wc -c
   gcloud secrets versions access 2 --secret=qre-demo-openrouter-key | cut -c1-9
   ```

Don't pipe the key from PowerShell: that adds a CRLF. `spend.api_key` strips surrounding whitespace anyway, as a guard, since a newline in an HTTP header breaks every call. A bad version is disabled with `gcloud secrets versions disable N --secret=qre-demo-openrouter-key`, or removed for good with `destroy` (permanent).

**The runtime service account.** It can read that one secret and nothing else. Cloud Run writes its logs without any role on the account.

```bash
gcloud iam service-accounts create qre-demo-run --display-name="qre-agent demo (runtime)"
gcloud secrets add-iam-policy-binding qre-demo-openrouter-key --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor
```

**The deploy service account.**
- It can push to this one repository and act as the runtime account when deploying.
- `run.developer` on the service is added after the service exists (First deploy).
- It never needs the secret: Cloud Run reads it as the runtime account.

```bash
gcloud iam service-accounts create qre-demo-deployer --display-name="qre-agent demo (GitHub deploy)"
gcloud artifacts repositories add-iam-policy-binding qre-demo --location="$REGION" --member="serviceAccount:$DEPLOY_SA" --role=roles/artifactregistry.writer
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser
```

A new service account can take a few seconds to become usable: if a binding says the account doesn't exist, wait 30 seconds and run it again.

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
1. Create the environment `demo`. Make yourself a required reviewer, so every deploy waits for your click, and allow only the `master` branch. By default admins can bypass the approval (`can_admins_bypass: true`); turn that off under Settings > Environments > demo if it should hold for you too.
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
5. With `deploy: true`, run `gcloud run deploy qre-demo --image=<repo>@<digest>`. This changes only the image: every other setting, the secret version included, stays as the last revision had it.

The job summary has the digest, the image size and the deployed revision. The first run uses `deploy: false`, because the service doesn't exist until the first deploy below. Start a run with `gh workflow run deploy.yml --ref master -f deploy=true`, then approve it under the run's **Review deployments**.

**Measurement tables differ from CI's.** IBM's compiler generates its measurement tables with a search that breaks cost ties in `HashMap` order, so every generation gives slightly different tables (bicycle timesteps by about 1%). The Deploy build generated its own tables, so the live tables (gross `fcc2ed47…`, two-gross `21c4a3e0…`) are not the ones CI's builds reported. Every result, and `/health`, reports the hashes of the tables it used, so any bicycle number can be traced to its table. Upstream [PR #28](https://github.com/qiskit-community/bicycle-architecture-compiler/pull/28) makes the generation deterministic; it's open, not merged.

## First deploy (by hand, after the first push)

`DIGEST` is the `sha256:...` from the workflow summary. The `MSYS2_ARG_CONV_EXCL` prefix is for Git Bash (see above).

```bash
MSYS2_ARG_CONV_EXCL="--add-volume-mount" gcloud run deploy qre-demo --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo@$DIGEST" \
  --execution-environment=gen2 --cpu=1 --memory=2Gi \
  --min-instances=0 --max-instances=1 --concurrency=20 --timeout=360 --port=8080 \
  --service-account="$RUN_SA" \
  --set-secrets=OPENROUTER_API_KEY=qre-demo-openrouter-key:2 \
  --add-volume=name=sandbox-tmp,type=in-memory,size-limit=64Mi \
  --add-volume-mount=volume=sandbox-tmp,mount-path=/sandbox-tmp \
  --ingress=all --allow-unauthenticated --labels=app=qre-demo
gcloud run services describe qre-demo --region="$REGION" --format="yaml(spec.template.spec.volumes,spec.template.spec.containers[0].volumeMounts)"
gcloud run services add-iam-policy-binding qre-demo --region="$REGION" --member="serviceAccount:$DEPLOY_SA" --role=roles/run.developer
gcloud run services describe qre-demo --region="$REGION" --format='value(status.url)'
```

- The second command must show `mountPath: /sandbox-tmp` exactly, with no Windows path in front.
- `--allow-unauthenticated` makes it public, which is what a demo is.
- From then on, deploys go through the workflow with `deploy: true`.
- **The traffic trap.** A deploy with `--no-traffic` (a test revision, as in check 6 below) pins traffic to the revision serving at the time. Later deploys, the workflow's included, then create revisions that get **no traffic**. After any `--no-traffic` deploy, put traffic back on the newest revision: `gcloud run services update-traffic qre-demo --region="$REGION" --to-latest`.
- **Permissions caveat:** Google's deploy page says `run.developer` on the service, `serviceAccountUser` on the runtime account and `artifactregistry.reader` on the repository are enough. If the workflow's deploy step is refused anyway, the error names the missing permission. I'd rather add that one than a project-wide role.
- **Undo:** `gcloud run services delete qre-demo --region="$REGION"` removes everything that runs. The repository, secret and service accounts cost nothing at this size, but can be deleted the same way. To keep it running but private: `gcloud run services remove-iam-policy-binding qre-demo --region="$REGION" --member=allUsers --role=roles/run.invoker`.

## Cost at zero traffic (per month)

| item | expected | basis |
|---|---|---|
| Cloud Run | $0 | min instances 0 and request-based billing: an idle service bills nothing. Under traffic, the free tier (180,000 vCPU-s, 360,000 GiB-s, 2M requests) covers roughly 6,000 runs, far more than the $5 key pays for (about 190 at the eval mix) |
| Artifact Registry | $0 to about $0.02 | 0.5 GB free. The image is 332 MB compressed, so one fits and two (the cleanup keeps two) are about 0.66 GB. The overage rate of about $0.10 per GB-month is from memory, because Google's pricing page didn't render for me |
| Secret Manager | $0 | 1 active version (6 free), a few reads per cold start (10,000 free) |
| Logging | $0 | one line per run, far under the 50 GiB free |
| network | $0 | pulls in the same region; the page and results are kilobytes (1 GB free from North America) |
| IAM, WIF, GitHub Actions | $0 | public repository |
| **total** | **$0.00 to about $0.02** | plus the OpenRouter key, which is capped at $5 by OpenRouter, not by Google |

The $1 budget alert only emails, and it doesn't stop billing. `max-instances=1` is what bounds Cloud Run spend. The free-tier figures above are from Google's free-tier page; the pricing page didn't render, so I couldn't check the per-unit rates.

## Post-deploy checks, in order

Do these before sharing the URL. Checks 1 to 3 cost nothing. Checks 4 and 5 make real LLM calls, about $0.01 in all. Results of the first deploy are in the last section.

1. **It's up and configured as planned.**
   - `gcloud run services describe qre-demo --region=$REGION` shows gen2, 2Gi, max 1, timeout 360, the volume and the secret reference.
   - `curl $URL/health` returns the versions and table hashes. `curl -I $URL/` returns 200 with the Content-Security-Policy header.
   - In the logs (`gcloud run services logs read qre-demo --region=$REGION`):
     - expected: the broker line ("sandbox broker listening ... as uid 10002") and the server's "sandbox: build_circuit runs behind the broker";
     - not expected: the entrypoint warning that `/sandbox-tmp` couldn't be given to the sandbox user. If it appears, the volume came up root-owned and was made world-writable.
2. **The real `/key` reply** (docs/demo-api.md, check 2). `curl $URL/limits` shows `accepting_runs: true` and `reason: null`. A wrong field name or shape would show as `budget_unknown`.
3. **The sandbox limits on gen2** (check 3).
   - Cloud Run has no `exec`, so the image has a self-check: `qre-entrypoint selfcheck` (`src/qre_agent/selfcheck.py`). The entrypoint starts the broker exactly as for the server, then runs the checks as `app` instead of the server. They check that the code runs as the sandbox user with a clean environment, a 1.6 GB allocation gets `MemoryError`, a CPU hog is stopped at 10 s, a 20 MB file is refused, and the 64 MiB volume fills and stops the writes.
   - Run it once as a Cloud Run job with the same image, 2 GiB and volume (jobs always run on gen2). It takes about a minute of compute, cents at most:

     ```bash
     MSYS2_ARG_CONV_EXCL="--add-volume-mount" gcloud run jobs create qre-selfcheck --region="$REGION" --image="$REGION-docker.pkg.dev/$PROJECT_ID/qre-demo/qre-demo@$DIGEST" \
       --args=selfcheck --cpu=1 --memory=2Gi --task-timeout=300 --max-retries=0 --service-account="$RUN_SA" \
       --add-volume=name=sandbox-tmp,type=in-memory,size-limit=64Mi --add-volume-mount=volume=sandbox-tmp,mount-path=/sandbox-tmp
     gcloud run jobs execute qre-selfcheck --region="$REGION" --wait
     gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="qre-selfcheck"' --order=asc --limit=60 --format='value(textPayload)'
     gcloud run jobs delete qre-selfcheck --region="$REGION" --quiet
     ```

     Every check line should start with `ok` and the last should say `selfcheck passed`. Logs can lag the job by half a minute. Memory reads "not available": Cloud Run doesn't expose cgroup memory files inside the container. CI runs the same check in Docker on every image build.
4. **One real run** from the page, with the example prompt.
   - It should end in a result with a verify badge.
   - Compare the run's `cost_usd` in the log (`gcloud run services logs read qre-demo --region=$REGION | grep "run d-"`) with the increase in the key's usage on https://openrouter.ai/settings/keys. They should agree within about 20%, the threshold the spend guard warns at.
5. **The client address** (check 1).
   - Send one real run with `X-Forwarded-For: 1.2.3.4`.
   - Then call `GET /limits` three times: with no header, with `1.2.3.4`, and with `5.6.7.8`.
   - All three showing the same `runs_left` means the address is the real one: Cloud Run appends it last, and the server uses that entry.
   - If only the `1.2.3.4` call differs, the header is client-controlled. Then the per-address limits are spoofable until `QRE_PROXY_HOPS` is fixed, though the daily cap still bounds cost.
6. **Out of credit.** Skipped on purpose for the first deploy: the unit tests cover it (`tests/test_limits.py`, `tests/test_routing.py`, `tests/test_demo.py`, `tests/test_server.py`: a key under $0.40 stops new runs with `budget_exhausted`, a 402 during a run ends it without escalating, and every later request gets 503). To test it live:
   - make a second OpenRouter key with a $0 limit, and add it as a new secret version (The secret, steps 2 to 4);
   - deploy a revision with no traffic: `gcloud run deploy qre-demo --region=$REGION --image=<same digest> --set-secrets=OPENROUTER_API_KEY=qre-demo-openrouter-key:<new version> --no-traffic --tag=oocheck`;
   - on its tag URL (printed by the deploy, `https://oocheck---qre-demo-…`), `/limits` should say `budget_exhausted` and `POST /estimate` should get 503;
   - then undo all of it, or the next deploy uses a dead key and gets no traffic: `gcloud run services update-traffic qre-demo --region=$REGION --remove-tags=oocheck`, `gcloud run services update qre-demo --region=$REGION --set-secrets=OPENROUTER_API_KEY=qre-demo-openrouter-key:2`, `gcloud run services update-traffic qre-demo --region=$REGION --to-latest`, disable the new secret version, and delete the $0 key on OpenRouter.
7. **Cold start.** Leave it idle for 20 minutes, then time the first request: `curl -s -o /dev/null -w "%{http_code} in %{time_total} s\n" "$URL/"`. Inside the container the server first answers in about 1.1 s on the CI runner (docs/demo-image.md); the rest of a cold start is Cloud Run starting an instance and pulling the 332 MB image. Record the number.

## Cold starts and the first run

Measured on revision 00001 (the first deploy) and 00002 (`aeb6904`: the server answers before the estimators load, #13), each once. The run numbers are for the example prompt (a 16-qubit QFT) or a 4-qubit QFT, both answered by Gemini.

| | value |
|---|---|
| first GET / after idling, instance shut down | 21 s on revision 00001; **7.1 s** on 00002 |
| GET /, warm | 0.13 s |
| container start to first response, on the CI runner (median of 5) | 2.51 s before #13; 1.03 to 1.09 s after (docs/demo-image.md) |
| background load of the estimators on Cloud Run | 308 s: from 05:50:03 (startup) to 05:55:11, finished inside the first run's request |
| first run after that, end to end (Cloud Run request log) | **16.1 s**: 2.3 s waiting for the rest of the load, then a 13.7 s run |
| later runs, end to end | 13.8 s (13.7 s run); the first revision's two runs took 19.0 and 18.2 s |

Why the load took 308 s: with request-based billing Cloud Run gives an instance CPU only while it starts and while a request is in flight. Startup ends when the server listens, so the load then crept along on the CPU of a few `/health` requests until a run's request gave it full CPU, after which the rest took 2.3 s. So after idling, `/health` saying `"ready": false` is normal, and the first run includes whatever is left of the load. That run had waited after a deploy; one arriving soon after a true cold start would wait for more of the load, about 5 to 6 s on this instance going by revision 00001's start-up (not measured directly). Since `b8f37e7` (#14) that wait counts against the run's 240 s, so a first run fails only if loading takes the whole 240 s. Always-on CPU (`--no-cpu-throttling`) would let the load finish in the background, but it bills the instance for its whole life, idle minutes included, so it isn't used.

## First deploy: what happened

Deployed 2026-10-12 as revision `qre-demo-00001-d49`, image `sha256:aa85552a…` (commit `04cb453`), at https://qre-demo-766408496665.us-central1.run.app.

| Check | Result |
|---|---|
| 1. Config, page, logs | Passed: gen2, max 1, concurrency 20, 360 s, 1 vCPU, 2 GiB, `qre-demo-run`, secret version 2; page 200 with the policy; broker as uid 10002, server found it, uvicorn up about 7 s after start, no `/sandbox-tmp` warning. Found: `/healthz` blocked by Cloud Run and `HEAD /` 405; fixed by the `/health` route and HEAD support |
| 2. Real `/key` reply | Passed: `accepting_runs: true`, 4 runs left this hour, 10 today |
| 3. Sandbox on gen2 | Passed: kernel 6.9.12; all six checks ok |
| 4. One real run | Passed: Gemini answered, verified, $0.0047 in 5 model calls and 18.8 s (16-qubit QFT at p = 1e-4: 136,285 surface and 1,601 gross physical qubits, matching local estimates). OpenRouter's usage figure was not compared |
| 5. Client address | Passed: all three `/limits` calls showed the same runs left; forged headers are ignored, `QRE_PROXY_HOPS=1` is right |
| 6. Out of credit | Skipped on purpose (unit tests cover it) |
| 7. Cold start | 21 s for the first GET / after idling (warm: 0.13 s). After the server stopped loading the estimators before answering (revision 00002): **7.1 s**, measured after 24 minutes idle (a new instance started for it). Details below |

What went wrong on the way, and is now in the steps above: the secret's first version (a pasted block fed `read`), and the deploy's `mount-path` (Git Bash path rewriting).

## Not verified

- **Per-unit prices:** the Artifact Registry and Secret Manager overage rates come from memory, because the pricing pages didn't render. The free-tier amounts come from Google's free-tier page.
- **Deploy permissions:** whether `run.developer` bound on the service alone is enough for the workflow's `gcloud run deploy`. The first workflow deploy is the test.
- **Cost against OpenRouter's own figure** (check 4).
- **A true cold start's first run:** the wait for the load was measured after a deploy (2.3 s), not right after a shut-down instance woke up.
