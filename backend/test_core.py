import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from commands import command_suggestions, parse_done
from history import MAX_CHATS
from providers import PROVIDER_PRESETS

class CoreTests(unittest.TestCase):
    def test_provider_catalog(self):
        self.assertGreaterEqual(len(PROVIDER_PRESETS), 20)
        self.assertTrue(any(p['kind']=='local' for p in PROVIDER_PRESETS))
        self.assertTrue(any(p['kind']=='online' for p in PROVIDER_PRESETS))

    def test_command_matching(self):
        result = command_suggestions('/prov')
        self.assertEqual(result[0]['command'], '/provider')

    def test_done_marker(self):
        self.assertEqual(parse_done('/done build app'), (True, 'build app'))
        self.assertEqual(parse_done('build app /done'), (True, 'build app'))
        self.assertFalse(parse_done('please /done build')[0])

    def test_history_limit_constant(self):
        self.assertEqual(MAX_CHATS, 30)

if __name__ == '__main__':
    unittest.main()
