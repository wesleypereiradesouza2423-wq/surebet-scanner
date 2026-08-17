import os, asyncio, json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text

DATABASE_URL=os.getenv('DATABASE_URL')
ODDS_API_KEY=os.getenv('ODDS_API_KEY')
SCAN_INTERVAL_SECONDS=int(os.getenv('SCAN_INTERVAL_SECONDS','300'))
MIN_ROI=float(os.getenv('MIN_ROI','0.50'))
MAX_SPORTS=int(os.getenv('MAX_SPORTS','30'))
SPORTS_PER_SCAN=int(os.getenv('SPORTS_PER_SCAN','3'))
REGIONS=os.getenv('REGIONS','eu,uk')
MAX_ODD_AGE_SECONDS=int(os.getenv('MAX_ODD_AGE_SECONDS','180'))
BOOKS=[x.strip() for x in os.getenv('BOOKS','Pinnacle,Betfair,Betano,Unibet,Betclic,William Hill,Bet Victor,Betsson,1xBet,Matchbook').split(',') if x.strip()]
PREFERRED_GROUPS={'Soccer','Tennis','Basketball','Baseball','Ice Hockey','Mixed Martial Arts','American Football','Rugby League','Rugby Union'}
templates=Jinja2Templates(directory='app/templates')

engine=None
DB_ERROR=None
LAST_SCAN_DIAG={}
ROTATION_INDEX=0

if DATABASE_URL:
    try:
        engine=create_engine(DATABASE_URL,pool_pre_ping=True)
    except Exception as e:
        DB_ERROR=repr(e)
        print('database url error:',DB_ERROR,flush=True)

def db_exec(sql,params=None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql),params or {})

