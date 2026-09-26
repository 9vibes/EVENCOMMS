# Container and Release Checks

`container_smoke.py` requires Python 3.11+ and a running Docker daemon. It uses
only the Python standard library on the host; WebSocket checks run with the
image's installed dependencies. It never pulls or rebuilds the supplied image.

```sh
python scripts/container_smoke.py --image evencomms:0.1.0
python scripts/container_smoke.py --image evencomms:0.1.0 --speech-pcm /tmp/speech.pcm
```

Without `--speech-pcm`, inference is disabled for quick local checks. With it,
the fixture must be raw, mono, 16 kHz, signed 16-bit little-endian PCM, at most
15 seconds, saying "north entrance". The release workflow generates this with
espeak and ffmpeg. Transcription must recognize at least one of those words,
ignoring punctuation and case; it is never mocked.

The script creates an empty, dedicated Docker volume, prepares it with
`python -m backend.init_data` as root from the same image, and boots the default
UID/GID 10001 application with a read-only root filesystem and dropped
capabilities. It checks static pages, bearer authentication, pairing, messages,
authenticated WebSocket readiness/ping with same-host and explicitly allowed
rewritten-Host origins, and persistence after removing and recreating the
container. This simulates proxy headers; it is not an end-to-end proxy test.
It cleans up its own containers and volume even on failure, and does not print
credentials or container logs.

The inference mode checks the default `base.en` model with a cold download,
then checks that model files survive unchanged and a second transcription
succeeds in the replacement container. It requires internet access for the
download and does **not** claim offline operation. Each HTTP request has a
150-second timeout; the inference service has a 140-second timeout in this
test. Model hosting/network failures fail the release rather than bypass STT.

## Publication

`release.yml` runs on `v*` tags and manual dispatch. It first calls the full CI
workflow, including its existing Compose boot test. The optional manual
version defaults to the source tag or project
version. Versions must match `pyproject.toml`, `frontend/package.json`, and
`frontend/app.json`; source tags must be exactly `v<version>`. Stable SemVer
and SemVer prereleases are supported, but build metadata is rejected because
Docker tags cannot contain `+`. No source tags are created or changed.

The release builds **linux/amd64 only** on Ubuntu 24.04, labels it with source,
revision and version, and smoke-tests its local image ID with real CPU STT.
Only that exact image is tagged and pushed to
`ghcr.io/9vibes/evencomms:<version>` using `GITHUB_TOKEN` with `packages: write`.
There are no `latest`, moving minor-version, or ARM tags. The first version is
`0.1.0`.

Publication is serialized. An authenticated registry check rejects existing
tags and fails closed on authentication, network, and unexpected registry
errors. It permits only a confirmed missing manifest/repository. This prevents
replacement by this workflow, not by external writers: GHCR's tag API does not
provide an atomic create-only push. Restrict other package writers accordingly.
To retry a successfully published version, do not delete/reassign its tag;
publish a new version instead. A manual run publishes its selected source ref,
so select the intended release tag (or reviewed commit) deliberately.

The published repository digest is written to `image-reference.txt`, uploaded
as the `image-reference` artifact, and recorded in the job summary. Use this
digest for the Umbrel store integration. A first GHCR package can be private:
a maintainer must set its visibility to **Public** if needed and separately
verify an anonymous pull by digest. The workflow does not wait for visibility
changes. The token also needs permission to publish into the `9vibes` namespace;
organization/package policy can deny that even with `packages: write`.
