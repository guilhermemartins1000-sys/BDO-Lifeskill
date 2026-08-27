import json
import os
import time
import urllib.parse
import urllib.request
import re
import base64
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HOST="127.0.0.1"
PORT=int(os.environ.get("BDO_PORT","8787"))
ROOT=os.path.dirname(os.path.abspath(__file__))
API_V2="https://api.arsha.io/v2/sa"
API_V1="https://api.arsha.io/v1/sa"
CACHE={}
TTL=35
LAST_GOOD={}

# Controle de vida do navegador: o servidor permanece ligado enquanto
# pelo menos uma aba do site envia heartbeats. Quando todas as abas
# são fechadas, o servidor se encerra automaticamente após um pequeno
# período de segurança. Isso evita deixar o processo Python rodando.
HEARTBEATS={}
HEARTBEAT_LOCK=threading.Lock()
SERVER_STARTED=time.time()
HEARTBEAT_TIMEOUT=30


def register_heartbeat(client_id):
    if not client_id:
        return
    now=time.time()
    with HEARTBEAT_LOCK:
        HEARTBEATS[str(client_id)]=now


def heartbeat_watchdog(httpd):
    # Dá tempo para o navegador abrir antes de considerar que não existe cliente.
    while True:
        time.sleep(5)
        now=time.time()
        with HEARTBEAT_LOCK:
            stale=[cid for cid,t in HEARTBEATS.items() if now-t>HEARTBEAT_TIMEOUT]
            for cid in stale:
                HEARTBEATS.pop(cid,None)
            active=bool(HEARTBEATS)
        if now-SERVER_STARTED > HEARTBEAT_TIMEOUT and not active:
            print("Nenhuma aba do site está conectada. Encerrando o servidor...")
            try:
                httpd.shutdown()
            except Exception:
                pass
            break


def http_json(url, timeout=12):
    last=None
    for attempt in range(3):
        try:
            req=urllib.request.Request(url,headers={
                "User-Agent":"Mozilla/5.0 BDO-Harmonia-Edania/2.0",
                "Accept":"application/json",
                "Accept-Encoding":"gzip"
            })
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read()
                if getattr(r,"headers",None) and r.headers.get("Content-Encoding")=="gzip":
                    import gzip
                    raw=gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last=e
            time.sleep(0.4*(attempt+1))
    raise last


def cached(key, fn, force=False):
    now=time.time()
    if not force and key in CACHE and now-CACHE[key][0] < TTL:
        return CACHE[key][1]
    val=fn()
    CACHE[key]=(now,val)
    return val


def parse_market_number(text):
    text=str(text).strip().replace(",","").replace(".","").upper()
    m=re.match(r"^([0-9]+)([KMB]?)$",text)
    if not m:return None
    return int(round(float(m.group(1))*{"":1,"K":1000,"M":1000000,"B":1000000000}[m.group(2)]))


def bdocodex_fallback(item_id):
    """Price fallback from BDO Codex SA market snapshot.
    It is used only when Arsha cannot return an item. The stock shown by this
    source is explicitly NOT treated as live/confirmed stock.
    """
    url=f'https://bdocodex.com/pt/item/{int(item_id)}/'
    try:
        req=urllib.request.Request(url,headers={
            'User-Agent':'Mozilla/5.0 BDO-Harmonia-Edania/3.0',
            'Accept':'text/html,application/xhtml+xml'
        })
        with urllib.request.urlopen(req,timeout=10) as r:
            text=r.read().decode('utf-8','ignore')
        # BDO Codex exposes the SA market snapshot in the item page.
        m=re.search(r'Preço de mercado no jogo:\s*SA:\s*([0-9.,]+)\s*Em estoque:\s*([0-9.,]+)',text,re.I|re.S)
        if not m:
            m=re.search(r'SA:\s*([0-9.,]+)\s*Em estoque:\s*([0-9.,]+)',text,re.I|re.S)
        if m:
            price=parse_market_number(m.group(1)); stock=parse_market_number(m.group(2))
            if price and price>0:
                return {'id':int(item_id),'sid':0,'basePrice':price,
                        'currentStock':stock,'stockKnown':False,
                        'totalTrades':None,'priceMin':price,'priceMax':price,
                        'lastSoldPrice':price,'lastSoldTime':0,
                        'source':'BDO Codex SA • snapshot de preço'}
    except Exception:
        pass
    return None

