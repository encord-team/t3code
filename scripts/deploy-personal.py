#!/usr/bin/env python3
"""Deploy one fork commit to an Apple Silicon Mac and a Linux x64 SSH host."""
import argparse
import ctypes
import json
import os
import plistlib
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone


class RollbackBlocked(RuntimeError):
    pass


def run(args, cwd=None, env=None, capture=False):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else sys.stderr, text=True)
    return result.stdout.strip() if capture else None


def validate_sha(value):
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('Expected a full Git commit SHA')
    return value


def extract_archive(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            if not (destination / member.name).resolve().is_relative_to(destination.resolve()) or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive entry: ' + member.name)
        source.extractall(destination, members=members)


def replace_runtime(base, stage, version, backup, activate, stop):
    """Caller stops the service first; failed activation restores data and runtime."""
    runtime = base / 'runtime/versions' / version
    data = base / 'userdata'
    state = base / 'runtime/service-state.json'
    backup.mkdir(parents=True, mode=0o700)
    shutil.copytree(data, backup / 'userdata')
    if state.exists():
        shutil.copy2(state, backup / 'service-state.json')
    had_runtime = runtime.exists()
    if had_runtime:
        runtime.rename(backup / 'runtime')
    try:
        shutil.copytree(stage, runtime)
        (runtime / '.install-complete').write_text(version + '\n')
        activate()
    except Exception:
        try:
            stop()
        except Exception as error:
            raise RollbackBlocked('Could not stop new service; recovery files retained at ' + str(backup)) from error
        try:
            if runtime.exists():
                runtime.rename(backup / 'failed-runtime')
            if had_runtime:
                (backup / 'runtime').rename(runtime)
            data.rename(backup / 'failed-userdata')
            # Rename the consistent snapshot back; no second full copy during recovery.
            (backup / 'userdata').rename(data)
            if (backup / 'service-state.json').exists():
                (backup / 'service-state.json').replace(state)
            elif state.exists():
                state.unlink()
        except Exception as error:
            raise RollbackBlocked('Recovery could not finish; service left stopped. Inspect ' + str(backup)) from error
        raise


def build(options):
    sha = validate_sha(options.sha)
    cache = Path(options.cache).expanduser()
    vp = shutil.which(options.vp) or str(Path(options.vp).expanduser())
    if options.vp == 'vp' and not Path(vp).is_file():
        vp = str(Path.home() / '.local/share/t3-encord-vp/bin/vp')
    if not Path(vp).is_file():
        raise RuntimeError('Vite+ required; pass --vp or --remote-vp with its full path')
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
    output = cache / sha / ('mac' if sys.platform == 'darwin' else 'linux')
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
    if sys.platform == 'darwin':
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
    else:
        run(command + [vp, 'run', '--filter', 't3', 'build'], cwd=source, env=env)
        run([vp, 'env', 'exec', '--node', '26.8.2', 'node', 'apps/server/scripts/cli.ts', 'build-exe', '--target', 'linux-x64'], cwd=source, env=env)
        run(['cargo', 'build', '--locked', '--release', '--manifest-path', 'native/resource-monitor/Cargo.toml'], cwd=source, env=env)
        monitor = source / 'apps/server/dist/resource-monitor/linux-x64'
        monitor.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / 'native/resource-monitor/target/release/t3-resource-monitor', monitor)
        run(command + ['node', 'scripts/build-cli-archive.ts', '--platform', 'linux', '--arch', 'x64', '--version', version, '--output-dir', output], cwd=source, env=env)
        artifact = output / ('t3-%s-linux-x64' % version)
        extract_archive(output / (artifact.name + '.tar.gz'), output)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix='t3-deploy-smoke-') as smoke, open(output / 'smoke.log', 'w') as log:
            process = subprocess.Popen([str(artifact / 't3'), 'serve', '--host', '127.0.0.1', '--port', str(port), '--base-dir', smoke, '--no-browser'], stdout=log, stderr=log, start_new_session=True)
            try:
                health(port, protocol, process)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=30)
    result = dict(sha=sha, version=version, protocol=protocol, artifact=str(artifact), update_repository=options.update_repository)
    pending = marker.with_name('deployment-' + timestamp() + '.json')
    pending.write_text(json.dumps(result) + '\n')
    pending.replace(marker)
    return result


