#!/usr/bin/env python3
"""Build a static SA market snapshot for BDO Lifeskill.

Runs in GitHub Actions. The browser only reads market.json, so GitHub Pages
never needs a Python server or a CORS proxy.
"""
from __future__ import annotations
import json, os, re, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT=os.path.dirname(os.path.abspath(__file__))
INDEX=os.path.join(ROOT,'index.html')
SNAP=os.path.join(ROOT,'market.json')
HIST=os.path.join(ROOT,'market_history.json')
V1='https://api.arsha.io/v1/sa'
V2='https://api.arsha.io/v2/sa'
UA='Mozilla/5.0 BDO-Lifeskill-Market/1.0'

NPC_OR_DERIVED={'Catalisador Mágico','Água Purificada','Garrafa Vazia','Sal','Açúcar'}


def http_json(url, method='GET', payload=None, timeout=30):
    data=None
    headers={'User-Agent':UA,'Accept':'application/json'}
    if payload is not None:
        data=json.dumps(payload).encode('utf-8')
        headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=data,method=method,headers=headers)
    with urllib.request.urlopen(req,timeout=timeout) as r:
        raw=r.read()
        return json.loads(raw.decode('utf-8'))


def _balanced_object(text,start):
    depth=0; in_str=False; esc=False; quote=''
    for i in range(start,len(text)):
        c=text[i]
        if in_str:
            if esc: esc=False
            elif c=='\\': esc=True
            elif c==quote: in_str=False
        else:
            if c in ('"',"'"): in_str=True; quote=c
            elif c=='{': depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return text[start:i+1],i+1
    raise ValueError('objeto JS não fechado')


def extract_data():
    text=open(INDEX,encoding='utf-8').read()
    start=text.index('const DATA=')+len('const DATA=')
    obj,end=_balanced_object(text,start)
    data=json.loads(obj)
    # The current site extends DATA.priceIds after the main DATA object.
    # Merge every explicit priceIds assignment so the online counter matches
    # the full dashboard catalog (not just the original 117 IDs).
    for m in re.finditer(r'Object\.assign\(DATA\.priceIds\|\|\{\},\s*\{',text):
        brace=text.find('{',m.start())
        try:
            block,_=_balanced_object(text,brace)
            data.setdefault('priceIds',{}).update(json.loads(block))
        except Exception:
            pass
    m=re.search(r'const STRATEGIC_IDS=\{',text)
    if m:
        try:
            block,_=_balanced_object(text,text.find('{',m.start()))
            data.setdefault('priceIds',{}).update(json.loads(block))
        except Exception:
            pass
    return data


def parse_v1(data,wanted):
    out={}
    msg=str(data.get('resultMsg','')) if isinstance(data,dict) else ''
    for row in msg.split('|'):
        a=row.split('-')
        if len(a)<10: continue
        try:
            iid=int(a[0]); mn=int(a[1]); mx=int(a[2])
            if iid not in wanted or mn!=0 or mx!=0: continue
            out[iid]={
                'id':iid,'sid':0,'basePrice':int(a[3]),
                'currentStock':int(a[4]),'stockKnown':True,
                'totalTrades':int(a[5]),'priceMin':int(a[6]),
                'priceMax':int(a[7]),'lastSoldPrice':int(a[8]),
                'lastSoldTime':int(a[9]),'source':'Arsha SA v1'
            }
        except Exception: pass
    return out


def parse_v2(data,wanted):
    out={}
    stack=[]
    if isinstance(data,list):
        for x in data:
            stack.extend(x if isinstance(x,list) else [x])
    elif isinstance(data,dict): stack=[data]
    for x in stack:
        if not isinstance(x,dict): continue
        try:
            iid=int(x.get('id') or 0); sid=int(x.get('sid') or 0)
            if iid not in wanted or sid!=0: continue
            stock=x.get('currentStock')
            out[iid]={
                'id':iid,'sid':0,'basePrice':int(x.get('basePrice') or 0),
                'currentStock':int(stock) if stock is not None else None,
                'stockKnown':stock is not None,
                'totalTrades':int(x.get('totalTrades') or 0),
                'priceMin':int(x.get('priceMin') or 0),
                'priceMax':int(x.get('priceMax') or 0),
                'lastSoldPrice':int(x.get('lastSoldPrice') or 0),
                'lastSoldTime':int(x.get('lastSoldTime') or 0),
                'source':'Arsha SA v2'
            }
        except Exception: pass
    return out


