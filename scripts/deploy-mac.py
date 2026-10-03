#!/usr/bin/env python3
"""Deploy one fork commit to the installed Apple Silicon Mac app."""
import argparse
import ctypes
import json
import os
import plistlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def run(args, cwd=None, env=None, capture=False):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else sys.stderr, text=True)
    return result.stdout.strip() if capture else None


def validate_sha(value):
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('Expected a full Git commit SHA')
    return value


def build(options):
    sha = validate_sha(options.sha)
    cache = Path(options.cache).expanduser()
    vp = shutil.which(options.vp) or str(Path(options.vp).expanduser())
    if options.vp == 'vp' and not Path(vp).is_file():
        vp = str(Path.home() / '.local/share/t3-encord-vp/bin/vp')
    if not Path(vp).is_file():
        raise RuntimeError('Vite+ required; pass --vp with its full path')
    env = os.environ.copy()
    root = Path(vp).resolve().parent.parent
    for candidate in [root, root.parent]:
        if (candidate / 'env').exists():
            env['VP_HOME'] = str(candidate)
            break
    env['PATH'] = os.pathsep.join([str(Path(vp).parent), str(Path.home() / '.cargo/bin'), env.get('PATH', '')])
    env['RUSTUP_TOOLCHAIN'] = '1.95.0'
    source = cache / sha / 'source'
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        run(['git', 'clone', '--no-checkout', '--filter=blob:none', options.repository, source])
    run(['git', 'fetch', '--no-tags', 'origin', sha], cwd=source)
    run(['git', 'checkout', '--detach', sha], cwd=source)
    if run(['git', 'status', '--porcelain'], cwd=source, capture=True):
        raise RuntimeError('Build checkout is dirty: ' + str(source))
    version = json.loads((source / 'apps/server/package.json').read_text())['version']
    protocol = int(re.search(r'ORCHESTRATION_PROTOCOL_VERSION = (\d+)', (source / 'packages/contracts/src/environment.ts').read_text()).group(1))
    output = cache / sha / 'mac'
    marker = output / 'deployment.json'
    if marker.exists() and not options.rebuild:
        result = json.loads(marker.read_text())
        if (result['sha'], result['protocol'], result.get('update_repository')) == (sha, protocol, options.update_repository) and Path(result['artifact']).exists():
            return result
    # Rebuild into fresh output; a failed rebuild cannot corrupt a verified cache hit.
    output = output / ('build-' + timestamp())
    output.mkdir(parents=True, exist_ok=True)
    command = [vp, 'env', 'exec', '--node', '24.13.1']
    if run(command + ['node', '--version'], env=env, capture=True) != 'v24.13.1':
        raise RuntimeError('Enable managed Node environments with vp env on before deploying')
    run(command + [vp, 'i', '--frozen-lockfile', '--filter=t3...', '--filter=@t3tools/web...', '--filter=@t3tools/desktop...', '--filter=@t3tools/scripts...', '--filter=@t3tools/monorepo'], cwd=source, env=env)
    # Public defaults only; never copy the contributor's .env into artifacts.
    shutil.copy2(source / '.env.example', source / '.env')
    env['T3CODE_DESKTOP_UPDATE_REPOSITORY'] = options.update_repository
    env['T3CODE_DESKTOP_SIGNED'] = 'false'
    run(command + [vp, 'run', 'dist:desktop:artifact', '--platform', 'mac', '--target', 'zip', '--arch', 'arm64', '--output-dir', output], cwd=source, env=env)
    archives = list(output.glob('*.zip'))
    if len(archives) != 1:
        raise RuntimeError('Expected exactly one Mac ZIP')
    unpacked = output / 'unpacked'
    run(['ditto', '-x', '-k', archives[0], unpacked])
    apps = list(unpacked.glob('*.app'))
    if len(apps) != 1:
        raise RuntimeError('Expected exactly one app in the ZIP')
    run(['plutil', '-lint', apps[0] / 'Contents/Info.plist'])
    with open(apps[0] / 'Contents/Info.plist', 'rb') as plist:
        identity = plistlib.load(plist)
    if identity['CFBundleShortVersionString'] != version:
        raise ValueError('Mac app version does not match the build')
    if run(['lipo', '-archs', apps[0] / 'Contents/MacOS' / identity['CFBundleExecutable']], capture=True) != 'arm64':
        raise ValueError('Mac app is not arm64')
    embedded = run(command + ['node', '--input-type=module', '-e',
                  'import {extractFile} from "@electron/asar"; console.log(JSON.parse(extractFile(process.argv[1], "package.json").toString()).t3codeCommitHash)',
                  apps[0] / 'Contents/Resources/app.asar'], cwd=source / 'scripts', env=env, capture=True)
    if embedded != sha[:12]:
        raise ValueError('Mac app does not embed the requested commit')
    artifact = apps[0]
    result = dict(sha=sha, version=version, protocol=protocol, artifact=str(artifact), update_repository=options.update_repository)
    pending = marker.with_name('deployment-' + timestamp() + '.json')
    pending.write_text(json.dumps(result) + '\n')
    pending.replace(marker)
    return result


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')


def exchange(first, second):
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    if library.renamex_np(os.fsencode(first), os.fsencode(second), 2) != 0:
        raise OSError(ctypes.get_errno(), 'Could not atomically exchange app bundles')


def install_mac(info, destination):
    destination = Path(destination)
    backup = destination.parent / '.t3code-rollback' / (timestamp() + '-' + info['sha'][:12])
    backup.mkdir(parents=True, mode=0o700)
    # Swap directly with the backup path, with no fallible move after the swap.
    stage = backup / destination.name
    run(['ditto', info['artifact'], stage])
    (backup / 'deployment.json').write_text(json.dumps(info) + '\n')
    if destination.exists():
        exchange(stage, destination)
    else:
        stage.rename(destination)
    return str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--repository', default='https://github.com/encord-team/t3code.git')
    parser.add_argument('--update-repository', default='encord-team/t3code')
    parser.add_argument('--vp', default='vp')
    parser.add_argument('--cache', default='~/.cache/t3code-personal-deploy')
    parser.add_argument('--app', default='/Applications/T3 Code (Alpha).app')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'darwin' or os.uname().machine != 'arm64':
        raise RuntimeError('Deployment requires an Apple Silicon Mac')
    repository_root = Path(__file__).resolve().parent.parent
    if options.ref.startswith('origin/') and not options.dry_run:
        run(['git', 'fetch', 'origin'], cwd=repository_root)
    options.sha = validate_sha(run(['git', 'rev-parse', '--verify', options.ref + '^{commit}'], cwd=repository_root, capture=True))
    print('Deploying %s to %s' % (options.sha, options.app), file=sys.stderr)
    if options.dry_run:
        print('Dry run: no builds or installation changes. Mac takes effect on next restart.')
        return
    mac = build(options)
    if options.build_only:
        print(json.dumps(dict(mac=mac), indent=2))
        return
    destination = Path(options.app)
    if not destination.exists():
        raise RuntimeError('Expected an existing Mac installation; nothing installed')
    if not os.access(destination.parent, os.W_OK):
        raise RuntimeError('App directory is not writable; nothing installed')
    mac_backup = install_mac(mac, destination)
    print(json.dumps(dict(sha=options.sha, mac_backup=mac_backup, protocol=mac['protocol'], mac_restart='Quit and reopen to use the new build'), indent=2))


if __name__ == '__main__':
    main()
