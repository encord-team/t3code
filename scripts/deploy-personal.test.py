import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import json
import sys
import os

spec = importlib.util.spec_from_file_location('deploy_personal', Path(__file__).with_name('deploy-personal.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def test_health_rejects_same_protocol_from_another_environment(self):
        with mock.patch.object(deploy, 'read_descriptor', return_value=dict(orchestrationProtocolVersion=2, environmentId='wrong', serverVersion='0.0.45')):
            with self.assertRaisesRegex(ValueError, 'different environment'):
                deploy.health(3773, 2, expected=dict(environmentId='expected', serverVersion='0.0.45'))

    def test_incomplete_recovery_never_restarts_old_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / '.t3'
            runtime = base / 'runtime/versions/0.0.45'
            runtime.mkdir(parents=True)
            (runtime / 't3').write_text('old')
            data = base / 'userdata'
            data.mkdir()
            (data / 'state.sqlite').write_text('data')
            stage = root / 'stage'
            stage.mkdir()
            (stage / 't3').write_text('new')
            unit = root / '.config/systemd/user/t3code.service'
            unit.parent.mkdir(parents=True)
            unit.write_text('old unit')
            launcher = root / '.local/bin/t3'
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(runtime / 't3')
            sha = 'a' * 40
            cache = root / 'cache'
            marker = cache / sha / 'linux/deployment.json'
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps(dict(sha=sha, version='0.0.45', protocol=2, artifact=str(stage))))
            options = SimpleNamespace(base_dir=str(base), cache=str(cache), sha=sha, port=3773)
            calls = []
            def run(args, **kwargs):
                calls.append([str(arg) for arg in args])
                if 'install' in args:
                    raise RuntimeError('activation failed')
                if '--property=Environment' in args:
                    return 'T3CODE_HOME=' + str(base)
                if '--property=MainPID' in args:
                    return str(os.getpid())
                if args[0] == 'ss':
                    return 'pid=%s,' % os.getpid()
                return 'active'
            original = Path.rename
            def rename(path, target):
                if path == data:
                    raise OSError('recovery rename failed')
                return original(path, target)
            with mock.patch.object(Path, 'home', return_value=root), mock.patch.object(deploy, 'run', side_effect=run), mock.patch.object(deploy, 'read_descriptor', return_value=dict(environmentId='expected')), mock.patch.object(Path, 'rename', rename):
                # /proc ownership is an external Linux-only probe in the installer.
                original_read = Path.read_text
                def read(path, *args, **kwargs):
                    if str(path).startswith('/proc/'):
                        return ''
                    return original_read(path, *args, **kwargs)
                with mock.patch.object(Path, 'read_text', read):
                    with self.assertRaises(deploy.RollbackBlocked):
                        deploy.install_linux(options)
            self.assertFalse(any('start' in args for args in calls))

    def test_failed_stop_retains_recovery_files_without_restoring_live_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'home'
            runtime = base / 'runtime/versions/0.0.45'
            runtime.mkdir(parents=True)
            (runtime / 't3').write_text('old')
            data = base / 'userdata'
            data.mkdir()
            (data / 'state.sqlite').write_text('old data')
            stage = root / 'stage'
            stage.mkdir()
            (stage / 't3').write_text('new')
            def activate():
                (data / 'state.sqlite').write_text('new data')
                raise RuntimeError('failed health')
            def stop():
                raise RuntimeError('stop failed')
            with self.assertRaises(deploy.RollbackBlocked):
                deploy.replace_runtime(base, stage, '0.0.45', root / 'backup', activate, stop)
            self.assertEqual((runtime / 't3').read_text(), 'new')
            self.assertEqual((data / 'state.sqlite').read_text(), 'new data')
            self.assertEqual((root / 'backup/userdata/state.sqlite').read_text(), 'old data')

    def test_failed_rebuild_leaves_previous_verified_artifact_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha = 'a' * 40
            source = root / sha / 'source'
            (source / 'apps/server').mkdir(parents=True)
            (source / 'apps/server/package.json').write_text('{"version":"0.0.45"}')
            (source / 'packages/contracts/src').mkdir(parents=True)
            (source / 'packages/contracts/src/environment.ts').write_text('ORCHESTRATION_PROTOCOL_VERSION = 2')
            output = root / sha / ('mac' if sys.platform == 'darwin' else 'linux')
            artifact = output / 'old-verified-artifact'
            artifact.mkdir(parents=True)
            (artifact / 'contents').write_text('verified')
            info = dict(sha=sha, protocol=2, artifact=str(artifact), update_repository='fork/repo')
            (output / 'deployment.json').write_text(json.dumps(info))
            options = SimpleNamespace(sha=sha, cache=str(root), vp='/usr/bin/true', rebuild=True, repository='unused', update_repository='fork/repo')
            def command(args, **kwargs):
                if 'i' in args:
                    raise RuntimeError('dependency installation failed')
                if '--version' in args:
                    return 'v24.13.1'
                return ''
            with mock.patch.object(deploy, 'run', side_effect=command):
                with self.assertRaisesRegex(RuntimeError, 'dependency installation failed'):
                    deploy.build(options)
                options.rebuild = False
                self.assertEqual(deploy.build(options), info)
            self.assertEqual((artifact / 'contents').read_text(), 'verified')

    @unittest.skipUnless(sys.platform == 'darwin', 'Uses the native atomic Mac swap')
    def test_remote_failure_restores_actual_mac_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'Installed.app'
            (app / 'Contents').mkdir(parents=True)
            (app / 'Contents/version').write_text('old')
            artifact = root / 'New.app'
            (artifact / 'Contents').mkdir(parents=True)
            (artifact / 'Contents/version').write_text('new')
            info = dict(sha='a' * 40, protocol=2, artifact=str(artifact))
            def remote(options, action):
                if action == 'install':
                    raise RuntimeError('remote failed')
                return info
            with mock.patch.object(sys, 'argv', ['deploy', '--ref', 'HEAD', '--app', str(app)]), mock.patch.object(deploy, 'build', return_value=info), mock.patch.object(deploy, 'remote', side_effect=remote):
                with self.assertRaisesRegex(RuntimeError, 'remote failed'):
                    deploy.main()
            self.assertEqual((app / 'Contents/version').read_text(), 'old')
            backups = list((root / '.t3code-rollback').glob('*/Installed.app/Contents/version'))
            self.assertEqual(backups[0].read_text(), 'new')

    def test_archive_rejects_escape_before_extracting(self):
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                entry = tarfile.TarInfo('../escaped')
                entry.size = 1
                output.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                deploy.extract_archive(archive, root / 'stage')
            self.assertFalse((root / 'escaped').exists())

    def test_failed_runtime_install_restores_runtime_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'home'
            runtime = base / 'runtime/versions/0.0.45'
            runtime.mkdir(parents=True)
            (runtime / 't3').write_text('old runtime')
            data = base / 'userdata'
            data.mkdir()
            (data / 'state.sqlite').write_text('old data')
            stage = root / 'stage'
            stage.mkdir()
            (stage / 't3').write_text('new runtime')
            state = base / 'runtime/service-state.json'
            state.write_text('old state')

            def fail():
                (data / 'state.sqlite').write_text('changed data')
                state.write_text('changed state')
                raise RuntimeError('health check failed')

            with self.assertRaisesRegex(RuntimeError, 'health check failed'):
                deploy.replace_runtime(base, stage, '0.0.45', root / 'backup', fail, lambda: None)
            self.assertEqual((runtime / 't3').read_text(), 'old runtime')
            self.assertEqual((data / 'state.sqlite').read_text(), 'old data')
            self.assertEqual(state.read_text(), 'old state')
            self.assertEqual((root / 'backup/failed-userdata/state.sqlite').read_text(), 'changed data')

    def test_successful_runtime_install_keeps_recovery_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'home'
            runtime = base / 'runtime/versions/0.0.45'
            runtime.mkdir(parents=True)
            (runtime / 't3').write_text('old')
            (base / 'userdata').mkdir()
            (base / 'userdata/state.sqlite').write_text('data')
            stage = root / 'stage'
            stage.mkdir()
            (stage / 't3').write_text('new')
            deploy.replace_runtime(base, stage, '0.0.45', root / 'backup', lambda: None, lambda: None)
            self.assertEqual((runtime / 't3').read_text(), 'new')
            self.assertEqual((root / 'backup/runtime/t3').read_text(), 'old')
            self.assertEqual((root / 'backup/userdata/state.sqlite').read_text(), 'data')

    def test_ref_must_resolve_to_a_commit(self):
        with self.assertRaises(ValueError):
            deploy.validate_sha('origin/main; rm -rf /')
        self.assertEqual(deploy.validate_sha('a' * 40), 'a' * 40)


if __name__ == '__main__':
    unittest.main()
