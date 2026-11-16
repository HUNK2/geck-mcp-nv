"""TOOL-MERGE step 0: the tool list is emitted slim, with no change to what a call may send.

- no `title` keyword in any input or output schema (pydantic writes one per field and model);
- an optional parameter is emitted as its own type, not `anyOf [X, null]` with `default: null`;
- descriptions carry no docstring indentation.

Calls are checked by each tool's pydantic model, not by the emitted schema, so a client that still sends null for
an optional parameter keeps working; the last test checks that for every tool."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

import test_service  # noqa: E402
from geck_mcp.server import make_server, slim_schema  # noqa: E402

SCHEMA_LISTS = ('anyOf', 'oneOf', 'allOf', 'prefixItems')
SCHEMA_MAPS = ('properties', '$defs', 'definitions', 'patternProperties')


def schema_nodes(node):
    """Every schema object inside a JSON schema (not the keys of a properties map)."""
    if not isinstance(node, dict):
        return
    yield node
    for key in SCHEMA_MAPS:
        for child in (node.get(key) or {}).values():
            yield from schema_nodes(child)
    for key in SCHEMA_LISTS:
        for child in node.get(key) or ():
            yield from schema_nodes(child)
    for key in ('items', 'additionalProperties', 'not'):
        if isinstance(node.get(key), dict):
            yield from schema_nodes(node[key])


class SlimSchemaUnitTests(unittest.TestCase):
    def test_titles_go_but_a_parameter_called_title_stays(self):
        schema = {'title': 'fooArguments', 'type': 'object',
                  'properties': {'title': {'title': 'Title', 'type': 'string'}},
                  '$defs': {'M': {'title': 'M', 'type': 'object', 'properties': {}}}}
        self.assertEqual(slim_schema(schema), {'type': 'object', 'properties': {'title': {'type': 'string'}},
                                               '$defs': {'M': {'type': 'object', 'properties': {}}}})

    def test_optional_collapses_to_its_type(self):
        schema = {'properties': {
            'a': {'anyOf': [{'type': 'string', 'maxLength': 5}, {'type': 'null'}], 'default': None,
                  'description': 'd'},
            'b': {'anyOf': [{'type': 'array'}, {'type': 'object'}, {'type': 'null'}], 'default': None},
            'c': {'anyOf': [{'type': 'integer'}, {'type': 'null'}], 'default': 3},        # a real default: kept
            'd': {'anyOf': [{'type': 'object'}, {'type': 'null'}]},                      # required null: kept
            'e': {'anyOf': [{'$ref': '#/$defs/M'}, {'type': 'null'}], 'default': None}}}
        props = slim_schema(schema)['properties']
        self.assertEqual(props['a'], {'type': 'string', 'maxLength': 5, 'description': 'd'})
        self.assertEqual(props['b'], {'anyOf': [{'type': 'array'}, {'type': 'object'}]})
        self.assertEqual(props['c'], schema['properties']['c'])
        self.assertEqual(props['d'], schema['properties']['d'])
        self.assertEqual(props['e'], {'$ref': '#/$defs/M'})

    def test_input_is_not_changed(self):
        schema = {'title': 't', 'properties': {'a': {'anyOf': [{'type': 'string'}, {'type': 'null'}],
                                                     'default': None, 'title': 'A'}}}
        before = json.dumps(schema)
        slim_schema(schema)
        self.assertEqual(json.dumps(schema), before)


class SlimListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def test_listing_is_slim(self):
        server = make_server(self.fixture.p, self.fixture.service)
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
        titled, nullable, indented = [], [], []
        for tool in tools:
            for which, schema in (('in', tool.input_schema), ('out', tool.output_schema)):
                for node in schema_nodes(schema or {}):
                    if isinstance(node.get('title'), str):
                        titled.append('%s.%s' % (tool.name, which))
                    if 'default' in node and node['default'] is None and any(
                            s.get('type') == 'null' for s in node.get('anyOf', ())):
                        nullable.append(tool.name)
            if '\n ' in (tool.description or ''):
                indented.append(tool.name)
        self.assertEqual(sorted(set(titled)), [])
        self.assertEqual(sorted(set(nullable)), [])
        self.assertEqual(indented, [])
        # The tool manager's own schemas are left as they were (the slim copy is only what is emitted).
        raw = server._tool_manager.get_tool('fnv_ref').parameters
        self.assertEqual(raw.get('title'), 'fnv_refArguments')

    async def test_the_envelope_schema_is_emitted_once_per_entry_tool(self):
        """The envelope (ok, operation, data, error, outcome) is every tool's answer. Its full schema is emitted on
        fnv_status and geck_status; the other tools emit a short object schema that names them. The tool manager
        keeps the full schema on every tool, so structured results are still checked against it."""
        server = make_server(self.fixture.p, self.fixture.service)
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        full = tools['geck_status'].output_schema
        self.assertIn('error', full.get('properties', {}))
        self.assertEqual(tools['fnv_status'].output_schema, full)
        wrong = []
        for name, tool in tools.items():
            out = tool.output_schema
            raw = slim_schema(server._tool_manager.get_tool(name).output_schema or {})
            if name in ('fnv_status', 'geck_status') or raw != full:
                if out != raw:
                    wrong.append((name, 'changed'))
                continue
            if out.get('type') != 'object' or 'fnv_status' not in out.get('description', '') or len(json.dumps(out)) > 400:
                wrong.append((name, out))
        self.assertEqual(wrong, [])
        self.assertLess(sum(len(json.dumps(t.output_schema or {})) for t in tools.values()), 80000)
        self.assertIn('error', server._tool_manager.get_tool('fnv_ref').output_schema.get('properties', {}))

    async def test_null_for_an_optional_parameter_still_validates(self):
        server = make_server(self.fixture.p, self.fixture.service)
        refused = {}
        for tool in server._tool_manager.list_tools():
            props = (tool.parameters or {}).get('properties', {})
            nulls = {name: None for name, schema in props.items()
                     if 'default' in schema and schema['default'] is None}
            if not nulls:
                continue
            try:
                tool.fn_metadata.arg_model.model_validate(nulls)
            except Exception as e:  # noqa: BLE001 - only errors on the null fields count (required ones are missing)
                errors = [err['loc'][0] for err in e.errors() if err['loc'] and err['loc'][0] in nulls]
                if errors:
                    refused[tool.name] = errors
        self.assertEqual(refused, {})


if __name__ == '__main__':
    unittest.main()
