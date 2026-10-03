# Deploying a personal fork

From an Apple Silicon Mac, deploy the same committed fork revision to the installed
desktop app and a Linux x64 host running `t3code.service`:

```sh
vp run deploy:personal -- --ref origin/main --remote het-1
```

You can also run `python3 scripts/deploy-personal.py --ref origin/main --remote het-1`
directly. When `vp` is absent from PATH, the script checks the isolated installation
at `~/.local/share/t3-encord-vp/bin/vp` on each machine.

The default repository is `encord-team/t3code`. Push your changes first: both build
checkouts fetch the exact commit from that repository. Uncommitted changes are not
deployed. `origin/main` is fetched before resolving its SHA; an explicit SHA can
deploy or rebuild an older commit. This script does not publish releases.

Both machines need Python 3.9+, Git, Vite+, and Rust 1.95.0 installed through
rustup. The remote needs noninteractive SSH access, an active systemd user service,
and enough free space for builds and data backups. Vite+ must have managed Node
environments enabled (`vp env on`); it provisions Node 24.13.1 for builds and
26.8.2 for the standalone Linux executable. Pass `--vp` and `--remote-vp` when
Vite+ is not on the corresponding PATH. The Mac app directory must be writable.

```sh
vp run deploy:personal -- --dry-run
vp run deploy:personal -- --build-only
```

Dry run resolves the local ref without fetching, building, contacting the remote,
or installing anything. Build only produces both artifacts without installation.
Successful builds are cached by commit; `--rebuild` forces a rebuild.

The coordinator builds an unsigned Mac ZIP and a standalone Linux archive in
separate checkouts below `~/.cache/t3code-personal-deploy`. It includes public
configuration from `.env.example`, never your local `.env`. Both artifacts must
match the commit and orchestration protocol before either is installed. Linux
gets an isolated startup test; the Mac archive is checked for its embedded commit,
version, architecture, and plist validity.

The Mac bundle is replaced atomically without quitting the running app. Quit and
reopen it after deployment. The Linux service stops briefly for a consistent
`userdata` backup, then starts the new runtime. Active remote agents and terminals
can be interrupted. Existing network settings and the T3 home remain in use.

Mac backups live beside the app under `.t3code-rollback`. Linux backups live under
`~/.t3/runtime/personal-backups` and include data, launcher state, the replaced
runtime if present, and the previous unit. Paths are printed on success. Failed
Linux activation restores its runtime, data, launcher state, and unit; the Mac
bundle is then restored too. Data written during failed activation is retained
in `failed-userdata`. An interrupted SSH connection can leave deployment outcome
unknown; inspect `t3 service status` and `.encord-source-commit` in the runtime
before retrying. Backups are retained until you remove them.

If recovery cannot stop the new service or restore its snapshot, it reports
`RollbackBlocked` and retains recovery files instead of starting an old server
against incompletely restored data. Inspect the reported backup path before
restarting manually.

Use `--app`, `--remote-home`, and `--port` for nondefault installations. `--port`
selects the health-check port; it does not change the server's listening settings.
For another fork, set both `--repository` and `--update-repository`.

The script bypasses normal update discovery: `t3 update` selects upstream builds
and can overwrite the fork. Mac builds remain unsigned and unnotarized, with the
same signing and native passkey limitations as manual local builds. See
[release setup](./release.md).

The optional manual Mac Actions workflow uploads downloadable DMG/ZIP artifacts.
It does not perform this two-machine deployment.