def init_db():
    global DB_ERROR
    if not engine: return
    try:
        db_exec('''CREATE TABLE IF NOT EXISTS scans (id BIGSERIAL PRIMARY KEY, scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), events_count INTEGER NOT NULL DEFAULT 0, surebets_count INTEGER NOT NULL DEFAULT 0)''')
        db_exec('''CREATE TABLE IF NOT EXISTS surebets (id BIGSERIAL PRIMARY KEY, scan_id BIGINT REFERENCES scans(id) ON DELETE CASCADE, event_id TEXT, sport_key TEXT, sport_title TEXT, event_name TEXT, commence_time TIMESTAMPTZ, outcomes_count INTEGER, roi NUMERIC, implied_sum NUMERIC, profit_per_1000 NUMERIC, legs JSONB, revalidated BOOLEAN DEFAULT FALSE, recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        db_exec('CREATE INDEX IF NOT EXISTS idx_surebets_recorded_at ON surebets(recorded_at DESC)')
        db_exec('CREATE INDEX IF NOT EXISTS idx_surebets_roi ON surebets(roi DESC)')
        DB_ERROR=None
    except Exception as e:
        DB_ERROR=repr(e)
        print('database init error:',DB_ERROR,flush=True)

def is_selected_book(title):
    low=title.lower()
    return any(b.lower() in low for b in BOOKS)

def best_arb(event):
    now=datetime.now(timezone.utc); counts={}; markets=[]
    for book in event.get('bookmakers',[]):
        if not is_selected_book(book.get('title','')): continue
        market=next((m for m in book.get('markets',[]) if m.get('key')=='h2h'),None)
        if not market: continue
        outcomes=market.get('outcomes',[])
        if len(outcomes) not in (2,3): continue
        last_update=market.get('last_update') or book.get('last_update')
        if not last_update: continue
        try: age=(now-datetime.fromisoformat(last_update.replace('Z','+00:00'))).total_seconds()
        except Exception: continue
        if age>MAX_ODD_AGE_SECONDS: continue
        universe='|'.join(sorted(o['name'] for o in outcomes)); counts[universe]=counts.get(universe,0)+1
        markets.append((book,market,universe,int(max(0,age))))
    if not counts: return None
    universe=max(counts,key=counts.get); names=universe.split('|'); offers={n:[] for n in names}
    for book,market,uni,age in markets:
        if uni!=universe: continue
        for outcome in market.get('outcomes',[]):
            offers[outcome['name']].append({'name':outcome['name'],'odd':float(outcome['price']),'book':book['title'],'link':outcome.get('link') or market.get('link') or book.get('link'),'age_sec':age})
    if any(not offers[n] for n in names): return None
    combos=[[]]
    for name in names: combos=[c+[o] for c in combos for o in offers[name]]
    best=None
    for legs in combos:
        if len({l['book'] for l in legs})<2: continue
        inv=sum(1/l['odd'] for l in legs)
        if best is None or inv<best['inv']: best={'legs':legs,'inv':inv,'outcomes':len(names)}
    return best

async def api_get(client,url,params):
    r=await client.get(url,params=params,timeout=25)
    quota={k:r.headers.get(k) for k in ('x-requests-remaining','x-requests-used','x-requests-last') if r.headers.get(k) is not None}
    if r.status_code>=400:
        body=r.text[:500]
        raise RuntimeError(f'HTTP {r.status_code}: {body}')
    return r.json(),quota

async def revalidate_event(client,sport_key,event_id):
    try:
        data,_=await api_get(client,f'https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds',{'apiKey':ODDS_API_KEY,'regions':REGIONS,'markets':'h2h','oddsFormat':'decimal','dateFormat':'iso','includeLinks':'true','includeSids':'true'})
        return data
    except Exception:
        return None

def choose_sports(sports):
    global ROTATION_INDEX
    active=[s for s in sports if s.get('active') and not s.get('has_outrights')]
    active.sort(key=lambda s:(0 if s.get('group') in PREFERRED_GROUPS else 1,s.get('group',''),s.get('title','')))
    active=active[:MAX_SPORTS]
    if not active: return [],0
    n=min(SPORTS_PER_SCAN,len(active))
    chosen=[active[(ROTATION_INDEX+i)%len(active)] for i in range(n)]
    ROTATION_INDEX=(ROTATION_INDEX+n)%len(active)
    return chosen,len(active)

async def perform_scan():
    global LAST_SCAN_DIAG
    if not ODDS_API_KEY or not engine:
        LAST_SCAN_DIAG={'ok':False,'reason':'missing ODDS_API_KEY or DATABASE_URL','db_error':DB_ERROR}
        return LAST_SCAN_DIAG

    diag={'ok':True,'events':0,'surebets':0,'sports_available':0,'sports_scanned':[],'errors':[],'quota':{}}
    async with httpx.AsyncClient(headers={'User-Agent':'SurebetScanner/1.1'}) as client:
        try:
            sports,quota=await api_get(client,'https://api.the-odds-api.com/v4/sports/',{'apiKey':ODDS_API_KEY})
            diag['quota'].update(quota)
        except Exception as e:
            diag.update({'ok':False,'reason':'Falha ao carregar esportes','errors':[str(e)]})
            LAST_SCAN_DIAG=diag
            return diag

        chosen,total=choose_sports(sports)
        diag['sports_available']=total
        events_count=0; candidates=[]

        for sport in chosen:
            diag['sports_scanned'].append(sport.get('title',sport.get('key')))
            try:
                events,quota=await api_get(client,f"https://api.the-odds-api.com/v4/sports/{sport['key']}/odds/",{'apiKey':ODDS_API_KEY,'regions':REGIONS,'markets':'h2h','oddsFormat':'decimal','dateFormat':'iso','includeLinks':'true','includeSids':'true'})
                diag['quota'].update(quota)
            except Exception as e:
                diag['errors'].append(f"{sport.get('title')}: {e}")
                continue

            for ev in events:
                commence=ev.get('commence_time')
                if commence:
                    try:
                        if datetime.fromisoformat(commence.replace('Z','+00:00'))<=datetime.now(timezone.utc): continue
                    except Exception: pass
                events_count+=1
                arb=best_arb(ev)
                if not arb or arb['inv']>=1: continue
                if (1/arb['inv']-1)*100>=MIN_ROI: candidates.append((sport,ev))

        confirmed=[]
        for sport,ev in candidates:
            fresh=await revalidate_event(client,sport['key'],ev['id'])
            if not fresh: continue
            arb=best_arb(fresh)
            if not arb or arb['inv']>=1: continue
            roi=(1/arb['inv']-1)*100
            if roi<MIN_ROI: continue
            ret=1000/arb['inv']; legs=[]
            for leg in arb['legs']:
                l=dict(leg); l['stake_for_1000']=ret/l['odd']; legs.append(l)
            confirmed.append({'event_id':ev.get('id'),'sport_key':sport['key'],'sport_title':sport['title'],'event_name':f"{ev.get('home_team')} × {ev.get('away_team')}",'commence_time':ev.get('commence_time'),'outcomes_count':arb['outcomes'],'roi':roi,'implied_sum':arb['inv'],'profit_per_1000':ret-1000,'legs':legs})

        row=db_exec('INSERT INTO scans(events_count, surebets_count) VALUES (:e,:s) RETURNING id',{'e':events_count,'s':len(confirmed)})
        if row is None:
            diag.update({'ok':False,'reason':'database unavailable','db_error':DB_ERROR})
            LAST_SCAN_DIAG=diag
            return diag
        scan_id=row.scalar_one()
        for x in confirmed:
            db_exec('''INSERT INTO surebets(scan_id,event_id,sport_key,sport_title,event_name,commence_time,outcomes_count,roi,implied_sum,profit_per_1000,legs,revalidated) VALUES (:scan_id,:event_id,:sport_key,:sport_title,:event_name,:commence_time,:outcomes_count,:roi,:implied_sum,:profit_per_1000,CAST(:legs AS JSONB),TRUE)''',{**x,'scan_id':scan_id,'legs':json.dumps(x['legs'])})

        diag['events']=events_count; diag['surebets']=len(confirmed)
        if events_count==0 and diag['errors']:
            diag['ok']=False; diag['reason']='Nenhum evento retornado; veja errors/quota'
        LAST_SCAN_DIAG=diag
        return diag

async def collector_loop():
    await asyncio.sleep(8)
    while True:
        try: await perform_scan()
        except Exception as e: print('collector error:',repr(e),flush=True)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app):
    init_db(); task=asyncio.create_task(collector_loop()); yield; task.cancel()

app=FastAPI(lifespan=lifespan)

@app.get('/',response_class=HTMLResponse)
def home(request:Request): return templates.TemplateResponse('index.html',{'request':request})

@app.get('/health')
def health():
    db_ok=False; db_error=DB_ERROR
    if engine:
        try:
            with engine.connect() as conn: conn.execute(text('SELECT 1'))
            db_ok=True
        except Exception as e: db_error=repr(e)
    return {'status':'ok','db':db_ok,'api_key':bool(ODDS_API_KEY),'db_error':db_error}

@app.post('/api/scan')
async def manual_scan(): return await perform_scan()

@app.get('/scan-now')
async def scan_now():
    result=await perform_scan()
    msg=f"Scan concluído: {result.get('events',0)} eventos, {result.get('surebets',0)} surebets"
    if not result.get('ok',False): msg=f"Scan com erro: {result.get('reason','ver diagnóstico')}"
    return RedirectResponse(url='/?scanmsg='+msg.replace(' ','%20'),status_code=303)

@app.get('/api/debug')
def debug(): return LAST_SCAN_DIAG

@app.get('/api/latest')
def latest(limit:int=50):
    if not engine: return []
    try:
        with engine.begin() as conn:
            rows=conn.execute(text("SELECT id,sport_title,event_name,commence_time,outcomes_count,roi::float,profit_per_1000::float,legs,recorded_at FROM surebets WHERE recorded_at > NOW() - INTERVAL '24 hours' ORDER BY recorded_at DESC,roi DESC LIMIT :limit"),{'limit':min(limit,200)}).mappings().all()
        return [dict(r) for r in rows]
    except Exception: return []

@app.get('/api/stats')
def stats():
    if not engine: return {}
    try:
        with engine.begin() as conn:
            s=conn.execute(text("SELECT COUNT(*) AS surebets_24h,COALESCE(MAX(roi),0)::float AS best_roi_24h,COALESCE(AVG(roi),0)::float AS avg_roi_24h,COALESCE(MAX(profit_per_1000),0)::float AS best_profit_1000 FROM surebets WHERE recorded_at > NOW() - INTERVAL '24 hours'" )).mappings().one()
            scan=conn.execute(text('SELECT scanned_at,events_count,surebets_count FROM scans ORDER BY id DESC LIMIT 1')).mappings().first()
        return {'stats':dict(s),'last_scan':dict(scan) if scan else None}
    except Exception as e: return {'error':repr(e)}
