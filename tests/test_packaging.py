"""Standalone package portability, privacy boundary and repeat installation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import shutil
from unittest.mock import patch
import tempfile
import unittest
import zipfile
import fixtures
import build_skill
from build_skill import build


class StandalonePackage(unittest.TestCase):
    def test_complete_repeatable_install_and_credential_preservation(self):
        with tempfile.TemporaryDirectory(prefix='skill path with spaces ') as tmp:
            base=Path(tmp);result=build(base/'packages');archive=Path(result['archive'])
            extracted=base/'skill files';extracted.mkdir()
            with zipfile.ZipFile(archive) as zipped:
                self.assertFalse(any('__pycache__' in item.filename or item.filename.endswith('.pyc') for item in zipped.infolist()))
                zipped.extractall(extracted)
            skill=extracted/'wechat-wecom-export'
            target=base/'deployment with spaces'
            command=[sys.executable,str(skill/'scripts/install.py'),'--target',str(target)]
            first=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            self.assertTrue((target/'lib/wecom/cdp.py').is_file())
            self.assertTrue((target/'native/wecom_keyscan.c').is_file())
            self.assertTrue((target/'docs/media-caption.md').is_file())
            private=target/'keys/local.json';private.parent.mkdir();private.write_text(json.dumps({'synthetic':'private'}))
            second=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertEqual(json.loads(private.read_text()),{'synthetic':'private'})
            self.assertFalse((target/'keys/minimax.json').exists())
            self.assertFalse((target/'.runtime').exists())
            help_result=subprocess.run([sys.executable,str(target/'scripts/deploy.py'),'--help'],capture_output=True,text=True)
            self.assertEqual(help_result.returncode,0,help_result.stderr)
            original=(target/'lib/wecom/cdp.py').read_bytes()
            (target/'lib/wecom/cdp.py').write_bytes(original+b'\n# local change\n')
            third=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(third.returncode,0)
            self.assertEqual((target/'lib/wecom/cdp.py').read_bytes(),original+b'\n# local change\n')

    def test_archive_hash_does_not_depend_on_local_umask(self):
        with tempfile.TemporaryDirectory(prefix='normalized package ') as tmp:
            root=Path(tmp)/'source';root.mkdir()
            manifest=json.loads((build_skill.ROOT/'release-manifest.json').read_text())
            for name in manifest:
                destination=root/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(build_skill.ROOT/name,destination)
                destination.chmod(0o700 if destination.stat().st_mode & 0o100 else 0o600)
            with patch.object(build_skill,'ROOT',root):
                private_modes=build_skill.build(Path(tmp)/'private-modes')
                for name in manifest:
                    destination=root/name
                    destination.chmod(0o755 if destination.stat().st_mode & 0o100 else 0o644)
                git_modes=build_skill.build(Path(tmp)/'git-modes')
            self.assertEqual(private_modes['sha256'],git_modes['sha256'])
