"""Configuration and external upload defaults using synthetic secrets only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import fixtures  # establishes library and script search paths
from local_config import mysql_options, launch_environment, read_private_json, media_root
from mysql_store import conn, captions
import deploy


class LocalConfiguration(unittest.TestCase):
    def config(self, folder, **changes):
        path = Path(folder) / 'private settings.json'
        value = dict(mysql=dict(user='synthetic-user', password='synthetic-secret', host='127.0.0.1', port=3307),
                     media_root=str(Path(folder) / 'media folder'))
        value.update(changes)
        path.write_text(json.dumps(value));path.chmod(0o600)
        return path

    def test_missing_password_fails_before_connect(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), patch('local_config.ROOT', Path(tmp)):
            with patch('mysql_store.conn.pymysql.connect') as connect:
                with self.assertRaises(ValueError):conn.connect()
                connect.assert_not_called()

    def test_cli_and_clean_background_load_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            with patch.dict(os.environ, {'WXEXPORT_CONFIG':str(path)}, clear=True):
                expected = mysql_options();environment = launch_environment()
                self.assertEqual(media_root(), (Path(tmp) / 'media folder').resolve())
                self.assertNotIn('synthetic-secret', json.dumps(environment))
                self.assertNotIn('WX_MYSQL_PASSWORD', environment)
                program = 'import json; from local_config import mysql_options; o=mysql_options(); print(json.dumps({k:v for k,v in o.items() if k!="password"})); assert o["password"]=="synthetic-secret"'
                child = subprocess.run([sys.executable,'-c',program], env={**environment,'PYTHONPATH':str(Path(conn.__file__).resolve().parents[1])}, capture_output=True, text=True)
                self.assertEqual(child.returncode,0,child.stderr)
                self.assertEqual(json.loads(child.stdout), {k:v for k,v in expected.items() if k!='password'})

    def test_environment_precedence_and_driver_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.config(tmp)
            with patch.dict(os.environ, {'WXEXPORT_CONFIG':str(path),'WX_MYSQL_PASSWORD':'synthetic-override','WX_MYSQL_PORT':'3310'}, clear=True):
                with patch('mysql_store.conn.pymysql.connect') as connect:
                    conn.connect('repairtest_config')
                    self.assertEqual(connect.call_args.kwargs['password'],'synthetic-override')
                    self.assertEqual(connect.call_args.kwargs['port'],3310)
                    self.assertFalse(connect.call_args.kwargs['local_infile'])

    def test_unsafe_permissions_and_malformed_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.config(tmp);path.chmod(0o644)
            with self.assertRaises(ValueError):read_private_json(path)
            path.chmod(0o600);path.write_text('{')
            with self.assertRaises(ValueError):read_private_json(path)

    def test_reconfiguration_preserves_existing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.config(tmp);before=path.read_bytes()
            with self.assertRaises(FileExistsError), contextlib.redirect_stdout(io.StringIO()):deploy.private_write(path,{'mysql':{}})
            self.assertEqual(path.read_bytes(),before)

    def test_caption_defaults_and_authorization(self):
        with tempfile.TemporaryDirectory() as tmp, patch('mysql_store.files.ROOT',Path(tmp)):
            (Path(tmp)/'keys').mkdir();path=Path(tmp)/'keys/minimax.json'
            config=dict(host='https://api.minimaxi.com',api_key='synthetic-key',enabled=False,image_external_consent=False)
            path.write_text(json.dumps(config));path.chmod(0o600)
            with patch('mysql_store.captions.urllib.request.urlopen') as opener:
                with self.assertRaises(captions.APIError):captions.load_config()
                opener.assert_not_called()
            config.update(enabled=True,image_external_consent=True);path.write_text(json.dumps(config))
            self.assertEqual(captions.load_config()['api_key'],'synthetic-key')
            path.chmod(0o644)
            with self.assertRaises(captions.APIError):captions.load_config()
