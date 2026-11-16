"""fnv_guide: guides, tool search, engine research search. Every tool a guide names must be registered."""
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_guide  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from geck_mcp.fnv_guide import GuideTools, TOOL_NAME, load_guides, tool_table  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / 'geck_mcp'


def registered_tools():
    """Every fnv_* tool of the two dicts and every geck_* / fnv_* tool defined in the package source."""
    names = set(tool_table())
    for path in PACKAGE.glob('*.py'):
        names.update(re.findall(r'^\s+async def ((?:geck|fnv)_[a-z0-9_]+)\(', path.read_text(encoding='utf-8'), re.M))
    return names


def split_planned(text):
    """(text before the Planned section, the Planned section)."""
    head, _, planned = text.partition('\n## Planned')
    return head, planned


class GuideFileTests(unittest.TestCase):
    def test_guides_have_title_summary_words_and_sections(self):
        guides = load_guides()
        self.assertGreaterEqual(set(guides), {'crash', 'script'})
        for name, g in guides.items():
            self.assertTrue(g['title'] and g['summary'] and g['words'], name)
            for heading in ('## Steps', '## What the answers mean', '## Known traps', '## Checks that prove the fix'):
                self.assertIn(heading, g['text'], '%s lacks %s' % (name, heading))

    def test_every_tool_a_guide_names_exists(self):
        known = registered_tools()
        self.assertIn('fnv_crash', known)
        self.assertIn('geck_status', known)
        for name, g in load_guides().items():
            head, planned = split_planned(g['text'])
            named = set(TOOL_NAME.findall(head))
            self.assertTrue(named, name)
            self.assertEqual(sorted(named - known), [], 'guide %s names tools that are not registered' % name)
            # the planned section must name tools that do not exist yet, so it is removed when they arrive
            self.assertEqual(sorted(set(TOOL_NAME.findall(planned)) & known), [], 'guide %s: planned tool exists' % name)

    def test_the_check_would_catch_a_bad_name(self):
        known = registered_tools()
        self.assertIn('fnv_not_a_tool', set(TOOL_NAME.findall('call `fnv_not_a_tool` now')) - known)

    def test_both_new_tools_are_registered(self):
        from geck_mcp.fnv_live_tools import FNV_LIVE_TOOLS
        self.assertEqual(FNV_LIVE_TOOLS['fnv_guide'][0], 'read')
        self.assertEqual(FNV_LIVE_TOOLS['fnv_warnings'][0], 'control')


class GuideToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.research = Path(self.tmp.name)
        (self.research / 'systems.md').write_text(
            '# Research: systems\n\n## 1. Barter\nBarter price comes from BarterMenu_GetSortValue 0x0072ed00.\n'
            'It uses the speech skill.\n\n### Fast travel\nPlayerCharacter::FastTravel 0x0093cdf0 runs hourly steps.\n\n'
            '## 2. Crime\nAlarm functions only.\n', encoding='utf-8')
        (self.research / 'other.txt').write_text('barter in a text file is ignored', encoding='utf-8')
        self.tools = GuideTools(str(self.research))

    def tearDown(self):
        self.tmp.cleanup()

    def test_list(self):
        out = self.tools.run('list')
        self.assertEqual(list(out)[:2], ['verdict', 'reason'])
        self.assertEqual({r['name'] for r in out['rows']}, set(load_guides()))

    def test_topic_by_name_and_by_problem_words(self):
        by_name = self.tools.run('topic', 'crash')
        self.assertIn('# The game crashed', by_name['guide'])
        self.assertEqual(by_name['reason'], 'Exact name.')
        by_words = self.tools.run('topic', 'my game crashed to desktop after loading a save')
        self.assertTrue(by_words['verdict'].endswith('(crash)'))
        self.assertTrue(any(c['name'] == 'script' for c in by_words['otherCandidates']) or by_words['otherCandidates'] == [])
        script = self.tools.run('topic', 'my quest script does not run')
        self.assertTrue(script['verdict'].endswith('(script)'))
        none = self.tools.run('topic', 'zzzz qqqq')
        self.assertTrue(none['verdict'].startswith('No guide matches'))
        with self.assertRaises(ValueError):
            self.tools.run('topic')

    def test_find_tools_and_guides(self):
        out = self.tools.run('find', 'crash log')
        names = [r['name'] for r in out['rows']]
        self.assertIn('fnv_crash', names)
        self.assertIn('crash', names)
        self.assertEqual(out['rows'][0]['name'].startswith(('fnv_crash', 'crash')), True)
        warn = self.tools.run('find', 'engine warnings missing files')
        self.assertIn('fnv_warnings', [r['name'] for r in warn['rows']][:5])
        paged = self.tools.run('find', 'script', limit=3, offset=1)
        self.assertEqual((paged['count'], paged['offset']), (3, 1))
        self.assertTrue(paged['more'])
        brief = self.tools.run('find', 'script', brief=True)
        self.assertEqual(set(brief['rows'][0]), {'kind', 'name', 'description'})
        self.assertTrue(self.tools.run('find', 'qqqqzzzz')['verdict'].startswith('No match'))

    def test_engine_search_research_sections(self):
        out = self.tools.run('engine', 'barter price')
        self.assertEqual(out['rows'][0]['heading'], 'Research: systems > 1. Barter')
        self.assertEqual((out['rows'][0]['file'], out['rows'][0]['line']), ('systems.md', 3))
        self.assertIn('BarterMenu_GetSortValue', out['rows'][0]['excerpt'])
        self.assertEqual(out['total'], 1)
        fast = self.tools.run('engine', 'fast travel')
        self.assertEqual(fast['rows'][0]['heading'], 'Research: systems > 1. Barter > Fast travel')
        self.assertEqual(self.tools.run('engine', 'nothing here')['total'], 0)

    def test_engine_search_facts(self):
        out = self.tools.run('engine', 'TESQuest stages')
        self.assertTrue(any(f['name'] == 'TESQuest.stages' and f['offset'] == '0x44' for f in out['facts']))

    def test_engine_without_research_folder_says_what_to_do(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FNV_RESEARCH_DIR', None)
            out = GuideTools(None).run('engine', 'quest stages')
        self.assertIn('No research folder', out['reason'])
        self.assertIn('fnv.research_dir', out['next'])
        self.assertIn('FNV_RESEARCH_DIR', out['next'])
        self.assertTrue(out['facts'])
        # Release plan Phase 7 (L8): with no folder of notes, the guides that ship with the server are searched
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FNV_RESEARCH_DIR', None)
            out = GuideTools(None).run('engine', 'change engine function')
        self.assertTrue(out['total'] >= 1, out)
        self.assertEqual(out['rows'][0]['source'], 'guide')
        self.assertIn('shipped guides', out['reason'])
        self.assertIn('fnv_func_find', out['next'])
        with mock.patch.dict(os.environ, {'FNV_RESEARCH_DIR': str(self.research)}):
            self.assertEqual(GuideTools(None).run('engine', 'barter price')['total'], 1)

    def test_unknown_action_and_resource_text(self):
        with self.assertRaises(ConvError):
            self.tools.run('nope')
        self.assertIn('# The game crashed', fnv_guide.guide_text('crash'))
        for bad in ('missing', '../crash', 'CRASH'):
            with self.assertRaises(ConvError):
                fnv_guide.guide_text(bad)


class RegistrationTests(unittest.TestCase):
    """The live tools register with a fake server (no mcp 2 needed): both new tools, right kinds, working end to end."""

    def test_register_and_call(self):
        import asyncio
        from geck_mcp import fnv_live_tools
        from geck_mcp.config import Project
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'mo2/profiles/Dev').mkdir(parents=True)
            (root / 'game').mkdir()
            project = Project.model_validate({
                'project_id': 'regtest', 'launcher': 'native', 'game_root': str(root / 'game'), 'plugin': 'F.esp',
                'records': [{'editor_id': 'A', 'form_id': '01000800'}], 'state_dir': str(root / 'state'),
                'helper': str(root / 'h.exe'), 'fnv': {'mo2_root': str(root / 'mo2'), 'mo2_profile': 'Dev'}})
            tools = {}

            class FakeServer:
                def tool(self, annotations=None):
                    def decorate(fn):
                        tools[fn.__name__] = (fn, annotations)
                        return fn
                    return decorate

            fnv_live_tools.register(FakeServer(), project, ('read', 'act', 'control'), dict, maintain=False)
            self.assertEqual(set(tools), set(fnv_live_tools.FNV_LIVE_TOOLS))
            for name, (kind, _) in fnv_live_tools.FNV_LIVE_TOOLS.items():
                self.assertEqual(tools[name][1], kind, name)

            async def call():
                topic = await tools['fnv_guide'][0](action='topic', words='script does not run')
                status = await tools['fnv_warnings'][0](action='status')
                bad = await tools['fnv_warnings'][0](action='bogus')
                enabled = await tools['fnv_warnings'][0](action='enable')
                return topic, status, bad, enabled
            topic, status, bad, enabled = asyncio.run(call())
            self.assertTrue(topic.structured_content['data']['verdict'].endswith('(script)'))
            self.assertFalse(status.structured_content['data']['enabled'])
            self.assertTrue(bad.is_error)
            self.assertEqual(bad.structured_content['error']['code'], 'UNKNOWN_ACTION')
            self.assertEqual(enabled.structured_content['outcome'], 'acknowledged')
            self.assertTrue((root / 'mo2/overwrite/NVSE/nvse_config.ini').exists())


if __name__ == '__main__':
    unittest.main()