def bdolytics_fallback(item_id):
    # Price-only fallback. IMPORTANT: stockKnown=False so the UI never turns
    # a missing stock field into a false "SEM ESTOQUE".
    urls=[
        f"https://bdolytics.com/en/SA/market/central-market/item/{item_id}",
        f"https://bdolytics.com/en/SA/market/item/{item_id}"
    ]
    for url in urls:
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 BDO-Harmonia-Edania","Accept":"text/html"})
            with urllib.request.urlopen(req,timeout=10) as r:
                text=r.read().decode("utf-8","ignore")
            # Keep this conservative; never invent stock from page markup.
            candidates=re.findall(r'(?i)(?:lastSoldPrice|lastPrice|price)[^0-9]{0,50}([0-9][0-9,.]{2,})',text)
            for c in candidates:
                price=parse_market_number(c)
                if price and price>0:
                    return {"id":int(item_id),"sid":0,"basePrice":price,
                            "currentStock":None,"stockKnown":False,"totalTrades":None,
                            "priceMin":price,"priceMax":price,"lastSoldPrice":price,
                            "lastSoldTime":0,"source":"BDOLytics SA • preço"}
        except Exception:
            pass
    return None


def v2_rows(data):
    if isinstance(data,dict):
        # Some aliases return a single object; normalize it to a list.
        data=[data]
    return data if isinstance(data,list) else []


def post_json(url, payload, timeout=18):
    req=urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST', headers={
        'User-Agent':'Mozilla/5.0 BDO-Harmonia-Edania/3.0',
        'Accept':'application/json',
        'Content-Type':'application/json',
        'Accept-Encoding':'gzip'
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw=r.read()
        if getattr(r,'headers',None) and r.headers.get('Content-Encoding')=='gzip':
            import gzip
            raw=gzip.decompress(raw)
        return json.loads(raw.decode('utf-8'))


def parse_v1_result(data, wanted):
    """Parse both singular and plural Arsha V1 /item responses.

    POST returns a JSON array for multiple IDs. The base market row is also
    identified by minEnhance=0; maxEnhance is not necessarily 0 (often 7).
    """
    out={}
    if isinstance(data,dict):
        rows=[data]
    elif isinstance(data,list):
        rows=[]
        for x in data:
            if isinstance(x,dict): rows.append(x)
            elif isinstance(x,list): rows.extend(y for y in x if isinstance(y,dict))
    else:
        rows=[]
    for obj in rows:
        msg=str(obj.get('resultMsg',''))
        for row in msg.split('|'):
            a=row.split('-')
            if len(a)<10: continue
            try:
                iid=int(a[0]); mn=int(a[1]); mx=int(a[2])
                if iid not in wanted or mn!=0 or iid in out: continue
                out[iid]={
                    'id':iid,'sid':0,'basePrice':int(a[3]),
                    'currentStock':int(a[4]),'stockKnown':True,
                    'totalTrades':int(a[5]),'priceMin':int(a[6]),
                    'priceMax':int(a[7]),'lastSoldPrice':int(a[8]),
                    'lastSoldTime':int(a[9]),'source':'Arsha SA v1'
                }
            except Exception:
                continue
    return out


def parse_v2_result(data, wanted):
    out={}
    # V2 may return a flat array or an array of arrays for multiple IDs.
    stack=[]
    if isinstance(data,list):
        for x in data:
            if isinstance(x,list): stack.extend(x)
            else: stack.append(x)
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
        except Exception:
            continue
    return out


