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
