import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const modules=process.env.NCFTA_ARTIFACT_MODULES;
const require=createRequire(path.join(modules,'..','ncfta-export-loader.cjs'));
const {Workbook,SpreadsheetFile}=await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const [input,output]=process.argv.slice(2);
try{await fs.access(output);throw Error('Output already exists');}catch(e){if(e.code!=='ENOENT')throw e;}
const data=JSON.parse(await fs.readFile(input,'utf8')),m=data.manifest,rows=data.records;
const wb=Workbook.create();
const summary=wb.worksheets.add('Summary'),review=wb.worksheets.add('Review'),predictions=wb.worksheets.add('Predictions'),source=wb.worksheets.add('Source text');
const safe=v=>typeof v==='string'&&/^\s*[=+@-]/.test(v)?"'"+v:v??'';
const preview=v=>Array.from(String(v??'')).slice(0,1200).join('')+(Array.from(String(v??'')).length>1200?'\n[Full text on Source text]':'');
const rr=[],pp=[],ss=[];
for(const r of rows){
 const p=r.prediction,accepted=p.spans.filter(s=>s.accepted);
 rr.push([(r.attribute_uuid || "Missing UUID · "+r.review_id),r.group,r.event_date?new Date(r.event_date+'T00:00:00Z'):null,r.source_row.source_type,preview(r.source_row.source_comment),preview(r.source_row.source_value),preview(accepted.map(s=>s.field+': '+s.text).join('\n'))||(p.status==='predicted'?'(none)':'(not evaluated)'),p.status,p.warnings.join('; '),'Not reviewed','','',r.event_id,r.event_uuid,r.object_id,r.object_uuid,r.object_name,r.object_relation,r.attribute_uuid,r.attribute_distribution,r.attribute_sharing_group_id,r.first_seen??'',r.last_seen??'',p.warnings.length]);
 for(const s of p.spans){
  pp.push([(r.attribute_uuid || "Missing UUID · "+r.review_id),s.field,preview(s.text),s.confidence,s.accepted?'Yes':'No',s.reason??'',s.start,s.end,s.value_start??null,s.value_end??null]);
 }
 const chars=Array.from(r.input);
 for(let i=0;i<chars.length;i+=700)ss.push([(r.attribute_uuid || "Missing UUID · "+r.review_id),1+Math.floor(i/700),i,Math.min(i+700,chars.length),chars.slice(i,i+700).join(''),r.event_id,r.attribute_uuid]);
}
function table(sheet,headers,values,widths,name){
 const matrix=[headers,...values].map(row=>row.map(safe));
 if(matrix.length>1048570)throw Error('Excel row limit exceeded; split the date range');
 const range=sheet.getRangeByIndexes(4,0,matrix.length,headers.length);range.values=matrix;
 range.format.font={name:'Arial',size:10,color:'#19354A'};range.format.rowHeight=44;range.format.verticalAlignment='top';range.format.wrapText=true;
 widths.forEach((w,i)=>sheet.getRangeByIndexes(4,i,matrix.length,1).format.columnWidth=w);
 sheet.getRangeByIndexes(4,0,1,headers.length).format={fill:'#23465C',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},wrapText:true,rowHeight:34};
 if(values.length)sheet.tables.add(range,true,name).showFilterButton=true;
 sheet.showGridLines=false;sheet.freezePanes.freezeRows(5);sheet.freezePanes.freezeColumns(1);
 sheet.getRange('A2').values=[[sheet.name==='Review'?'NCFTA · v3 extraction review':name.replace(/Table$/,'')]];
 sheet.getRange('A2').format.font={name:'Arial',size:15,bold:true,color:'#19354A'};
}
table(review,['Attribute UUID / fallback','Group','Event date','Source type','Source comment','Source value preview','v3 extraction preview','Status','Warnings','Your decision','Correct fields / values','Your notes','Event ID','Event UUID','Object ID','Object UUID','Object name','Object relation','Attribute UUID','Attribute distribution','Attribute sharing group','First seen metadata','Last seen metadata','Warnings (count)'],rr,[40,9,14,22,30,50,55,19,40,22,40,40,15,38,15,38,24,24,38,16,16,26,26,17],'AttributeReviewTable');
review.getRange('A3').values=[['One row per source attribute. Yellow columns are editable. No reviewed ground truth: confidence is not accuracy.']];
if(rr.length){
 review.getRange(`C6:C${rr.length+5}`).setNumberFormat('yyyy-mm-dd');
 review.getRange(`J6:L${rr.length+5}`).format.fill='#FFF2CC';
 review.getRange(`J6:J${rr.length+5}`).dataValidation={rule:{type:'list',values:['Not reviewed','Correct','Partly correct','Incorrect','Missing extraction','Need more context']}};
 review.getRange(`I6:I${rr.length+5}`).conditionalFormats.add('containsText',{text:'missing',format:{fill:'#FCE4D6'}});
 for(let i=0;i<rr.length;i++)review.getRangeByIndexes(i+5,0,1,23).format.rowHeight=Math.min(160,Math.max(48,...rr[i].slice(4,9).map(s=>String(s).split('\n').length*15)));
}
table(predictions,['Attribute UUID / fallback','Field','Prediction text preview','Confidence','Accepted source span','Rejection reason','Input start','Input end','Value start','Value end'],pp,[40,30,65,15,20,42,14,14,14,14],'PredictionsTable');
predictions.getRange('A3').values=[['Offsets: Unicode characters, zero-based, end-exclusive. Full raw predictions remain in predictions.jsonl.']];
if(pp.length)predictions.getRange(`D6:D${pp.length+5}`).setNumberFormat('0.00%');
table(source,['Attribute UUID / fallback','Part','Input start','Input end','Exact input chunk','Event ID','Attribute UUID'],ss,[40,9,14,14,105,15,38],'SourceTextTable');
source.getRange('A3').values=[['Read numbered parts in order. Context is unchanged; only source-value spans are accepted. Raw JSON is retained with the run.']];
if(ss.length)source.getRange(`E6:E${ss.length+5}`).format.rowHeight=130;
summary.showGridLines=false;
summary.getRange('A1:B1').values=[['NCFTA extraction run','v3']];summary.getRange('A1:B1').format={fill:'#23465C',font:{name:'Arial',size:15,bold:true,color:'#FFFFFF'},rowHeight:32};
const values=[['Run ID',m.run_id],['Source',m.source??'NCFTA MISP'],['Event dates',m.filters?`${m.filters.from} to ${m.filters.to} (inclusive)`:(m.event_dates??[]).join(', ')],['Groups',(m.filters?.groups??m.groups??[]).join(', ')],['Events',m.events],['Objects',m.objects],['Attributes',rows.length],['Accepted predictions',pp.filter(r=>r[4]==='Yes').length],['Rows with warnings',rows.filter(r=>r.prediction.warnings.length).length],['No extraction (evaluated)',rows.filter(r=>r.prediction.status==='predicted'&&!r.prediction.spans.some(s=>s.accepted)).length],['Not evaluated',rows.filter(r=>r.prediction.status!=='predicted').length],['Precision / threshold',`${m.precision??'FP32'} / 0.50`],['Device',m.device??'No inference needed'],['Model SHA-256',m.model_sha256??''],['Record SHA-256',m.records_sha256],['Inference seconds',m.timing_note??(m.inference_sessions??[]).reduce((s,x)=>s+x.inference_seconds,0)],['Accuracy / F1','Unavailable: no independently reviewed answers'],['Scope','Attribute suggestions only; no ownership inferred and no writes to MISP'],['Source record','events.json plus attribute-records.jsonl; source IDs appear on Review'],['Date meaning','MISP event date; not necessarily crime or modification date']];
summary.getRangeByIndexes(2,0,values.length,2).values=values.map(r=>r.map(safe));summary.getRange(`A3:B${values.length+2}`).format={font:{name:'Arial',size:11,color:'#19354A'},rowHeight:30,wrapText:true,verticalAlignment:'center'};
summary.getRange('A1:A22').format.columnWidth=29;summary.getRange('B1:B22').format.columnWidth=100;
if(rows.length){
 summary.getRange('B9').formulas=[[`=COUNTA('Review'!A6:A${rows.length+5})`]];
 summary.getRange('B11').formulas=[[`=COUNTIFS('Review'!X6:X${rows.length+5},">0")`]];
}
wb.recalculate();
if(summary.getRange('B9').values[0][0]!==rows.length)throw Error('Workbook attribute count mismatch');
if(summary.getRange('B11').values[0][0]!==rows.filter(r=>r.prediction.warnings.length).length)throw Error('Workbook warning count mismatch');
const inspect=await wb.inspect({kind:'table',range:'Summary!A7:B13',include:'values,formulas',tableMaxRows:7,tableMaxCols:2});
await fs.writeFile(output+'.checks.json',JSON.stringify({rows:rr.length,predictions:pp.length,source_chunks:ss.length,summary:inspect.ndjson}));
if(process.env.NCFTA_RENDER==='1'){
 for(const [sheetName,range] of [['Summary','A1:B13'],['Review','A5:I9'],['Predictions','A5:F9'],['Source text','A5:E8']]){
  const image=await wb.render({sheetName,range,scale:1,format:'png'});await fs.writeFile(output+'.'+sheetName.replaceAll(' ','-')+'.png',new Uint8Array(await image.arrayBuffer()));
 }
}
await (await SpreadsheetFile.exportXlsx(wb)).save(output);
console.log('Workbook exported');
