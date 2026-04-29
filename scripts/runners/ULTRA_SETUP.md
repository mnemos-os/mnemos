# ULTRA GitLab Runner Setup

ULTRA provides the two non-Intel binary release paths for v4.0:

- `ultra-native`: native macOS arm64 runner for `mnemos-macos-aarch64`
- `ultra-podman-arm64`: arm64 Linux runner backed by Podman for
  `mnemos-linux-aarch64`

The GitLab release jobs are critical on tag pipelines. Register both runners
against the MNEMOS project or group before cutting the v4.0 GA tag.

## Prerequisites

Install on ULTRA:

```bash
brew install gitlab-runner podman python@3.11
python3.11 -m pip install --upgrade pip
podman machine init --cpus 6 --memory 8192 --disk-size 80
podman machine start
```

Confirm native tools:

```bash
uname -sm
python3.11 --version
podman run --rm --arch arm64 python:3.11-slim python -c 'import platform; print(platform.machine())'
```

## Cache Placement

Use persistent caches outside transient build directories:

- pip cache: `/Users/gitlab-runner/.cache/pip`
- Cargo cache: `/Users/gitlab-runner/.cargo`
- Podman volumes: `/Users/gitlab-runner/.local/share/containers`

Cargo is not part of the MNEMOS build script, but Python wheels can pull Rust
build backends when prebuilt wheels are unavailable. Keeping the Cargo cache
warm prevents tag builds from paying that cost repeatedly.

## Native macOS Runner

Use the GitLab registration token from the project or group runner settings.
Do not commit the token; paste it only during runner registration.

```bash
sudo gitlab-runner register \
  --url "https://gitlab.com" \
  --registration-token "<GITLAB_RUNNER_REGISTRATION_TOKEN>" \
  --description "ULTRA native macOS arm64" \
  --executor "shell" \
  --tag-list "ultra-native" \
  --run-untagged="false" \
  --locked="true"
```

Expected `config.toml` shape:

```toml
[[runners]]
  name = "ULTRA native macOS arm64"
  executor = "shell"
  tags = ["ultra-native"]
  [runners.cache]
    MaxUploadedArchiveSize = 0
```

The shell profile for the runner user should put Python 3.11 first on PATH:

```bash
export PATH="/opt/homebrew/bin:/opt/homebrew/opt/python@3.11/bin:$PATH"
export PIP_CACHE_DIR="$HOME/.cache/pip"
export CARGO_HOME="$HOME/.cargo"
```

## Linux arm64 Podman Runner

The `ultra-podman-arm64` runner should execute jobs inside an arm64 Linux
container, not on the macOS host. Reuse the existing Podman runner environment
used for Rust builds and adapt the image to Python 3.11.

Register the runner:

```bash
sudo gitlab-runner register \
  --url "https://gitlab.com" \
  --registration-token "<GITLAB_RUNNER_REGISTRATION_TOKEN>" \
  --description "ULTRA Podman Linux arm64" \
  --executor "custom" \
  --tag-list "ultra-podman-arm64" \
  --run-untagged="false" \
  --locked="true"
```

The custom executor must start an arm64 Linux container with the project
workspace mounted and Python 3.11 available. The job itself runs:

```bash
pip install --cache-dir=.cache/pip -e ".[build]"
bash scripts/build-binary.sh
```

Validate the executor before tag day:

```bash
podman run --rm --arch arm64 -v "$PWD:/workspace:Z" -w /workspace python:3.11-slim \
  bash -lc 'python -m pip install -e ".[build]" && bash scripts/build-binary.sh'
```

## Release Job Tags

The GitLab jobs expect these runner tags:

- `release:linux-aarch64` -> `ultra-podman-arm64`
- `release:macos-aarch64` -> `ultra-native`

ARGOS owns `release:linux-x86_64` through its `argos` runner tag.
