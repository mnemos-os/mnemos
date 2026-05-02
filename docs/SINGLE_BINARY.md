# MNEMOS Single-Binary Distribution

MNEMOS v5.0.0 publishes platform-native `mnemos` executables that bundle the
Python interpreter, runtime dependencies, and the sqlite-vec native extension.
Operators can copy one file onto an edge host, mark it executable, and start the
service without installing Python or running `pip`.

## Why Single-Binary

The single-binary build supports a zero-install edge appliance pattern:

- Drop `mnemos-linux-aarch64` on a Pi-class Linux host.
- Drop `mnemos-macos-aarch64` on an Apple Silicon laptop.
- Drop `mnemos-linux-x86_64` on an Intel Linux dev box or appliance.
- Use the same CLI shape as the Python package: `mnemos install`,
  `mnemos serve`, `mnemos health`, and the import/export helpers.

This is meant for operators who want the `edge` or `dev` profiles with no host
Python lifecycle to manage. Termux-style hosts need a matching Linux ABI; the
  v5.0 official matrix builds glibc Linux and macOS artifacts, not Android-native
Termux binaries.

## Download

Download the binary for your platform from the MNEMOS releases page:

https://github.com/mnemos-os/mnemos/releases

Published artifacts:

- `mnemos-linux-x86_64`
- `mnemos-linux-aarch64`
- `mnemos-macos-aarch64`

## Install

Linux x86_64:

```bash
chmod +x mnemos-linux-x86_64
sudo mv mnemos-linux-x86_64 /usr/local/bin/mnemos
mnemos version
```

Linux aarch64:

```bash
chmod +x mnemos-linux-aarch64
sudo mv mnemos-linux-aarch64 /usr/local/bin/mnemos
mnemos version
```

macOS Apple Silicon:

```bash
chmod +x mnemos-macos-aarch64
sudo mv mnemos-macos-aarch64 /usr/local/bin/mnemos
mnemos version
```

If macOS Gatekeeper marks the downloaded file as quarantined, remove the
download quarantine attribute before running it:

```bash
xattr -d com.apple.quarantine /usr/local/bin/mnemos
```

## First Run

For an all-in-one SQLite edge node:

```bash
mnemos install --profile edge
mnemos serve --profile edge
```

`mnemos install --profile edge` writes the local configuration and creates the
SQLite database. The binary includes the SQLite migration chain and sqlite-vec,
so first-run initialization does not need a source checkout.

For a development node:

```bash
mnemos install --profile dev
mnemos serve --profile dev
```

For a shared production service, use the `server` profile with PostgreSQL and
Redis. The single binary can run the server profile too, but extension-heavy
deployments are usually easier to operate from the package or container image.

## Limitations

- The artifact bundles a Python interpreter and runtime dependencies, so expect
  a binary around 80 MB depending on platform and dependency wheel contents.
- PyInstaller cannot cross-compile these artifacts. Each platform is built
  natively on matching hardware.
- A MNEMOS service running from the binary cannot `pip install` Python plugins
  into itself. Use the `server` profile from a normal Python environment or a
  container image when you need runtime extensibility.
- The official v5.0 matrix does not include macOS x86_64 or Windows.

## Build From Source

From a clean checkout on the target platform:

```bash
python -m pip install -e ".[build]"
bash scripts/build-binary.sh
```

The script writes `dist/mnemos-{platform}`, verifies `mnemos version`, and runs
`mnemos health` if `MNEMOS_BASE` points at a reachable MNEMOS instance.
