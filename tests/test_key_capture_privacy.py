"""Verify key discovery output without attaching a debugger or using real data."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import fixtures
import wecom_capture_key as capture


class CapturePrivacy(unittest.TestCase):
    def test_verified_key_is_returned_without_printing_it(self):
        key=bytes(range(16))
        with tempfile.TemporaryDirectory(prefix='capture path ') as tmp:
            def dump(*args,**kwargs):
                (Path(tmp)/'memdump/fake.bin').write_bytes(b'synthetic-memory')
                return 'DUMP_DONE regions=1'
            stdout=io.StringIO()
            with patch.object(capture,'RUNTIME',tmp),patch.object(capture,'run_lldb',side_effect=dump),patch.object(capture,'build_scanner',return_value='fake-scanner'),patch.object(capture.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='HIT\t'+key.hex())),patch.object(capture.crypto,'verify_key',return_value=True),contextlib.redirect_stdout(stdout):
                found=capture.capture(1,b'synthetic-page','synthetic-source.db')
            self.assertEqual(found,key)
            self.assertNotIn(key.hex(),stdout.getvalue())
            self.assertFalse((Path(tmp)/'memdump').exists())
            self.assertFalse((Path(tmp)/'page1.bin').exists())
