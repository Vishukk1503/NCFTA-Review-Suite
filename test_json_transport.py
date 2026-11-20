import copy
import io
import json
import unittest
import uuid
from rich.console import Console
import snowflake_loader as loader
from test_raw_loader import fixture, Session
import raw_loader as raw
import refined_loader as refined

def normalize(value):
    if isinstance(value, float) and value.is_integer(): return int(value)
    if isinstance(value, dict): return {k:normalize(v) for k,v in value.items()}
    if isinstance(value, list): return [normalize(v) for v in value]
    return value

class Tests(unittest.TestCase):
    def test_exact_batch_and_stage_retry(self):
        b,e,r=fixture()
        b['CATALOG_SHA256']=refined.CATALOG_SHA256
        b['SOURCE_MANIFEST']['seconds']=1.0
        e[0][0]['metric']=2.0
        r[0][0]['prediction']['seconds']=3.0
        session=Session(b,e,r)
        first=raw.run(session,raw.encoded(b),session.files,str(uuid.uuid4()))
        self.assertEqual(first['status'],'SUCCESS',first)
        session.tables=normalize(session.tables)
        second=raw.run(session,raw.encoded(b),session.files,str(uuid.uuid4()))
        self.assertEqual(second['status'],'NO_OP',second)
        tables=session.tables
        refined.validate_raw(tables['NCFTA_BATCHES'][0],tables['NCFTA_EVENTS'],tables['NCFTA_MODEL_RESULTS'])

    def test_tampered_variant_rejected(self):
        b,e,r=fixture(); b,e,r=raw.prepare(b,e,r)
        e[0]['EVENT_PAYLOAD']['id']='changed'
        with self.assertRaisesRegex(raw.Invalid,'RAW_JSON_CONFLICT'):
            refined.validate_raw(b,e,r)

    def test_boolean_is_not_number(self):
        self.assertFalse(raw.equivalent({'flag':True},{'flag':1}))

    def test_rich_activity(self):
        stream=io.StringIO();console=Console(file=stream,force_terminal=True)
        with loader.activity(console,'Loading RAW'):
            console.print('RAW SUCCESS | inserted: 1')
        self.assertIn('RAW SUCCESS',stream.getvalue())