def fetch_items(ids):
    wanted=set(ids); result={}
    # Keep requests well below the size that caused partial responses before.
    chunks=[ids[i:i+50] for i in range(0,len(ids),50)]
    for chunk in chunks:
        try: result.update(parse_v1(http_json(f'{V1}/item','POST',chunk,35),set(chunk)))
        except Exception: pass
        missing=[x for x in chunk if x not in result]
        if missing:
            try: result.update(parse_v2(http_json(f'{V2}/item?lang=en','POST',missing,35),set(missing)))
            except Exception:
                try: result.update(parse_v2(http_json(f'{V2}/item?id={urllib.parse.quote(",".join(map(str,missing)))}&lang=en',timeout=35),set(missing)))
                except Exception: pass
    return result


def fetch_history(iid):
    try:
        d=http_json(f'{V2}/history?id={iid}&sid=0&lang=pt',timeout=15)
        if isinstance(d,dict) and isinstance(d.get('history'),dict):
            vals=[int(v) for _,v in sorted(d['history'].items()) if int(v)>0]
            return vals[-7:]
    except Exception: pass
    try:
        d=http_json(f'{V1}/history?id={iid}&sid=0',timeout=12)
        vals=[]
        for v in str(d.get('resultMsg','')).split('-'):
            try:
                n=int(v)
                if n>0: vals.append(n)
            except Exception: pass
        return vals[-7:]
    except Exception: return []


def main():
    data=extract_data(); price_ids=data.get('priceIds',{})
    market_names=[n for n in price_ids if n not in NPC_OR_DERIVED]
    ids=[int(price_ids[n]) for n in market_names if str(price_ids[n]).isdigit()]
    by_id=fetch_items(ids)

    # History is intentionally focused on the items the Dashboard analyzes:
    # all fármacos, their required elixirs, perfumes and strategic market IDs.
    important=set()
    important.update(data.get('drugs',{}).keys())
    important.update(data.get('perfumes',{}).keys())
    for _,definition in data.get('drugs',{}).items():
        for item,_qty in definition[1]: important.add(item)
    for item in ['Fármaco da Harmonia','Fármaco da Harmonia - Edania','Perfume da Perseverança','Perfume do Desejo']:
        important.add(item)
    important={n for n in important if n in price_ids and n not in NPC_OR_DERIVED}
    # Include strategic IDs when their names are explicitly represented by the
    # STRATEGIC_IDS block in the HTML.
    text=open(INDEX,encoding='utf-8').read()
    m=re.search(r'const STRATEGIC_IDS=\{(.*?)\};',text,re.S)
    if m:
        for n in re.findall(r'"([^"]+)"\s*:\s*(\d+)',m.group(1)):
            if n[0] in price_ids: important.add(n[0])
    history={}
    important_ids=[int(price_ids[n]) for n in important if str(price_ids[n]).isdigit()]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures={ex.submit(fetch_history,i):i for i in important_ids}
        for f in as_completed(futures):
            iid=futures[f]
            try:
                vals=f.result()
                if vals: history[str(iid)]=vals
            except Exception: pass

    # Persistent demand history survives each scheduled workflow run.
    try:
        old=json.load(open(HIST,encoding='utf-8'))
    except Exception:
        old={}
    snapshots=old.get('snapshots',{})
    sales=old.get('salesHistory',{})
    today=datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now_ms=int(time.time()*1000)
    for name in market_names:
        iid=int(price_ids[name]); p=by_id.get(iid)
        if not p: continue
        trades=int(p.get('totalTrades') or 0)
        prev=snapshots.get(name,{})
        prev_trades=int(prev.get('trades') or 0)
        if trades>0 and prev_trades>0 and trades>=prev_trades:
            delta=trades-prev_trades
            if delta>0:
                arr=sales.setdefault(name,[])
                row=next((x for x in arr if x.get('date')==today),None)
                if row: row['delta']=int(row.get('delta') or 0)+delta
                else: arr.append({'date':today,'delta':delta})
                sales[name]=[x for x in arr if x.get('date','') >= datetime.fromtimestamp(time.time()-45*86400,timezone.utc).strftime('%Y-%m-%d')]
        snapshots[name]={'trades':trades,'timestamp':now_ms}

    # Do not carry bogus zeros into the online UI. Keep only real API rows.
    items=list(by_id.values())
    generated=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    payload={
        'version':1,
        'source':'GitHub Actions • Arsha SA',
        'generatedAt':generated,
        'items':items,
        'history':history,
        'salesHistory':sales,
        'salesSnapshot':snapshots
    }
    with open(SNAP,'w',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False,separators=(',',':'))
    with open(HIST,'w',encoding='utf-8') as f: json.dump({'snapshots':snapshots,'salesHistory':sales,'updatedAt':generated},f,ensure_ascii=False,separators=(',',':'))
    print(f'Gerado market.json: {len(items)}/{len(ids)} itens; histórico: {len(history)} itens; {generated}')

if __name__=='__main__': main()