def read_descriptor(port):
    with urllib.request.urlopen('http://127.0.0.1:%s/.well-known/t3/environment' % port, timeout=2) as response:
        return json.load(response)


def health(port, protocol, process=None, expected=None):
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError('Smoke-test server exited before becoming healthy')
        try:
            descriptor = read_descriptor(port)
            if descriptor.get('orchestrationProtocolVersion', 1) != protocol:
                raise ValueError('Server protocol does not match the built client')
            if expected and any(descriptor.get(key) != value for key, value in expected.items()):
                raise ValueError('Health endpoint belongs to a different environment or version')
            return descriptor
        except ValueError:
            raise
        except OSError as error:
            last = error
            time.sleep(0.25)
    raise RuntimeError('Server did not become healthy: ' + str(last))


def install_linux(options):
    base = Path(options.base_dir).expanduser()
    info = json.loads((Path(options.cache).expanduser() / validate_sha(options.sha) / 'linux/deployment.json').read_text())
    if info['sha'] != options.sha:
        raise ValueError('Build SHA mismatch')
    runtime = base / 'runtime/versions' / info['version']
    if run(['systemctl', '--user', 'is-active', 't3code.service'], capture=True) != 'active':
        raise RuntimeError('Repair t3code.service before deploying')
    backup = base / 'runtime/personal-backups' / (timestamp() + '-' + options.sha[:12])
    unit = Path.home() / '.config/systemd/user/t3code.service'
    old_unit = unit.read_bytes()
    environment = run(['systemctl', '--user', 'show', 't3code.service', '--property=Environment', '--value'], capture=True)
    configured = dict(value.split('=', 1) for value in shlex.split(environment) if '=' in value)
    if configured.get('T3CODE_HOME') != str(base):
        raise RuntimeError('Service uses a different T3 home; pass --remote-home to match it')
    if not (base / 'userdata').is_dir():
        raise RuntimeError('Expected existing T3 data before stopping the service')
    launcher = Path.home() / '.local/bin/t3'
    if not launcher.is_symlink():
        raise RuntimeError('Expected the installed t3 CLI symlink before deploying')
    old_launcher = os.readlink(launcher)
    # Confirm this port belongs to this service before interrupting it.
    previous = read_descriptor(options.port)
    service_pid = run(['systemctl', '--user', 'show', 't3code.service', '--property=MainPID', '--value'], capture=True)
    ports = run(['ss', '-H', '-ltnp', 'sport = :' + str(options.port)], capture=True)
    children = Path('/proc/' + service_pid + '/task/' + service_pid + '/children').read_text().split()
    if not any('pid=' + pid + ',' in ports for pid in [service_pid] + children):
        raise RuntimeError('Health-check port is not owned by t3code.service')
    run(['systemctl', '--user', 'stop', 't3code.service'])
    def stop():
        run(['systemctl', '--user', 'stop', 't3code.service'])
    def activate():
        (backup / 't3code.service').write_bytes(old_unit)
        (backup / 'cli-symlink.txt').write_text(old_launcher + '\n')
        (runtime / '.encord-source-commit').write_text(options.sha + '\n')
        run([runtime / 't3', 'service', 'install', '--base-dir', base])
        run(['systemctl', '--user', 'restart', 't3code.service'])
        health(options.port, info['protocol'], expected=dict(environmentId=previous['environmentId'], serverVersion=info['version']))
        run(['systemctl', '--user', 'is-active', 't3code.service'], capture=True)
        temporary = launcher.with_name('.t3-personal-' + timestamp())
        temporary.symlink_to(runtime / 't3')
        temporary.replace(launcher)
    try:
        replace_runtime(base, Path(info['artifact']), info['version'], backup, activate, stop)
    except RollbackBlocked:
        raise
    except Exception:
        unit.write_bytes(old_unit)
        temporary = launcher.with_name('.t3-personal-' + timestamp())
        temporary.symlink_to(old_launcher)
        temporary.replace(launcher)
        run(['systemctl', '--user', 'daemon-reload'])
        run(['systemctl', '--user', 'start', 't3code.service'])
        raise
    return dict(backup=str(backup), **info)


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


