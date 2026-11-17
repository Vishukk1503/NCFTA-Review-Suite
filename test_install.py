"""Portable installation/export checks, using only invented source records."""
import tempfile
import unittest
from pathlib import Path

def synthetic_payload():
    text='Email: setup@example.invalid\nAccount: 001234567\n😀'
    return {'manifest':{'run_id':'synthetic-setup','groups':['6'],'events':1,'objects':0,'precision':'FP32'},
            'records':[{'review_id':'00000000-0000-5000-8000-000000000001','attribute_uuid':'00000000-0000-4000-8000-000000000001',
                        'group':'6','event_date':'2026-01-01','event_id':'0001',
                        'source_row':{'source_type':'other','source_comment':'Synthetic only','source_value':'001234567'},
                        'input':text,
                        'prediction':{'status':'predicted','warnings':['Synthetic warning'],'spans':[
                            {'field':'email','text':'setup@example.invalid','confidence':0.9,'accepted':True,'start':7,'end':28},
                            {'field':'bank_account','text':'001234567','confidence':0.8,'accepted':False,'reason':'Synthetic rejected suggestion','start':38,'end':47}]}}]}

class PortableExportTests(unittest.TestCase):
    def test_identifier_injection_unicode_and_rejected_spans(self):
        from openpyxl import load_workbook
        from export_xlsx import write_workbook
        data=synthetic_payload()
        data['records'][0]['source_row']['source_comment']=' =HYPERLINK("https://example.invalid")'
        data['records'][0]['input']+='\n\x00end'
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'review.xlsx'
            write_workbook(data,output)
            wb=load_workbook(output)
            self.assertEqual(wb.sheetnames,['Summary','Review','Predictions','Source text'])
            self.assertEqual(wb['Review']['F6'].value,'001234567')
            self.assertEqual(wb['Review']['M6'].value,'0001')
            self.assertTrue(wb['Review']['E6'].value.startswith("'"))
            self.assertEqual(wb['Review']['J6'].value,'Not reviewed')
            self.assertEqual(wb['Predictions']['E7'].value,'No')
            self.assertIn('😀',wb['Source text']['E6'].value)
            self.assertIn('\ufffd',wb['Source text']['E6'].value)
            self.assertFalse(any(c.data_type=='f' for ws in wb for row in ws for c in row))
            self.assertEqual(len(wb['Review'].data_validations.dataValidation),1)
            wb.close()
            with self.assertRaises(FileExistsError):write_workbook(data,output)

    def test_empty_workbook(self):
        from export_xlsx import write_workbook
        with tempfile.TemporaryDirectory() as temp:
            write_workbook({'manifest':{},'records':[]},Path(temp)/'empty.xlsx')

    def test_suite_combined_and_group_export(self):
        from unittest.mock import patch
        from openpyxl import load_workbook
        import suite
        class UI:
            def update(self,*args,**kwargs): pass
        with tempfile.TemporaryDirectory() as temp:
            cfg=dict(suite.settings(),excel_backend='openpyxl')
            run=suite.new_run(cfg,run=Path(temp)/'run')
            events=[{'id':str(group),'uuid':'event-'+str(group),'date':'2026-01-01',
                     'sharing_group_id':str(group),'Attribute':[{'uuid':'attribute-'+str(group),'type':'other','value':'001234567'}]}
                    for group in (6,7)]
            suite.save(run/'events.json',events)
            rows=suite.prepare(run,events)
            manifest=suite.read(run/'manifest.json')
            manifest['inference_complete']=True
            suite.save(run/'manifest.json',manifest)
            identity=suite.canonical(dict(assets='synthetic-assets',threshold=.5,records=manifest['records_sha256']))
            import json
            with (run/'predictions.jsonl').open('w',encoding='utf8') as stream:
                for row in rows:
                    stream.write(json.dumps(dict(review_id=row['review_id'],input_hash=suite.canonical(row['input']),identity=identity,status='predicted',warnings=[],spans=[]))+'\n')
            with patch.object(suite,'assets',return_value=(['email'],'synthetic-assets')):
                combined=suite.export(run,cfg,UI())
            wb=load_workbook(combined,read_only=True)
            self.assertEqual(wb['Review'].max_row,7)
            wb.close()
            for group in (6,7):
                workbook=next((run/f'group-{group}').glob('*.xlsx'))
                wb=load_workbook(workbook,read_only=True)
                self.assertEqual(wb['Review'].max_row,6)
                self.assertEqual(wb['Review']['B6'].value,str(group))
                wb.close()
            self.assertTrue((run/'group-8'/'NO-DATA.txt').is_file())

if __name__=='__main__':unittest.main()