def bdolytics_fallback(item_id):
    # Price-only fallback. Never invent stock.
    urls=[
        f'https://bdolytics.com/en/SA/market/central-market/item/{item_id}',
        f'https://bdolytics.com/en/SA/market/item/{item_id}'
    ]
    for url in urls:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 BDO-Harmonia-Edania','Accept':'text/html'})
            with urllib.request.urlopen(req,timeout=8) as r:
                text=r.read().decode('utf-8','ignore')
            candidates=re.findall(r'(?i)(?:lastSoldPrice|lastPrice|price)[^0-9]{0,80}([0-9][0-9,.]{2,})',text)
            for c in candidates:
                price=parse_market_number(c)
                if price and price>0:
                    return {'id':int(item_id),'sid':0,'basePrice':price,'currentStock':None,'stockKnown':False,
                            'totalTrades':None,'priceMin':price,'priceMax':price,'lastSoldPrice':price,
                            'lastSoldTime':0,'source':'BDOLytics SA • preço'}
        except Exception:
            pass
    return None


def get_items(ids, force=False):
    """Batch market snapshot. The calculator is fed independently from the dashboard.

    Important change: Arsha V1/V2 batch endpoints are used instead of one HTTP request
    per item. The public V1 API explicitly supports POST /item with an array of IDs and
    returns amountListed, totalTrades and prices in one response. This avoids the
    59/79, 65/83 partial-load problem caused by dozens of individual requests.
    """
    clean=[]
    for x in ids:
        try:
            n=int(x)
            if n>0 and n not in clean: clean.append(n)
        except Exception: pass
    if not clean: return []
    key='prices:'+','.join(map(str,sorted(clean)))

    def load():
        result={}
        # 1) V1 batch — primary price + stock source.
        try:
            d=post_json(f'{API_V1}/item', clean, timeout=25)
            result.update(parse_v1_result(d,set(clean)))
        except Exception:
            pass

        # 2) V2 batch — fills anything V1 missed.
        missing=[n for n in clean if n not in result]
        if missing:
            try:
                d=post_json(f'{API_V2}/item?lang=en', missing, timeout=25)
                result.update(parse_v2_result(d,set(missing)))
            except Exception:
                # Some deployments expose V2 GET more reliably; one batched GET
                # with comma-separated IDs is much cheaper than 83 individual calls.
                try:
                    d=http_json(f'{API_V2}/item?id={urllib.parse.quote(",".join(map(str,missing)))}&lang=en',timeout=25)
                    result.update(parse_v2_result(d,set(missing)))
                except Exception: pass

        # 3) Price fallback from BDO Codex. It prevents a missing Arsha
        # response from turning a previously valid recipe price into 0.
        missing=[n for n in clean if n not in result]
        if missing:
            with ThreadPoolExecutor(max_workers=min(8,len(missing))) as ex:
                futures={ex.submit(bdocodex_fallback,n):n for n in missing}
                for f in as_completed(futures):
                    try:
                        x=f.result()
                        if x: result[int(x['id'])]=x
                    except Exception: pass

        # 4) BDOLytics is the final price-only fallback.
        missing=[n for n in clean if n not in result]
        if missing:
            with ThreadPoolExecutor(max_workers=min(8,len(missing))) as ex:
                futures={ex.submit(bdolytics_fallback,n):n for n in missing}
                for f in as_completed(futures):
                    try:
                        x=f.result()
                        if x: result[int(x['id'])]=x
                    except Exception: pass

        # 5) Last-good cache. A temporary failure never wipes a working price.
        for n in clean:
            if n in result:
                LAST_GOOD[n]=dict(result[n])
            elif n in LAST_GOOD:
                x=dict(LAST_GOOD[n]); x['source']='CACHE • último dado válido'; result[n]=x
        return [result[n] for n in clean if n in result]

    return cached(key,load,force=force)


def get_live_market_map(force=False):
    """Live-stock source used only by the Dashboard.
    /v2/sa/market is a convenience endpoint that returns items currently listed.
    This is intentionally isolated from /api/prices so dashboard changes cannot
    break the recipe calculator.
    """
    key='dashboard:market'
    def load():
        try:
            data=http_json(f'{API_V2}/market',timeout=30)
            rows=data if isinstance(data,list) else []
            out={}
            for x in rows:
                if not isinstance(x,dict): continue
                try:
                    iid=int(x.get('id') or 0)
                    if iid>0:
                        out[iid]={
                            'currentStock':int(x.get('currentStock') or 0),
                            'totalTrades':int(x.get('totalTrades') or 0),
                            'stockKnown':True,
                            'source':'Arsha SA v2 /market'
                        }
                except Exception: pass
            return out
        except Exception:
            return {}
    # Keep dashboard stock fresh but avoid hammering the public endpoint.
    old_ttl=TTL
    try:
        return cached(key,load,force=force)
    finally:
        pass


