"""Read-only, cached normalization of durable CLI logs into conversation events."""
import json
from collections import OrderedDict
from pathlib import Path

_cache = OrderedDict()

def reasoning_text(value):
    """Extract a provider-supplied reasoning summary without inventing one."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get('text') or value.get('summary') or value.get('content') or '')
    if isinstance(value, list):
        return '\n'.join(filter(None, (reasoning_text(item) for item in value)))
    return ''

def normalize(event):
    if not isinstance(event, dict):
        return []
    result = []
    def add(kind, text='', status='', output='', identity=None):
        result.append({'kind':kind,'text':str(text)[:16000 if kind=='message' else 6000 if kind=='reasoning' else 500],
                       'status':str(status),'output':str(output)[:16000],'event_id':identity})
    kind = event.get('type')
    item = event.get('item') or {}
    if kind in ('item.started','item.completed','item.updated'):
        identity = item.get('id')
        if item.get('type') == 'agent_message':
            add('message',item.get('text',''),identity=identity)
        elif item.get('type') == 'command_execution':
            add('command',item.get('command',''),item.get('status',''),item.get('aggregated_output',''),identity)
        elif item.get('type') == 'file_change':
            add('edit',', '.join(c.get('path','') for c in item.get('changes',[])),item.get('status',''),json.dumps(item.get('changes',[]),indent=2),identity)
        elif item.get('type') == 'reasoning':
            # These are concise summaries emitted by the CLI, not private model deliberation.
            text = reasoning_text(item.get('summary'))
            if text:
                add('reasoning',text,item.get('status',''),identity=identity)
        elif item.get('type') not in ('reasoning',None):
            add('tool',item.get('type'),item.get('status',''),json.dumps(item,indent=2),identity)
    if kind in ('assistant','user') and isinstance(event.get('message'),dict):
        for part in event['message'].get('content',[]):
            if not isinstance(part,dict):
                continue
            if part.get('type')=='text' and kind=='assistant':
                add('message',part.get('text',''))
            elif part.get('type') in ('thinking','reasoning') and kind=='assistant':
                text = reasoning_text(part.get('summary'))
                if text:
                    add('reasoning',text,identity=part.get('id'))
            elif part.get('type')=='tool_use':
                inputs=part.get('input') or {}
                name=part.get('name','Tool')
                add('command' if name=='Bash' else 'tool',name+' · '+str(inputs.get('command') or inputs.get('file_path') or inputs.get('path') or ''),'working',json.dumps(inputs,indent=2),part.get('id'))
            elif part.get('type')=='tool_result':
                content=part.get('content','')
                if isinstance(content,list):
                    content='\n'.join(c.get('text','') for c in content if isinstance(c,dict))
                add('result','Tool result','failed' if part.get('is_error') else 'completed',content,part.get('tool_use_id'))
    if kind=='message' and event.get('role')=='assistant':
        add('message',event.get('text',''),identity=event.get('id'))
    if kind=='result' and isinstance(event.get('result'),str):
        add('message',event['result'],identity='final-reply')
    if event.get('permission_denials'):
        tools=', '.join(sorted({str(d.get('tool_name','tool')) for d in event['permission_denials'] if isinstance(d,dict)}))
        add('permission','Permission needed: '+tools,'needs permission',json.dumps(event['permission_denials'],indent=2), 'permissions')
        result[-1]['requests']=[{'tool':d.get('tool_name','Tool'),'command':str((d.get('tool_input') or {}).get('command') or (d.get('tool_input') or {}).get('file_path') or '')[:1500]} for d in event['permission_denials'] if isinstance(d,dict)]
    if kind=='text':
        part=event.get('part') or {}
        if part.get('type') in ('thinking','reasoning'):
            text = reasoning_text(part.get('summary'))
            if text:
                add('reasoning',text,identity=part.get('id'))
        else:
            add('message',part.get('text',''),identity=part.get('id'))
    if kind in ('thinking','reasoning','thinking_delta','reasoning_delta'):
        part=event.get('part') or event
        text = reasoning_text(part.get('summary'))
        if text:
            add('reasoning',text,event.get('status',''),identity=part.get('id') or event.get('id'))
    if kind in ('tool_call','tool_use','tool_result'):
        part=event.get('part') or {}
        state=part.get('state') or {}
        name=part.get('tool') or event.get('toolName') or event.get('tool_name') or event.get('name') or 'Tool'
        inputs=state.get('input') or event.get('parameters') or event.get('input') or {}
        output=state.get('output') or event.get('output') or event.get('result') or json.dumps(inputs,indent=2)
        add('result' if kind=='tool_result' else 'tool',name,state.get('status') or ('completed' if kind=='tool_result' else 'working'),output,part.get('callID') or event.get('tool_call_id') or event.get('id'))
    if kind in ('error','turn.failed'):
        error=event.get('error') or event.get('message') or event.get('result') or 'Harness error'
        add('error',error,'failed',json.dumps(event,indent=2))
    return result

def read_events(path):
    if not path or not Path(path).is_file():
        return []
    path=Path(path)
    stat=path.stat()
    signature=(stat.st_mtime_ns,stat.st_size)
    cached=_cache.get(str(path))
    if cached and cached[0]==signature:
        return cached[1]
    merged=OrderedDict()
    with path.open(encoding='utf-8',errors='replace') as handle:
        for number,line in enumerate(handle):
            try:
                event=json.loads(line)
            except ValueError:
                continue
            for index,item in enumerate(normalize(event)):
                identity=item['event_id'] or f'line-{number}-{index}'
                if identity in merged and item['kind']=='result':
                    item={**merged[identity],'status':item['status'],'output':item['output']}
                merged[identity]=item
                if len(merged)>500:
                    merged.popitem(last=False)
    result=list(merged.values())
    if not result:
        # Earlier Rotation versions used Codex's human-readable transcript.
        # Recover only explicit assistant sections, never reasoning/tool output.
        codex_log=False
        collecting=False
        chunks=[]
        def finish_legacy():
            if chunks:
                text=''.join(chunks).strip()
                if text:
                    result.append({'kind':'message','text':text[:16000],'status':'','output':'','event_id':f'legacy-{len(result)}'})
                chunks.clear()
        with path.open(encoding='utf-8',errors='replace') as handle:
            for line in handle:
                if line.startswith('OpenAI Codex '):
                    codex_log=True
                if not codex_log:
                    continue
                if line.strip() in ('codex','exec','thinking','user','tokens used'):
                    finish_legacy()
                    collecting=line.strip()=='codex'
                elif collecting:
                    chunks.append(line)
        finish_legacy()
        result=result[-500:]
    _cache[str(path)]=(signature,result)
    while len(_cache)>64:
        _cache.popitem(last=False)
    return result
