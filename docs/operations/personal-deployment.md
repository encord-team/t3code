# Deploying a personal fork

After merging a change, deploy the committed fork revision to the installed Apple
Silicon Mac app:

```sh
vp run deploy:mac
```

The default path updates only `/Applications/T3 Code (Alpha).app`. It does not
connect to the Linux host or restart a server. The former combined
`deploy:personal` command has been removed.

You can also run `python3 scripts/deploy-mac.py` directly. When `vp` is absent
from PATH, the script checks `~/.local/share/t3-encord-vp/bin/vp`.

The default repository is `encord-team/t3code`. Push your changes first: the build
checkout fetches the exact commit from that repository. Uncommitted changes are
not deployed. `origin/main` is fetched before resolving its SHA; use `--ref` with
an explicit SHA to deploy or rebuild an older commit. This script does not publish
releases.

The Mac needs Python 3.9+, Git, Vite+, and Rust 1.95.0 installed through rustup.
Vite+ must have managed Node environments enabled (`vp env on`); builds use Node
24.13.1. Pass `--vp` when Vite+ is not on PATH. The app directory must be writable.

```sh
vp run deploy:mac -- --dry-run
vp run deploy:mac -- --build-only
```

Dry run resolves the local ref without fetching, building, or installing anything.
Build only produces the Mac artifact without installation. Successful builds are
cached by commit; `--rebuild` forces a rebuild.

The script builds an unsigned Mac ZIP in a separate checkout below
`~/.cache/t3code-personal-deploy`. It includes public configuration from
`.env.example`, never your local `.env`. The archive is checked for its embedded
commit, version, architecture, and plist validity before installation.

The Mac bundle is replaced atomically without quitting the running app. Quit and
reopen it after deployment. Backups live beside the app under `.t3code-rollback`;
the backup path is printed on success. Backups are retained until you remove them.

Use `--app` for a different installation. For another fork, set both `--repository`
and `--update-repository`.

The script bypasses normal update discovery: `t3 update` selects upstream builds
and can overwrite the fork. Mac builds remain unsigned and unnotarized, with the
same signing and native passkey limitations as manual local builds. See
[release setup](./release.md).

The optional manual Mac Actions workflow uploads downloadable DMG/ZIP artifacts.
It does not install the app.
