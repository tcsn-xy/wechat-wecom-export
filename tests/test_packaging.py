"""Standalone package portability, privacy boundary and repeat installation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import fixtures
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
