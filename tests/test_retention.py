import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from qianji.retention import prune_publications


class RetentionTests(unittest.TestCase):
    def test_current_analytics_keeps_its_label_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def write(folder, doc):
                raw = json.dumps(doc).encode()
                ref = hashlib.sha256(raw).hexdigest()
                path = root/folder/'exports/versions'/(ref+'.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                return ref, path
            current, current_path = write('analysis', {'kind':'current'})
            pinned, pinned_path = write('analysis', {'kind':'pinned'})
            _, obsolete = write('analysis', {'kind':'obsolete'})
            analytic, _ = write('analytics', {'analysis_ref':pinned, 'observed_analysis_ref':pinned})
            (root/'analysis/exports/current.json').write_text(json.dumps({'analysis_ref':current}))
            (root/'analytics/exports/current.json').write_text(json.dumps({'analytics_ref':analytic}))
            result = prune_publications(root, grace_seconds=0)
            self.assertEqual(len(result['removed']), 1)
            self.assertFalse(obsolete.exists())
            self.assertTrue(current_path.exists() and pinned_path.exists())
            pinned_path.write_bytes(b'broken')
            _, other = write('analysis', {'kind':'another'})
            with self.assertRaises(ValueError):
                prune_publications(root, grace_seconds=0)
            self.assertTrue(other.exists())
