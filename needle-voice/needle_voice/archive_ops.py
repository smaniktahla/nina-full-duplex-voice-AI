"""Needle archive domain: conservative local routing, read-only HTTP execution."""
from __future__ import annotations
import asyncio,json,os,re,time,urllib.request
from pathlib import Path
from .result import ToolResult,Status

def plan_search(text:str,explicit=False):
    low=text.lower()
    if re.search(r'\b(delete|remove|rename|move|edit|overwrite|upload|play|send)\b',low): return None
    if re.search(r'\b(compare|summarize|summarise|analyze|analyse|calculate|explain)\b',low): return None
    noun=r'\b(files?|documents?|workbooks?|spreadsheets?|pdfs?|photos?|pictures?|images?|songs?|music|albums?|videos?|nas|archive)\b'
    if not explicit and (not re.search(noun,low) or not re.search(r'\b(find|search|locate|look for|show|where)\b',low)): return None
    choices=[]
    for cat,pattern in [('documents',r'\b(documents?|workbooks?|spreadsheets?|pdfs?|contracts?)\b'),('pictures',r'\b(photos?|pictures?|images?)\b'),('music',r'\b(songs?|music|albums?)\b'),('videos',r'\b(videos?|movies?)\b')]:
        if re.search(pattern,low): choices.append(cat)
    cat=choices[0] if len(choices)==1 else 'all'
    # Only remove request scaffolding; retain content words, names and numbers.
    q=re.sub(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:find|search(?: for)?|locate|look for|show(?: me)?|where (?:is|are))\s+','',text,flags=re.I)
    q=re.sub(r'\b(?:on|in|from) (?:my |the )?(?:nas|archive|documents share|pictures share|music share|videos share)\b','',q,flags=re.I)
    q=re.sub(r'\b(?:my|the|a|an|that|those|some|about|containing|mentioning|taken|by|in|from|of|for)\b',' ',q,flags=re.I)
    q=re.sub(noun,' ',q,flags=re.I)
    q=' '.join(re.findall(r'[\w.-]+',q)).strip(' .-')
    if not q: return None
    return {'query':q,'category_filter':cat,'limit':5,'mode':'keyword'}

class ArchiveAgent:
    def __init__(self,endpoint=None,token_file=None):
        config_path=Path.home()/'.config/archive-search/client.json'
        try: config=json.loads(config_path.read_text())
        except (OSError,ValueError): config={}
        self.endpoint=(endpoint or os.getenv('ARCHIVE_SEARCH_URL') or config.get('url','http://127.0.0.1:8093')).rstrip('/')
        self.token_file=Path(token_file or os.getenv('ARCHIVE_TOKEN_FILE',str(Path.home()/'.config/archive-search/token')))

    async def handle(self,text,explicit=False):
        plan=plan_search(text,explicit)
        if not plan: return ToolResult(Status.UNSUPPORTED,reason='archive:not_simple_search',domain='archive')
        try:
            data=await asyncio.to_thread(self._search,plan)
            if not data['results']:
                return ToolResult(Status.NO_MATCH,speech='I found no matching files in the current NAS index. It may still be indexing.',reason='archive:no_match',domain='archive',telemetry={'query':plan,'results':data})
            names='; '.join(r['name'] for r in data['results'][:3])
            return ToolResult(Status.SUCCESS,speech=f"I found {data['total']} matching files. The first matches are {names}.",domain='archive',telemetry={'query':plan,'results':data})
        except Exception as e:
            return ToolResult(Status.UNAVAILABLE,speech='The NAS archive search is unavailable right now.',reason=type(e).__name__,domain='archive')

    def _search(self,plan):
        from urllib.parse import urlencode
        token=os.getenv('ARCHIVE_TOKEN','') or self.token_file.read_text().strip()
        params={'q':plan['query'],'category':plan['category_filter'],'limit':plan['limit']}
        req=urllib.request.Request(self.endpoint+'/api/search?'+urlencode(params),headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=5) as r: return json.load(r)
