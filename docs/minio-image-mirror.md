# MinIO image mirror

Why `docker-compose.yml` pulls MinIO from `docker.io/maxh8086` instead of
upstream, and what to do when the pin needs refreshing.

Status: **in use**. CI and local `docker compose` both depend on it.

## Why

MinIO publishes its container images to no registry we can read anonymously.

| Date | What happened |
|---|---|
| 2025 | MinIO stopped publishing to Docker Hub. The compose file moved to quay.io. |
| 2026-09-24 | quay.io withdrew anonymous pulls of `minio/*`. |

The second change is a namespace-wide access policy, not an expired digest:

```
docker pull quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z  -> 401 Unauthorized
docker pull quay.io/minio/minio:latest                        -> 401 Unauthorized
GET https://quay.io/v2/minio/minio/tags/list                  -> 401
```

quay still issues an anonymous token for the repository; the token simply
carries no pull action (`access: [{..., "actions": []}]`). So re-pinning a
digest cannot fix it, and neither can moving to a different tag.

**A locally cached copy is not a fix either.** A development machine that pulled
before the lockout still runs fine, because `docker compose` only pulls an image
it does not already have. CI runners start with an empty image store every run,
so they must reach a registry. That asymmetry is what made this bug look
confusing: it only ever failed in CI.

Blast radius while it was broken: CI died at the compose pull on every branch
including `main`, so roughly 155 service-backed tests (`tests/test_pit.py`,
`test_blob_s3.py`, `test_raw_source_file.py`, `test_financial_facts_schema.py`
and the `*_adapters.py` files) silently did not run between 2026-09-19 and the
commit that added this document.

## What is mirrored

Release `RELEASE.2025-09-07T16-13-09Z`, the same release the compose file
already pinned, byte-identical to the quay images. The source bytes came from a
development machine's Docker cache, retagged and pushed: after the lockout there
is no other way to obtain them without a MinIO account.

| Service | Image |
|---|---|
| `blob` | `docker.io/maxh8086/minio@sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2` |
| `blob-init` | `docker.io/maxh8086/mc@sha256:eb4ea9884b77704230e2423e9004d2fa738dc272876b9cc41a297d29443b8780` |

Both repositories are **public**, so CI pulls them anonymously and needs no
registry credentials. MinIO's images are AGPL-3.0, which permits
redistribution.

Two things to know about those digests.

- **They are not quay's digests.** Quay served a multi-arch index; the mirror
  push flattened it to the one platform the pushing machine had, which has its
  own digest. Copying the upstream digest across would 404. The upstream ones
  were `14cea493...` (minio) and `a7fe349e...` (mc), and they still appear in
  `docker inspect` output on any machine that pulled from quay before the
  lockout, which is a good way to confuse yourself.
- **They are `linux/amd64` only.** CI (`ubuntu-latest`) and the current
  development machine are both amd64. An arm64 host needs the mirror re-pushed
  for its platform; see below.

The pin stays a **digest**, never a tag. The write-once bucket design in
`core/blob.py` assumes the object-lock and retention behaviour of a fixed image.

## Known risk: Docker Hub pull limits

Docker Hub rate-limits anonymous pulls, and GitHub Actions runners share
outbound IPs, so the budget is consumed by every other project on the same
runner. This is the one way the mirror can fail that the old quay setup could
not, and it shows up as an intermittent `toomanyrequests` at the compose pull
rather than a clean 401.

If that starts happening, the fix is to authenticate the pull, which raises the
limit substantially:

1. Create a Docker Hub personal access token with `Public Repo Read-only`.
2. Store it in the repository as the secrets `DOCKERHUB_USERNAME` and
   `DOCKERHUB_TOKEN`.
3. Add a step to `.github/workflows/ci.yml` before `Build image`:

   ```yaml
   - name: Log in to Docker Hub (raises the pull rate limit)
     run: >
       echo "${{ secrets.DOCKERHUB_TOKEN }}"
       | docker login -u "${{ secrets.DOCKERHUB_USERNAME }}" --password-stdin
   ```

Not done pre-emptively: it adds a secret and a failure mode for a problem that
may never appear on a repository this small.

## Refreshing the mirror

Needed when upgrading the MinIO release, or when adding a platform such as
arm64. Requires a machine that already holds the upstream image, or MinIO
credentials for quay.

```bash
REL=RELEASE.2025-09-07T16-13-09Z

# 1. Obtain the upstream image (needs quay credentials, or a cached copy).
docker pull quay.io/minio/minio:$REL
docker pull quay.io/minio/mc:$REL

# 2. Retag into our namespace and push.
for r in minio mc; do
  docker tag "quay.io/minio/$r:$REL" "docker.io/maxh8086/$r:$REL"
  docker push "docker.io/maxh8086/$r:$REL"
done
```

`docker push` prints the digest it stored. Read it back from the registry
anonymously to confirm what a fresh CI runner will actually resolve -- not
`docker inspect`, which reports the upstream digest and will mislead you:

```bash
for r in minio mc; do
  T=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:maxh8086/$r:pull" \
      | python -c "import sys,json;print(json.load(sys.stdin)['token'])")
  echo -n "$r: "
  curl -s -o /dev/null -D- -H "Authorization: Bearer $T" \
    -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
    "https://registry-1.docker.io/v2/maxh8086/$r/manifests/$REL" \
    | grep -i docker-content-digest
done
```

Put those digests in `docker-compose.yml`, then verify end to end:

```bash
docker compose --profile test build test
docker compose --profile test run --rm test
docker compose --profile test down -v
```

`docker compose --profile test pull` reports `pull access denied` for
`equity-knowledge:dev`. That is expected and unrelated: the app image is built
locally, never pulled. CI builds it before running anything.

## If the mirror is ever the thing that breaks

Fallback order, same spirit as CLAUDE.md *Breakage*: re-push from a cached copy,
then a newer MinIO release re-mirrored, then a different S3-compatible server.

The last one is a real change, not a swap. The coupling to MinIO is small --
`core/blob.py` only calls `put_object` and `head_object`, both plain S3 -- but
the buckets rely on object lock with a default `GOVERNANCE` retention, created
by `blob-init` and asserted in `tests/test_blob_s3.py`. Many S3-compatible
servers do not implement object lock at all, and adopting one of those would
quietly drop the write-once guarantee that the whole raw-source-file design
rests on. Check that first, before anything else about a candidate.

Worth knowing if `mc` ever becomes inconvenient to mirror: it is used only to
provision buckets, and `mc mb --with-lock` plus `mc retention set --default
GOVERNANCE` are both plain S3 calls (`create_bucket(ObjectLockEnabledForBucket=
True)` and `put_object_lock_configuration`). `blob-init` could run boto3 in our
own app image instead, removing the second mirrored image entirely.
