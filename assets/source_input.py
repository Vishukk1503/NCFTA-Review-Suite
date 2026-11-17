"""Literal model-input contract shared by preparation and live inference.

No semantic labeling, abbreviation expansion, relation inference, or source
rewriting. Object metadata stays in provenance until a matching deployed
contract explicitly adds it to both training and inference.
"""
VERSION = 'ncfta-attribute-context-v1'

def packet(row):
    text='';segments=[]
    for key,label,suffix,eligible in [
        ('source_type','Source type: ','\n',False),
        ('source_category','Source category: ','\n',False),
        ('source_comment','Source comment: ','\n',False),
        ('source_value','Source value:\n','',True),
    ]:
        value=row[key]
        if not isinstance(value,str):raise TypeError(key+' must be an unchanged string')
        text+=label;start=len(text);text+=value
        segments.append(dict(column=key,start=start,end=len(text),target_eligible=eligible))
        text+=suffix
    return dict(version=VERSION,text=text,segments=segments,
                source_value_offset=next(s['start'] for s in segments if s['column']=='source_value'))

def input_text(row):
    p=packet(row)
    return p['text'],p['source_value_offset']

def source_span(p,start,end,text):
    """Map a trained-model literal span back to its exact JSON source column.

    Reject model-generated text, scaffold labels, and spans crossing columns.
    Caller supplies the unchanged schema field key; this never reclassifies it.
    """
    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(p['text']):
        raise ValueError('Invalid character span')
    if p['text'][start:end]!=text:raise ValueError('Prediction is not literal source evidence')
    for s in p['segments']:
        if s['target_eligible'] and s['start']<=start<end<=s['end']:
            return dict(column=s['column'],start=start-s['start'],end=end-s['start'],text=text)
    raise ValueError('Prediction is in context scaffolding or crosses source columns')
