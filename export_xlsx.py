"""Portable Excel review export; no Node or Codex installation required."""
from __future__ import annotations
import json
import re
from pathlib import Path

def safe(value):
    if value is None:
        return ''
    if isinstance(value, str):
        # XML 1.0 control characters cannot be stored in XLSX; raw JSON is authoritative.
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '\ufffd', value)
        if re.match(r'^\s*[=+@-]', value):
            value = "'" + value
    return value

def preview(value):
    text = str(value or '')
    return text[:1200] + ('\n[Full text in source JSON / prediction journal]' if len(text) > 1200 else '')

def write_workbook(data, output):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.worksheet.datavalidation import DataValidation
    output = Path(output)
    if output.exists():
        raise FileExistsError('Choose a new workbook path; reviewer edits are never overwritten.')
    m, rows = data['manifest'], data['records']
    wb = Workbook()
    wb.remove(wb.active)
    summary, review, predictions, source = [wb.create_sheet(n) for n in ('Summary','Review','Predictions','Source text')]
    rr, pp, ss = [], [], []
    for r in rows:
        p = r['prediction']
        rid = r.get('attribute_uuid') or 'Missing UUID · ' + r['review_id']
        accepted = [s for s in p['spans'] if s['accepted']]
        source_row = r['source_row']
        rr.append([rid,r['group'],r.get('event_date',''),source_row.get('source_type',''),preview(source_row.get('source_comment','')),preview(source_row.get('source_value','')),preview('\n'.join(s['field']+': '+s['text'] for s in accepted)) or ('(none)' if p['status']=='predicted' else '(not evaluated)'),p['status'],'; '.join(p['warnings']),'Not reviewed','','',r.get('event_id',''),r.get('event_uuid',''),r.get('object_id',''),r.get('object_uuid',''),r.get('object_name',''),r.get('object_relation',''),r.get('attribute_uuid',''),r.get('attribute_distribution',''),r.get('attribute_sharing_group_id',''),r.get('first_seen',''),r.get('last_seen',''),len(p['warnings'])])
        for s in p['spans']:
            pp.append([rid,s['field'],preview(s['text']),s.get('confidence'), 'Yes' if s['accepted'] else 'No',s.get('reason',''),s['start'],s['end'],s.get('value_start'),s.get('value_end')])
        text = r['input']
        for start in range(0,len(text),700):
            ss.append([rid,start//700+1,start,min(start+700,len(text)),text[start:start+700],r.get('event_id',''),r.get('attribute_uuid','')])
    def table(ws, headers, values, widths, title, note):
        if len(values)+5>1048576:
            raise ValueError('Excel row limit exceeded; split the date range.')
        ws.sheet_view.showGridLines = False
        ws['A2'] = title
        ws['A2'].font = Font(name='Calibri',size=16,bold=True,color='23465C')
        ws['A3'] = note
        ws.freeze_panes = 'B6'
        for i,width in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=width
        for row_index,values_row in enumerate([headers]+values,5):
            for col_index,value in enumerate(values_row,1):
                cell=ws.cell(row_index,col_index,safe(value))
                cell.alignment=Alignment(vertical='top',wrap_text=True)
                cell.font=Font(name='Calibri',size=11,color='19354A')
                if row_index==5:
                    cell.fill=PatternFill('solid',fgColor='23465C')
                    cell.font=Font(name='Calibri',size=11,bold=True,color='FFFFFF')
            ws.row_dimensions[row_index].height=34 if row_index==5 else 60
        if values:
            tab=Table(displayName=ws.title.replace(' ','')+'Table',ref=f'A5:{get_column_letter(len(headers))}{len(values)+5}')
            tab.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
            ws.add_table(tab)
    table(review,['Attribute UUID / fallback','Group','Event date','Source type','Source comment','Source value preview','v3 extraction preview','Status','Warnings','Your decision','Correct fields / values','Your notes','Event ID','Event UUID','Object ID','Object UUID','Object name','Object relation','Attribute UUID','Attribute distribution','Attribute sharing group','First seen metadata','Last seen metadata','Warnings (count)'],rr,[40,9,14,22,30,50,55,19,40,22,40,40,15,38,15,38,24,24,38,16,16,26,26,17],'NCFTA · v3 extraction review','One row per attribute. Yellow columns are editable. Confidence is not accuracy.')
    table(predictions,['Attribute UUID / fallback','Field','Prediction text preview','Confidence','Accepted source span','Rejection reason','Input start','Input end','Value start','Value end'],pp,[40,30,65,15,20,42,14,14,14,14],'Predictions','Offsets are Unicode characters, zero-based, end-exclusive. Full predictions remain in predictions.jsonl.')
    table(source,['Attribute UUID / fallback','Part','Input start','Input end','Exact input chunk','Event ID','Attribute UUID'],ss,[40,9,14,14,105,15,38],'Source text','Read parts in order. XML control characters are replaced for Excel display; raw JSON preserves the exact source.')
    if rr:
        validation=DataValidation(type='list',formula1='"Not reviewed,Correct,Partly correct,Incorrect,Missing extraction,Need more context"')
        review.add_data_validation(validation)
        validation.add(f'J6:J{len(rr)+5}')
    for row in review.iter_rows(min_row=6,max_row=len(rr)+5,min_col=10,max_col=12) if rr else []:
        for cell in row: cell.fill=PatternFill('solid',fgColor='FFF2CC')
    for row in predictions.iter_rows(min_row=6,max_row=len(pp)+5,min_col=4,max_col=4) if pp else []:
        row[0].number_format='0.00%'
    filters=m.get('filters')
    dates=(f"{filters['from']} to {filters['to']} (inclusive)" if filters else ', '.join(m.get('event_dates',[])))
    values=[['Run ID',m.get('run_id','')],['Source',m.get('source','NCFTA MISP')],['Event dates',dates],['Groups',', '.join(map(str,(filters or {}).get('groups',m.get('groups',[]))))],['Events',m.get('events',0)],['Objects',m.get('objects',0)],['Attributes',len(rows)],['Accepted predictions',sum(s[4]=='Yes' for s in pp)],['Rows with warnings',sum(bool(r['prediction']['warnings']) for r in rows)],['No extraction (evaluated)',sum(r['prediction']['status']=='predicted' and not any(s['accepted'] for s in r['prediction']['spans']) for r in rows)],['Not evaluated',sum(r['prediction']['status']!='predicted' for r in rows)],['Precision / threshold',str(m.get('precision','FP32'))+' / 0.50'],['Device',m.get('device','No inference needed')],['Model SHA-256',m.get('model_sha256','')],['Record SHA-256',m.get('records_sha256','')],['Inference seconds',m.get('timing_note',sum(s.get('inference_seconds',0) for s in m.get('inference_sessions',[])))],['Accuracy / F1','Unavailable: no independently reviewed answers'],['Scope','Attribute suggestions only; no ownership inferred and no writes to MISP'],['Source record','events.json plus attribute-records.jsonl; prediction details in predictions.jsonl'],['Date meaning','MISP event date; not necessarily crime or modification date']]
    table(summary,['NCFTA extraction run','v3'],values,[29,100],'Run summary','Counts describe saved extraction output, not reviewed accuracy.')
    # Static observed counts need no calculation engine on the work PC.
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream: wb.save(stream)
    check=load_workbook(output,read_only=True,data_only=False)
    try:
        if check['Review'].max_row != len(rr)+5 or check['Predictions'].max_row != len(pp)+5:
            raise ValueError('Workbook row count mismatch')
    finally: check.close()
    output.with_suffix('.xlsx.checks.json').write_text(json.dumps(dict(rows=len(rr),predictions=len(pp),source_chunks=len(ss),exporter='openpyxl',formula_cells=0)),encoding='utf8')

if __name__=='__main__':
    import sys
    write_workbook(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')),sys.argv[2])
