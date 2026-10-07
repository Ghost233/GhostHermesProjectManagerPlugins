"""Synthetic original Codex archive peer. The protocol itself forbids execution writes."""
import json
import sys
from pathlib import Path
root=Path(sys.argv[1])
for line in sys.stdin:
    request=json.loads(line)
    with (root/'archive-wire.jsonl').open('a') as log:
        log.write(json.dumps(request)+'\n')
    method,params=request['method'],request.get('params',{})
    state=json.loads((root/'archive-peer.json').read_text())
    if method=='initialized':
        continue
    if method=='initialize':
        result={'userAgent':'codex-cli/0.160.1','codexHome':str(root/'synthetic-home'),'platformFamily':'unix','platformOs':'fixture'}
    elif method=='thread/read':
        result={'thread':state['thread']}
    elif method=='thread/turns/list':
        result=state['turns'].get(params.get('cursor',''),{'data':[],'nextCursor':None})
    elif method=='thread/items/list':
        result=state['items'][params['turnId']].get(params.get('cursor',''),{'data':[],'nextCursor':None})
    else:
        raise RuntimeError('Archive reader attempted execution: '+method)
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
