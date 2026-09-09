from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from our_harness.playwright_scenarios import local_browser_scenario
from our_harness import swarm_work


SOURCE = r'''const {test,expect}=require('@playwright/test');
test('changes the marker',async({page})=>{
  await page.goto(`/nested/board.html`);
  await expect(page.locator('#status')).toHaveText(`Player's turn`);
  await page.locator('[data-slot="7"]').click();
  await expect(page.locator('#status')).toHaveText('Done');
  await expect(page.locator('.chosen')).toHaveCount(1);
});'''


class LocalPlaywrightScenarioTests(unittest.TestCase):
    def test_selection_excludes_dependencies_and_preserves_filters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('tests/selected.spec.cjs', 'other/unrelated.test.js', 'node_modules/pkg/hidden.test.js'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(SOURCE)
            command = ['node', 'playwright', 'test', 'tests/', '--grep', 'desired', '--project', 'chromium']
            self.assertEqual(swarm_work._playwright_spec_files(root, command), [root / 'tests/selected.spec.cjs'])
            self.assertEqual(len(swarm_work._playwright_spec_files(root, ['node','playwright','test'])), 2)
            self.assertEqual(swarm_work._playwright_spec_files(root, ['node','playwright','test','missing/']), [])
            with mock.patch.object(swarm_work, 'run_brokered_playwright_suite', return_value={'passed':False,'broker':{}}) as run:
                swarm_work._run_brokered_playwright_specs(root, command, timeout=5)
            self.assertEqual(run.call_args.args[1], command[2:])
            with mock.patch.object(swarm_work, 'run_brokered_playwright_suite', return_value={'passed':False,'broker':{}}) as run:
                swarm_work._run_brokered_playwright_specs(root, ['node','playwright','test','tests/selected.spec.cjs:999'], timeout=5)
            self.assertEqual(run.call_args.args[1][-1], 'tests/selected.spec.cjs:999')
            (root / 'custom.cjs').write_text("module.exports={use:{baseURL:'https://example.com/'}};")
            with mock.patch.object(swarm_work, 'run_brokered_playwright_suite', return_value={'passed':False,'broker':{}}) as run:
                swarm_work._run_brokered_playwright_specs(root, ['node','playwright','test','tests/','--config','custom.cjs'], timeout=5)
            self.assertEqual(run.call_args.args[2], 'https://example.com/')

    @unittest.skipUnless(os.name == 'nt', 'Windows contained ordinary local suite')
    def test_ordinary_local_suite_fixtures_loops_keyboard_modules_and_selection(self):
        runtime = swarm_work.discover_bundled_playwright_runtime(required=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'nested').mkdir()
            (root / 'nested/index.html').write_text('<input aria-label="Name"><button>Save</button><p id="status">Waiting</p><script type="module" src="app.mjs"></script>')
            (root / 'nested/app.mjs').write_text("document.querySelector('button').onclick=()=>setTimeout(()=>document.querySelector('#status').textContent='  Saved   Ada  ',150);")
            source = '''const {test,expect}=require('@playwright/test');
test('desired ordinary',async({page})=>{
 await page.goto('/nested/');
 for(const value of ['A','Ada']){await page.getByRole('textbox',{name:'Name'}).fill(value);}
 await page.getByRole('textbox',{name:'Name'}).press('End');
 await page.getByRole('button',{name:'Save'}).click();
 await expect(page.locator('#status')).toHaveText('Saved Ada');
 await expect(page.getByRole('textbox')).toHaveValue('Ada');
});
test('excluded failure',async()=>{expect(true).toBe(false);});'''
            (root / 'ordinary.spec.cjs').write_text(source)
            result = swarm_work._run_brokered_playwright_specs(root,
                ['node','playwright','test','ordinary.spec.cjs','--grep','desired','--workers','3'],timeout=40,runtime=runtime)
            self.assertEqual(result['exit_code'], 0, result)
            receipt = result['brokered_e2e_receipts'][0]['receipt']
            self.assertEqual(len(receipt['tests']), 1, receipt)
            self.assertTrue(receipt['external_write_denied'])
            (root / 'ordinary.spec.cjs').write_text(source.replace("toHaveValue('Ada')", "toHaveValue('Wrong')"))
            failed = swarm_work._run_brokered_playwright_specs(root,
                ['node','playwright','test','ordinary.spec.cjs','--grep','desired','--retries=0'],timeout=40,runtime=runtime)
            self.assertNotEqual(failed['exit_code'], 0, failed)
            self.assertFalse(failed['containment_unavailable'], failed)

    def test_templates_and_all_assertions_keep_source_order(self):
        parsed = local_browser_scenario(SOURCE)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['schema_version'], 2)
        self.assertEqual([s['action'] for s in parsed['steps']], ['assert', 'click', 'assert', 'assert'])
        self.assertEqual(parsed['steps'][0]['value'], "Player's turn")
        self.assertEqual(parsed['steps'][1]['selector'], '[data-slot="7"]')
        self.assertEqual(parsed['steps'][-1]['value'], 1)
        self.assertEqual(local_browser_scenario(SOURCE), json.loads(json.dumps(parsed)))

    def test_escaped_strings_and_comments_do_not_become_browser_code(self):
        source = SOURCE.replace("`Player's turn`", r"'Player\'s turn'")
        source = "/* await page.locator('wrong').click(); */\n" + source
        source += "\n// await expect(page.locator('wrong')).toHaveText('bad')"
        self.assertEqual(local_browser_scenario(source), local_browser_scenario(SOURCE))
        unicode_source = SOURCE.replace("`Player's turn`", r'"\u00c5\uD83C\uDF1F"')
        self.assertEqual(local_browser_scenario(unicode_source)['steps'][0]['value'], 'Å🌟')

    def test_unsupported_or_dynamic_steps_are_never_silently_omitted(self):
        cases = [
            SOURCE.replace("`Player's turn`", "`${player}'s turn`"),
            SOURCE.replace(".toHaveCount(1)", ".toHaveCount(expected)"),
            SOURCE.replace(".click()", ".dblclick()"),
            SOURCE.replace(".toHaveText('Done')", ".toContainText('Done')"),
            SOURCE.replace(".click()", ".click({force:true})"),
            SOURCE.replace("await page.locator", "if (ready) await page.locator"),
            SOURCE.replace("test('changes", "test.skip('changes"),
            SOURCE.replace("await page.locator", "ready && await page.locator"),
            SOURCE.replace("await page.locator('[data-slot=\"7\"]').click();", "const neverCalled=async()=>{await page.locator('[data-slot=\"7\"]').click();};"),
            SOURCE + "test('another',async({page})=>{await page.goto('/other');});",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(local_browser_scenario(source))

    @unittest.skipUnless(os.name == 'nt', 'Windows contained browser')
    def test_real_browser_checks_initial_intermediate_and_final_states(self):
        runtime = swarm_work.discover_bundled_playwright_runtime()
        if runtime is None:
            self.skipTest('Bundled browser unavailable')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'nested').mkdir()
            (root / 'nested/board.html').write_text('''<p id="status">Player's turn</p>
<button data-slot="7" onclick="document.querySelector('#status').textContent='Done';this.className='chosen'">Go</button>''')
            for source, passed in ((SOURCE, True), (SOURCE.replace("`Player's turn`", "`Wrong initial state`"), False),
                                   (SOURCE.replace('toHaveCount(1)', 'toHaveCount(2)'), False)):
                with self.subTest(passed=passed, source=source):
                    result = swarm_work._run_brokered_playwright_scenario(
                        root, local_browser_scenario(source), timeout=30, runtime=runtime,
                    )
                    self.assertEqual(result['passed'], passed, result)
                    if passed:
                        self.assertEqual(len(result['receipt']['assertions']), 3)
                        self.assertTrue(all(one['passed'] for one in result['receipt']['assertions']))
                    else:
                        self.assertIn('Assertion failed', result['receipt']['error'])
                        self.assertFalse(result['receipt']['passed'])