def get_dashboard(ids, force=False):
    """Dashboard market snapshot using the documented /item endpoints.

    The previous version depended on the convenience /market endpoint and then
    merged it with /item. That created a single point of failure: if /market
    was unavailable, the dashboard could lose its live stock view even though
    /item was still returning price + currentStock. The Arsha API documents
    /v2/:region/item as returning basePrice, currentStock, totalTrades and
    priceMin/priceMax/lastSoldPrice, including multiple IDs in one request.
    We therefore use the same batch snapshot as the calculator and preserve
    the stock exactly as returned by the market API.
    """
    items=get_items(ids,force=force)
    merged=[]
    live_ok=False
    for item in items:
        x=dict(item)
        if x.get('stockKnown') is True:
            x['stockSource']=str(x.get('source') or 'Arsha SA')
            live_ok=True
        else:
            x['stockSource']=str(x.get('source') or 'estoque não confirmado')
        merged.append(x)
    return merged, live_ok

def get_history(item_id):
    n=int(item_id); key="history:"+str(n)
    def load():
        try:
            d=http_json(f"{API_V2}/history?id={n}&sid=0&lang=pt",timeout=12)
            if isinstance(d,dict) and isinstance(d.get("history"),dict):
                vals=[int(v) for _,v in sorted(d["history"].items()) if int(v)>0]
                return vals[-7:]
        except Exception: pass
        try:
            d=http_json(f"{API_V1}/history?id={n}&sid=0",timeout=10)
            msg=str(d.get("resultMsg","")); vals=[]
            for v in msg.split("-"):
                try:
                    iv=int(v)
                    if iv>0: vals.append(iv)
                except Exception: pass
            return vals[-7:]
        except Exception: return []
    return cached(key,load)


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        p=urllib.parse.urlparse(self.path)
        if p.path=="/api/warehouse-grid":
            try:
                length=int(self.headers.get("Content-Length","0"))
                if length<=0 or length>15*1024*1024: raise ValueError("Imagem ausente ou maior que 15 MB")
                raw=self.rfile.read(length)
                from PIL import Image, ImageOps, ImageEnhance
                import cv2, numpy as np, base64, io, re, pytesseract
                im=Image.open(io.BytesIO(raw)).convert("RGB")
                arr=np.array(im); gray=cv2.cvtColor(arr,cv2.COLOR_RGB2GRAY)
                # Detect repeated slot borders using strong vertical/horizontal transitions.
                def borders(axis):
                    if axis=='x': d=np.abs(np.diff(gray.astype(np.int16),axis=1)); score=(d>35).sum(axis=0); limit=gray.shape[0]*0.70
                
                def get_positions_x():
                    d=np.abs(np.diff(gray.astype(np.int16),axis=1)); sc=(d>35).sum(axis=0); cand=[i for i,v in enumerate(sc) if v>gray.shape[0]*0.70]
                    return cand
                def get_positions_y():
                    d=np.abs(np.diff(gray.astype(np.int16),axis=0)); sc=(d>35).sum(axis=1); cand=[i for i,v in enumerate(sc) if v>gray.shape[1]*0.70]
                    return cand
                def cluster(vals):
                    out=[]
                    for v in vals:
                        if not out or v>out[-1][-1]+1: out.append([v])
                        else: out[-1].append(v)
                    return [int(round(sum(c)/len(c))) for c in out]
                def choose(seq, target_min=6, target_max=16):
                    # Slot borders in BDO are nearly periodic. Search for the best
                    # arithmetic progression instead of being confused by icon edges.
                    best=[]
                    for step10 in range(300,401):  # 30.0 .. 40.0 px
                        step=step10/10.0
                        for start in seq:
                            cur=[]; k=0
                            while k<target_max:
                                expected=start+k*step
                                cand=min(seq,key=lambda v:abs(v-expected)) if seq else None
                                if cand is None or abs(cand-expected)>5.5 or (cur and cand<=cur[-1]): break
                                if not cur or cand!=cur[-1]: cur.append(cand)
                                k+=1
                            if len(cur)>len(best): best=cur
                    if len(best)>=target_min:return best
                    return best
                xs=choose(cluster(get_positions_x())); ys=choose(cluster(get_positions_y()))

                # Fallback robusto para capturas em escala diferente ou com partes da UI
                # do jogo ao redor da grade. O detector antigo exigia linhas que atravessassem
                # 70% da imagem e por isso falhava em screenshots normais do BDO.
                def periodic_positions(proj, lo=24, hi=80, min_lines=4):
                    proj=np.asarray(proj,dtype=np.float32)
                    if len(proj)<80: return []
                    # Remove o fundo lento e conserva picos de borda.
                    base=cv2.GaussianBlur(proj.reshape(1,-1),(0,0),5).reshape(-1)
                    sig=np.maximum(0,proj-base)
                    # Também considera bordas escuras/claras fortes.
                    sig=(sig-sig.mean())/(sig.std()+1e-6)
                    best=None
                    for step in np.linspace(lo,hi,113):
                        for phase in np.linspace(0,step,12,endpoint=False):
                            pos=[]; k=0
                            while phase+k*step < len(sig):
                                e=phase+k*step
                                a=max(0,int(round(e-3))); b=min(len(sig),int(round(e+4)))
                                if b<=a: break
                                j=a+int(np.argmax(sig[a:b])); val=float(sig[j])
                                if val>0.35: pos.append(j)
                                k+=1
                            if len(pos)>=min_lines:
                                dif=np.diff(pos)
                                med=float(np.median(dif)) if len(dif) else step
                                regular=float(np.mean(np.abs(dif-med))) if len(dif) else 999
                                score=len(pos)*3.0-regular*0.35+float(np.mean([max(0,float(sig[max(0,q-2):min(len(sig),q+3)].max())) for q in pos]))
                                if best is None or score>best[0]: best=(score,pos)
                    return best[1] if best else []

                if len(xs)<3 or len(ys)<3:
                    dx=np.abs(np.diff(gray.astype(np.int16),axis=1)); dy=np.abs(np.diff(gray.astype(np.int16),axis=0))
                    px=(dx>18).sum(axis=0); py=(dy>18).sum(axis=1)
                    xs2=periodic_positions(px); ys2=periodic_positions(py)
                    if len(xs)<3: xs=xs2
                    if len(ys)<3: ys=ys2

                # Último recurso: capturas que são praticamente só a janela 9x8 do armazém.
                # Usa a geometria do BDO sem exigir dimensões exatas; escala é calculada pela imagem.
                if len(xs)<3 or len(ys)<3:
                    h,w=gray.shape[:2]
                    candidates=[]
                    for cols in (8,9,10,12):
                        for rows in (6,7,8,9,10):
                            sx=w/cols; sy=h/rows
                            if 24<=sx<=80 and 24<=sy<=80 and abs(sx/sy-1)<0.22:
                                # pontuação pela energia de borda nas linhas previstas
                                sc=0.0
                                for c in range(1,cols):
                                    x=int(round(c*sx)); sc+=float(px[max(0,x-2):min(len(px),x+3)].max())
                                for r in range(1,rows):
                                    y=int(round(r*sy)); sc+=float(py[max(0,y-2):min(len(py),y+3)].max())
                                candidates.append((sc,cols,rows,sx,sy))
                    if candidates:
                        _,cols,rows,sx,sy=max(candidates)
                        xs=[int(round(c*sx)) for c in range(cols+1)]
                        ys=[int(round(r*sy)) for r in range(rows+1)]

                if len(xs)<3 or len(ys)<3:
                    raise ValueError(f"Grade não reconhecida ({gray.shape[1]}x{gray.shape[0]} px). Envie a janela do Armazém com os slots visíveis.")
                cells=[]
                for r in range(len(ys)-1):
                    for c in range(len(xs)-1):
                        x0,x1=xs[c],xs[c+1]; y0,y1=ys[r],ys[r+1]
                        if x1-x0<20 or y1-y0<20: continue
                        crop=im.crop((x0+2,y0+2,x1-2,y1-2))
                        ca=np.array(crop)
                        # Empty slot detection: very dark and low variance in upper area.
                        upper=ca[:max(1,int(ca.shape[0]*.72)),:]
                        if float(upper.mean())<38 and float(upper.std())<22: continue
                        # OCR only the bottom strip where BDO writes stack count.
                        bottom=crop.crop((0,int(crop.height*.66),crop.width,crop.height))
                        up=bottom.resize((bottom.width*6,bottom.height*6),Image.Resampling.LANCZOS)
                        up=ImageEnhance.Contrast(ImageOps.grayscale(up)).enhance(2.4)
                        txt=pytesseract.image_to_string(up,config='--psm 7 -c tessedit_char_whitelist=0123456789.,').strip()
                        m=re.search(r'\d[\d.,]*',txt); qty=0
                        if m:
                            rawq=m.group(0).replace('.','').replace(',','')
                            try: qty=int(rawq)
                            except: qty=0
                        buf=io.BytesIO();crop.save(buf,format='PNG',optimize=True)
                        cells.append({"row":r,"col":c,"qty":qty,"image":base64.b64encode(buf.getvalue()).decode('ascii')})
                out={"ok":True,"cols":len(xs)-1,"rows":len(ys)-1,"cells":cells}
                b=json.dumps(out,ensure_ascii=False).encode('utf-8');self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
            except Exception as e:
                b=json.dumps({"ok":False,"error":str(e)},ensure_ascii=False).encode('utf-8');self.send_response(400);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if p.path=="/api/ocr-warehouse":
            try:
                length=int(self.headers.get("Content-Length","0"))
                if length<=0 or length>15*1024*1024:
                    raise ValueError("Imagem ausente ou maior que 15 MB")
                raw=self.rfile.read(length)
                data=json.loads(raw.decode("utf-8"))
                image=data.get("image","")
                if "," in image:
                    image=image.split(",",1)[1]
                img=base64.b64decode(image)
                from PIL import Image, ImageOps, ImageEnhance, ImageFilter
                import pytesseract
                im=Image.open(io.BytesIO(img)).convert("RGB")
                # Aumenta a resolução e melhora contraste para capturas do BDO.
                scale=2 if max(im.size)<2400 else 1
                if scale>1: im=im.resize((im.width*scale,im.height*scale),Image.Resampling.LANCZOS)
                gray=ImageOps.grayscale(im)
                gray=ImageEnhance.Contrast(gray).enhance(1.8)
                gray=gray.filter(ImageFilter.SHARPEN)
                config='--oem 3 --psm 6'
                d=pytesseract.image_to_data(gray,lang='por+eng',config=config,output_type=pytesseract.Output.DICT)
                words=[]
                for i,t in enumerate(d.get('text',[])):
                    t=str(t).strip()
                    if not t: continue
                    try: conf=float(d['conf'][i])
                    except Exception: conf=-1
                    words.append({
                        'text':t,'conf':conf,
                        'left':int(d['left'][i]),'top':int(d['top'][i]),
                        'width':int(d['width'][i]),'height':int(d['height'][i]),
                        'block':int(d['block_num'][i]),'line':int(d['line_num'][i])
                    })
                lines={}
                for w in words:
                    key=(w['block'],w['line'])
                    lines.setdefault(key,[]).append(w)
                out=[]
                for ws in lines.values():
                    ws.sort(key=lambda x:x['left'])
                    out.append({
                        'text':' '.join(x['text'] for x in ws),
                        'words':ws,
                        'top':min(x['top'] for x in ws),
                        'left':min(x['left'] for x in ws)
                    })
                full='\n'.join(x['text'] for x in out)
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(json.dumps({'ok':True,'text':full,'lines':out},ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(json.dumps({'ok':False,'error':str(e)},ensure_ascii=False).encode('utf-8'))
            return
        self.send_error(404)

    def do_GET(self):
        p=urllib.parse.urlparse(self.path)
        if p.path=="/api/heartbeat":
            try:
                q=urllib.parse.parse_qs(p.query)
                client_id=q.get("client",[""])[0]
                register_heartbeat(client_id)
                body=b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Cache-Control","no-store")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500,str(e))
            return
        if p.path=="/api/icon":
            try:
                q=urllib.parse.parse_qs(p.query); item_id=int(q.get("id",[0])[0])
                if item_id<=0 or item_id>9999999: raise ValueError("ID inválido")
                urls=[
                    f"https://cdn.arsha.io/icons/{item_id}.png",
                    f"https://s1.pearlcdn.com/SA/TradeMarket/Common/img/BDO/item/{item_id}.png",
                    f"https://s1.pearlcdn.com/NAEU/TradeMarket/Common/img/BDO/item/{item_id}.png"
                ]
                last=None
                data=None
                for url in urls:
                    try:
                        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"})
                        with urllib.request.urlopen(req,timeout=10) as rr: data=rr.read()
                        if data and len(data)>100: break
                    except Exception as ex: last=ex
                if not data: raise last or ValueError("Ícone não encontrado")
                self.send_response(200);self.send_header("Content-Type","image/png");self.send_header("Cache-Control","public, max-age=86400");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data);return
            except Exception as e:
                self.send_error(404,str(e));return
        if p.path=="/api/warehouse-grid":
            # This endpoint is implemented in do_POST; GET is intentionally unsupported.
            self.send_error(405);return
        p=urllib.parse.urlparse(self.path)
        if p.path=="/api/item":
            try:
                q=urllib.parse.parse_qs(p.query)
                item_id=q.get("id",["0"])[0]
                force=q.get("refresh",["0"])[0]=="1"
                body=json.dumps(get_items([item_id],force=force),ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as e:
                body=json.dumps({"error":str(e)},ensure_ascii=False).encode(); self.send_response(502)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(body); return
        if p.path in ("/api/items","/api/prices"):
            try:
                q=urllib.parse.parse_qs(p.query)
                ids=q.get("ids",[""])[0].split(",")
                force=q.get("refresh",["0"])[0]=="1"
                body=json.dumps(get_items(ids,force=force),ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as e:
                body=json.dumps({"error":str(e)},ensure_ascii=False).encode(); self.send_response(502)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(body); return
        if p.path=="/api/dashboard":
            # Dashboard-only market snapshot. It has its own live-stock source
            # and cannot mutate the calculator's price state.
            try:
                q=urllib.parse.parse_qs(p.query)
                ids=[x for x in q.get("ids",[""])[0].split(",") if x]
                force=q.get("refresh",["0"])[0]=="1"
                items,live_ok=get_dashboard(ids,force=force)
                body=json.dumps({"items":items,"liveStock":live_ok},ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as e:
                body=json.dumps({"error":str(e)},ensure_ascii=False).encode(); self.send_response(502)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(body); return
        if p.path=="/api/history":
            try:
                q=urllib.parse.parse_qs(p.query)
                body=json.dumps(get_history(q.get("id",["0"])[0]),ensure_ascii=False).encode(); self.send_response(200)
            except Exception as e:
                body=json.dumps({"error":str(e)},ensure_ascii=False).encode(); self.send_response(502)
            self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(body); return
        # Always serve the HTML fresh. This prevents an older index.html from being
        # reused by the browser after site updates. API responses already use no-store.
        if p.path in ("/", "/index.html"):
            try:
                path=os.path.join(ROOT,"index.html")
                with open(path,"rb") as f: body=f.read()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma","no-cache")
                self.send_header("Expires","0")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            except Exception as e:
                self.send_error(500,str(e)); return
        super().do_GET()

class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1)
        super().server_bind()

if __name__=="__main__":
    os.chdir(ROOT)
    print("BDO Harmonia & Edania")
    print(f"Site: http://127.0.0.1:{PORT}/index.html")
    print("Preços: Arsha V1 / SA -> V2 -> BDOLytics preço -> cache")
    print("Estoque: somente Arsha V2/V1 é considerado confirmado.")
    print("Proxy ativo. Pode fechar com CTRL+C.")
    httpd=ReusableHTTPServer((HOST,PORT),Handler)
    threading.Thread(target=heartbeat_watchdog,args=(httpd,),daemon=True,name="BDO-Heartbeat-Watchdog").start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        print("Servidor BDO Harmonia encerrado.")
