import tempfile
import unittest
from pathlib import Path
from tools.realapp_dataset.core import save
from tools.realapp_dataset.oom_ledger import build


class LedgerTests(unittest.TestCase):
    def test_duplicate_evidence_counts_one_process_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['original','reanalysis']:
                p=root/name;p.mkdir()
                save(p/'result.json',{})
                save(p/'original_state.json',{'boot_id':'boot'})
                save(p/'events.json',{'events':[{'category':'oom_kill','pid':12,
                    'identity':{'start_ticks':'123'},'time':10},
                    {'category':'application_crash','pid':13,'time':11}],
                    'oom_label_coverage_complete':False})
            result=build(root)
            self.assertEqual(result['confirmed_oom_count'],1)
            self.assertEqual(len(result['incidents'][0]['evidence_files']),2)
            self.assertFalse(result['runs'][0]['oom_coverage_complete'])

    def test_pid_reuse_counts_separate_lifetimes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'run';p.mkdir()
            save(p/'result.json',{})
            save(p/'events.json',{'events':[{'category':'oom_kill','pid':12,
                'identity':{'start_ticks':start},'time':10} for start in ['123','456']]})
            self.assertEqual(build(root)['confirmed_oom_count'],2)
