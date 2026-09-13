import unittest
from pathlib import Path
from unittest.mock import patch
from our_harness.providers import subscription_cli as cli


class ManagedCliRefreshTests(unittest.TestCase):
    def test_retained_legacy_managed_binary_follows_verified_newer_build(self):
        old = r'C:\Users\arbitrary\AppData\Local\Packages\OpenAI.Codex_any\LocalCache\Local\OpenAI\Codex\bin\codex.exe'
        newer = Path('arbitrary-new-build/codex.exe')
        with patch.object(cli.shutil, 'which', return_value=old), \
             patch.object(cli, '_every_build_of', return_value=[]), \
             patch.object(cli, '_where_else_it_might_be', return_value=[newer]), \
             patch.object(cli, '_the_version_of', side_effect=lambda value: (2,0) if value == str(newer) else (1,0)):
            self.assertEqual(cli.available('codex-cli', [old]), str(newer))

    def test_repeated_separators_do_not_hide_a_known_managed_path(self):
        path = r'C:\\Users\\arbitrary\\AppData\\Local\\OpenAI\\Codex\\bin\\build\\codex.exe'
        self.assertTrue(cli._rediscoverable_codex_command('codex-cli', [path]))
        self.assertFalse(cli._rediscoverable_codex_command('codex-cli', [path, '--custom-flag']))

    def test_custom_executable_remains_exact_authority(self):
        with patch.object(cli.shutil, 'which', return_value='custom/codex.exe'), \
             patch.object(cli, '_where_else_it_might_be') as discover:
            self.assertEqual(cli.available('codex-cli', ['custom/codex.exe']), 'custom/codex.exe')
            discover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