def remote(options, action):
    args = ['python3', '-', '--worker', action, '--sha', options.sha, '--repository', options.repository, '--update-repository', options.update_repository, '--vp', options.remote_vp, '--cache', options.remote_cache, '--base-dir', options.remote_home, '--port', str(options.port)]
    if options.rebuild:
        args.append('--rebuild')
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', options.remote, shlex.join(args)], input=Path(__file__).read_text(), text=True, stdout=subprocess.PIPE, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--remote', default='het-1')
    parser.add_argument('--repository', default='https://github.com/encord-team/t3code.git')
    parser.add_argument('--update-repository', default='encord-team/t3code')
    parser.add_argument('--vp', default='vp')
    parser.add_argument('--remote-vp', default='vp')
    parser.add_argument('--cache', default='~/.cache/t3code-personal-deploy')
    parser.add_argument('--remote-cache', default='~/.cache/t3code-personal-deploy')
    parser.add_argument('--remote-home', default='~/.t3')
    parser.add_argument('--app', default='/Applications/T3 Code (Alpha).app')
    parser.add_argument('--port', type=int, default=3773)
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--worker', choices=['build', 'install'], help=argparse.SUPPRESS)
    parser.add_argument('--sha', help=argparse.SUPPRESS)
    parser.add_argument('--base-dir', default='~/.t3', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.worker:
        if sys.platform != 'linux' or os.uname().machine != 'x86_64':
            raise RuntimeError('Worker requires Linux x64')
        print(json.dumps(build(options) if options.worker == 'build' else install_linux(options)))
        return
    if sys.platform != 'darwin' or os.uname().machine != 'arm64':
        raise RuntimeError('Coordinator requires an Apple Silicon Mac')
    repository_root = Path(__file__).resolve().parent.parent
    if options.ref.startswith('origin/') and not options.dry_run:
        run(['git', 'fetch', 'origin'], cwd=repository_root)
    options.sha = validate_sha(run(['git', 'rev-parse', '--verify', options.ref + '^{commit}'], cwd=repository_root, capture=True))
    print('Deploying %s to %s and %s' % (options.sha, options.app, options.remote), file=sys.stderr)
    if options.dry_run:
        print('Dry run: no builds, SSH connections, or installation changes. Mac takes effect on next restart.')
        return
    mac = build(options)
    linux = remote(options, 'build')
    if (mac['sha'], mac['protocol']) != (linux['sha'], linux['protocol']):
        raise RuntimeError('Artifacts do not match; nothing installed')
    if options.build_only:
        print(json.dumps(dict(mac=mac, linux=linux), indent=2))
        return
    destination = Path(options.app)
    if not destination.exists():
        raise RuntimeError('Expected an existing Mac installation; nothing installed')
    if not os.access(destination.parent, os.W_OK):
        raise RuntimeError('App directory is not writable; nothing installed')
    mac_backup = install_mac(mac, destination)
    try:
        linux_install = remote(options, 'install')
    except Exception:
        exchange(Path(mac_backup) / destination.name, destination)
        raise
    print(json.dumps(dict(sha=options.sha, mac_backup=mac_backup, linux_backup=linux_install['backup'], protocol=mac['protocol'], mac_restart='Quit and reopen to use the new build'), indent=2))


if __name__ == '__main__':
    main()
