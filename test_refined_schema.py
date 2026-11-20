"""Layer-3 catalog and deployment-artifact checks; no Snowflake connection."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('ncfta_refined_schema', ROOT / 'snowflake' / 'build_refined_schema.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class RefinedSchemaTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(builder.CATALOG.read_text(encoding='utf-8-sig'))
        self.columns = builder.schema(self.catalog)

    def test_exact_catalog_order_and_types(self):
        business = self.columns[len(builder.TECHNICAL):]
        self.assertEqual(len(self.columns), 151)
        self.assertEqual(len({c['name'] for c in self.columns}), 151)
        self.assertEqual([c['name'].lower() for c in business], [f['key'] for f in self.catalog['fields']])
        self.assertEqual(sum(c['data_type'] == 'ARRAY' for c in business), 116)
        self.assertEqual({c['name'].lower() for c in business if c['data_type'] == 'VARCHAR'},
                         {'observation_first_seen', 'observation_last_seen'})
        self.assertTrue(all(not c['required'] and c['default'] is None for c in business))

    def test_lineage_and_deletion_contract(self):
        columns = {c['name']: c for c in self.columns}
        for name in ('RESULT_ID', 'SOURCE_BATCH_ID', 'EVENT_SNAPSHOT_ID', 'SOURCE_INSTANCE_ID',
                     'SOURCE_REVIEW_ID', 'SOURCE_ATTRIBUTE_SHA256', 'PREDICTION_IDENTITY',
                     'MODEL_SHA256', 'CATALOG_SHA256', 'TRANSFORM_VERSION', 'CREATED_BY_LOAD_RUN_ID'):
            self.assertTrue(columns[name]['required'], name)
        for name in ('EVENT_UUID', 'OBJECT_UUID', 'ATTRIBUTE_UUID', 'SOURCE_EVENT_ID',
                     'SOURCE_OBJECT_ID', 'SOURCE_ATTRIBUTE_ID', 'SOURCE_ATTRIBUTE_DELETED', 'SOURCE_OBJECT_DELETED'):
            self.assertFalse(columns[name]['required'], name)
        self.assertEqual(columns['IS_DELETED']['data_type'], 'BOOLEAN')
        self.assertTrue(columns['IS_DELETED']['required'])
        self.assertIsNone(columns['IS_DELETED']['default'])
        self.assertEqual(columns['REVIEW_STATUS']['default'], "'NOT_REVIEWED'")

    def test_invalid_catalogs_fail_instead_of_silent_schema_drift(self):
        mutations = []
        missing = copy.deepcopy(self.catalog); missing['fields'].pop(); mutations.append(missing)
        duplicate = copy.deepcopy(self.catalog); duplicate['fields'][-1]['key'] = duplicate['fields'][0]['key']; mutations.append(duplicate)
        invalid = copy.deepcopy(self.catalog); invalid['fields'][0]['key'] = 'x); DROP TABLE x;'; mutations.append(invalid)
        collision = copy.deepcopy(self.catalog); collision['fields'][0]['key'] = 'source_type'; mutations.append(collision)
        metadata = copy.deepcopy(self.catalog)
        next(f for f in metadata['fields'] if f['key'] == 'observation_first_seen')['key'] = 'new_field'
        mutations.append(metadata)
        for catalog in mutations:
            with self.subTest(keys=[f['key'] for f in catalog['fields']][:1]), self.assertRaises(ValueError):
                builder.schema(catalog)

    def test_sql_comments_escape_catalog_quotes(self):
        self.catalog['fields'][0]['definition'] = "Source's label\nremains literal."
        sql = builder.render(builder.schema(self.catalog))
        self.assertIn("Source''s label remains literal.", sql)

    def test_checked_in_sql_is_current_and_verification_covers_every_column(self):
        self.assertEqual((builder.HERE / '03_refined.sql').read_text(encoding='utf-8'), builder.render(self.columns))
        verify = (builder.HERE / '03_verify_refined.sql').read_text(encoding='utf-8')
        self.assertEqual(verify, builder.verification(self.columns))
        expected_names = re.findall(r"^    \('([A-Z_][A-Z0-9_]*)',", verify, re.M)
        self.assertEqual(expected_names, [c['name'] for c in self.columns])
        sql = re.sub(r'--[^\n]*', '', builder.render(self.columns))
        self.assertEqual(sql.count('CREATE TABLE IF NOT EXISTS REFINED.NCFTA_FIELDS'), 1)
        self.assertNotRegex(sql, r'\b(?:DROP|TRUNCATE|DELETE|INSERT|UPDATE|GRANT)\s')

    def test_changed_catalog_checksum_blocks_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / 'catalog.json'
            catalog.write_bytes(builder.CATALOG.read_bytes() + b'\n')
            with patch.object(builder, 'CATALOG', catalog), patch.object(builder, 'HERE', root):
                with self.assertRaisesRegex(ValueError, 'checksum changed'):
                    builder.build()
            self.assertFalse((root / '03_refined.sql').exists())


if __name__ == '__main__':
    unittest.main()
