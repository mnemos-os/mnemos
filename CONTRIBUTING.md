# Contributing

Thanks for your interest in MNEMOS.

## License

MNEMOS is licensed under the Apache License, Version 2.0. Contributions to
this repository are accepted under the same license and under the Developer
Certificate of Origin (DCO) — see below.

## Developer Certificate of Origin (DCO)

We use the Developer Certificate of Origin 1.1 to track contribution
provenance. By signing off on a commit, you certify that you wrote the code
or otherwise have the right to contribute it under the project's open-source
license. The full DCO text is at <https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

### Signing commits

Every commit must include a `Signed-off-by` trailer attesting to the DCO.
The easiest way is `git commit -s`, which auto-inserts the trailer using
your configured `user.name` and `user.email`:

```
git commit -s -m "your commit message"
```

The trailer looks like:

```
Signed-off-by: Your Name <you@example.com>
```

PRs without DCO sign-off on every commit will be asked to amend
(`git commit --amend -s`) or rebase with sign-off
(`git rebase --signoff origin/master`).

## Development workflow

- Use a feature branch for non-trivial changes.
- Keep commits focused and reviewable; split large changes.
- Install from source through the v4 package entry points:

```bash
python -m pip install -e ".[dev,sqlite]"
mnemos install --profile dev
mnemos serve --profile dev
```

- Run the default test suite before opening a PR:

```bash
pytest -q
```

- Run lint before handoff:

```bash
ruff check . --extend-exclude .venv-ci
```

- For changes touching tenancy, DAG history, triggers, import/export, or
  auth, include focused regression tests and document the expected
  operator behavior for 404 vs 409 vs 403 outcomes.

### Building single-binary artifacts

The v4.0 binary release is built with PyInstaller. Use the `build` extra from a
clean checkout on the target platform:

```bash
python -m pip install -e ".[build]"
bash scripts/build-binary.sh
```

PyInstaller does not cross-compile these artifacts. Build linux-x86_64,
linux-aarch64, and macos-aarch64 on matching hosts.

### Multi-worker development

The `dev` and `edge` profiles are intentionally single-worker SQLite profiles.
For multi-worker work, use the `server` profile with Redis-backed shared state:

```bash
export MNEMOS_PROFILE=server
export RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1
export MNEMOS_WORKERS=2
mnemos serve --profile server
```

If `MNEMOS_WORKERS > 1` with `RATE_LIMIT_STORAGE_URI=memory://`, startup logs a
warning because circuit-breaker, rate-limit, and concurrency state are only
process-local.

## Guidelines

- Prefer small, reviewable commits.
- Do not commit secrets, `.env` files, logs, backups, or local infrastructure notes.
- Keep public docs generic and portable.
- Add or update tests when behavior changes.

## Reporting issues

Please include:

- what you expected
- what happened
- reproduction steps
- relevant logs or tracebacks
