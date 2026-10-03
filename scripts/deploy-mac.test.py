import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import json
import sys

spec = importlib.util.spec_from_file_location('deploy_mac', Path(__file__).with_name('deploy-mac.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def test_failed_rebuild_leaves_previous_verified_artifact_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha = 'a' * 40
            source = root / sha / 'source'
            (source / 'apps/server').mkdir(parents=True)
            (source / 'apps/server/package.json').write_text('{"version":"0.0.45"}')
            (source / 'packages/contracts/src').mkdir(parents=True)
            (source / 'packages/contracts/src/environment.ts').write_text('ORCHESTRATION_PROTOCOL_VERSION = 2')
            output = root / sha / 'mac'
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
    def test_deployment_replaces_mac_bundle_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'Installed.app'
            (app / 'Contents').mkdir(parents=True)
            (app / 'Contents/version').write_text('old')
            artifact = root / 'New.app'
            (artifact / 'Contents').mkdir(parents=True)
            (artifact / 'Contents/version').write_text('new')
            info = dict(sha='a' * 40, protocol=2, artifact=str(artifact))
            with mock.patch.object(sys, 'argv', ['deploy', '--ref', 'HEAD', '--app', str(app)]), mock.patch.object(deploy, 'build', return_value=info):
                deploy.main()
            self.assertEqual((app / 'Contents/version').read_text(), 'new')
            backups = list((root / '.t3code-rollback').glob('*/Installed.app/Contents/version'))
            self.assertEqual(backups[0].read_text(), 'old')

    def test_dry_run_does_not_build_or_install(self):
        with mock.patch.object(sys, 'argv', ['deploy', '--ref', 'HEAD', '--dry-run']), mock.patch.object(deploy, 'build') as build, mock.patch.object(deploy, 'install_mac') as install:
            deploy.main()
        build.assert_not_called()
        install.assert_not_called()

    def test_build_only_does_not_install(self):
        with mock.patch.object(sys, 'argv', ['deploy', '--ref', 'HEAD', '--build-only']), mock.patch.object(deploy, 'build', return_value=dict(sha='a' * 40)), mock.patch.object(deploy, 'install_mac') as install:
            deploy.main()
        install.assert_not_called()

    def test_ref_must_resolve_to_a_commit(self):
        with self.assertRaises(ValueError):
            deploy.validate_sha('origin/main; rm -rf /')
        self.assertEqual(deploy.validate_sha('a' * 40), 'a' * 40)


if __name__ == '__main__':
    unittest.main()
