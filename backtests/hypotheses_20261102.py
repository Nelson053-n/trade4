"""Парные исследования ST8/ST9. Только локальные файлы и публичный ISS, без брокера.
Запуск: .venv/bin/python backtests/hypotheses_20261102.py --prepare / --run
"""
from __future__ import annotations
import argparse, concurrent.futures, datetime as dt, hashlib, json, math, pickle, sys, urllib.request
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.st9.engine import St9Engine, Bar

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'out/hypotheses_20261102'
DATA = OUT / 'data'
OLD = Path('/tmp/claude-1000/-home-nel-trade4/150c05c3-da1b-4a05-95a5-0c758cd13ef8/scratchpad')
FC = OLD / 'fable_edge_futures/cache'
EC = OLD / 'fable_edge_equity/data'
PV = {'USDRUBF':1000., 'IMOEXF':10.}
TICKERS = ['SELG','LSNGP','LKOH','NVTK','ALRS']
END = '2026-10-29'

def read_json(url):
    with urllib.request.urlopen(url,timeout=35) as r:
        return json.load(r)

def frame(rows):
    d = pd.DataFrame(rows,columns=['ts','o','h','l','c','v'])
    d.index = pd.DatetimeIndex([dt.datetime.fromtimestamp(x/1000) for x in d.pop('ts')]) # восстановление naive ISS-строк в исходной TZ хоста
    return d[~d.index.duplicated()].sort_index().loc[:END]

def prepare():
    DATA.mkdir(parents=True,exist_ok=True)
    for sec in PV:
        d=frame(pickle.load(open(FC/f'candles_{sec}_60.pkl','rb')))
        d.to_csv(DATA/f'{sec}_60.csv',index_label='time')
        rates=pickle.load(open(FC/f'swaps_{sec}.pkl','rb'))
        pd.DataFrame([{'date':date,'swap':r.get('SWAPRATE'),'settle':r.get('SETTLEPRICE')} for date,r in rates.items()]).to_csv(DATA/f'{sec}_swap.csv',index=False)
    for tk in TICKERS:
        for pre in ['d','sl']:
            (DATA/f'{pre}_{tk}.csv').write_bytes((EC/f'{pre}_{tk}.csv').read_bytes())
    (DATA/'index_IMOEX.csv').write_bytes((EC/'index_IMOEX.csv').read_bytes())
    def fetch_contract(sec):
        if (DATA/f'{sec}_spec.json').exists() and (DATA/f'{sec}_{60 if sec.startswith(('Si','MX')) else 24}.csv').exists():return
        base=f'https://iss.moex.com/iss/engines/futures/markets/forts/securities/{sec}'
        meta=read_json(base+'.json?iss.meta=off')['securities']
        rows=[dict(zip(meta['columns'],r)) for r in meta['data']]
        if rows:
            m=rows[0]
            metaout={k:m.get(k) for k in ['SECID','MINSTEP','STEPPRICE','LOTVOLUME','LASTTRADEDATE']}
            metaout['point_value']=m['STEPPRICE']/m['MINSTEP']
        else:
            desc=read_json(f'https://iss.moex.com/iss/securities/{sec}.json?iss.meta=off')['description']
            m={r[0]:r[2] for r in desc['data']}
            if m.get('UNIT') not in ('в рублях за лот','В RUB за лот','RUB','В пунктах'):raise ValueError('не подтверждены единицы '+sec)
            if m.get('UNIT')=='В пунктах' and not sec.startswith('MX'):raise ValueError('неизвестна стоимость пункта '+sec)
            metaout={'SECID':sec,'LASTTRADEDATE':m['LSTTRADE'],'point_value':1.,'unit_source':m['UNIT']}

        (DATA/f'{sec}_spec.json').write_text(json.dumps(metaout))
        interval=60 if sec.startswith(('Si','MX')) else 24
        start=0;result=[]
        while True:
            c=read_json(base+f'/candles.json?iss.meta=off&interval={interval}&from=2023-01-01&till={END}&start={start}')['candles']
            result.extend(dict(zip(c['columns'],r)) for r in c['data'])
            if len(c['data'])<500:break
            start+=len(c['data'])
        d=pd.DataFrame(result)
        if d.empty:raise ValueError('нет свечей '+sec)
        d=d.rename(columns={'begin':'time','open':'o','high':'h','low':'l','close':'c','volume':'v'})
        d[['time','o','h','l','c','v']].to_csv(DATA/f'{sec}_{interval}.csv',index=False)
        print('downloaded',sec,len(d),flush=True)
    # Для маршрутизации используем исходные отдельные серии, без склейки по будущим объёмам.
    for prefix in ['Si','MX']:
        for suf in ['Z5','H6','M6','U6','Z6']:
            sec=prefix+suf
            frame(pickle.load(open(FC/f'candles_{sec}_60.pkl','rb'))).to_csv(DATA/f'{sec}_60.csv',index_label='time')
    contracts=[p+q+str(y) for p in ['LK','AL'] for y in [4,5,6] for q in ['H','M','U','Z']]
    errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(fetch_contract,s):s for s in contracts+['SiZ5','SiH6','SiM6','SiU6','SiZ6','MXZ5','MXH6','MXM6','MXU6','MXZ6']}
        for f in concurrent.futures.as_completed(futures):
            try:f.result()
            except Exception as e:errors.append({'contract':futures[f],'error':str(e)})
    (OUT/'download_errors.json').write_text(json.dumps(errors,ensure_ascii=False,indent=2))
    manifest={f.name:{'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'bytes':f.stat().st_size} for f in DATA.iterdir()}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('PREPARED',len(manifest),'errors',len(errors),flush=True)

def load(sec,interval=60):
    d=pd.read_csv(DATA/f'{sec}_{interval}.csv',parse_dates=['time']).set_index('time').sort_index()
    return d[~d.index.duplicated()].loc[:END]

def swaps(sec):
    d=pd.read_csv(DATA/f'{sec}_swap.csv',parse_dates=['date']).set_index('date')['swap']
    return d.loc[:END]


# ДСВД/праздничные дополнительные сессии не имеют расчёта фандинга.
NON_SETTLEMENT = {pd.Timestamp(x) for x in ['2026-02-23','2026-05-01','2026-06-12']}

def settlement_rate(rates, day):
    value=rates.get(day)
    if pd.notna(value):return float(value)
    if day.weekday()>=5 or day in NON_SETTLEMENT:return 0.
    raise ValueError('Нет ставки фандинга в основной торговый день '+str(day))

def settlement_prices(sec):
    d=pd.read_csv(DATA/f'{sec}_swap.csv',parse_dates=['date']).set_index('date')
    return d['settle'] if 'settle' in d else pd.Series(dtype=float)


def dividend_adjustments():
    return pd.read_csv(DATA/'IMOEXDIV.csv',parse_dates=['date']).set_index('date')['CLOSE']


def daily(d):
    return d.resample('D').agg({'o':'first','h':'max','l':'min','c':'last','v':'sum'}).dropna(subset=['c'])

def dd(pnl):
    v=np.r_[0.,np.cumsum(np.asarray(pnl,float))]
    return float(np.max(np.maximum.accumulate(v)-v))

def summary(pnl):
    x=pd.Series(pnl).astype(float)
    eq=x.cumsum(); mx=dd(x)
    return {'net':round(x.sum(),2),'mtm_dd':round(mx,2),'daily_vol':round(x.std(),4),
            'net_dd':round(x.sum()/mx,3) if mx else None,
            'sharpe':round(x.mean()/x.std()*math.sqrt(252),3) if x.std() else None,
            'years':{str(k):round(v,2) for k,v in x.groupby(x.index.year).sum().items()}}

def st9(variant,scale=1.,slip=.0011):
    dividends=dividend_adjustments();entitled=0.
    names=list(PV);raw={s:load(s) for s in names}
    # Общий период, ранняя история используется только для прогрева индикаторов.
    start=max(d.index.min() for d in raw.values())
    finish=min(d.index.max() for d in raw.values())
    raw={s:d.loc[start:finish] for s,d in raw.items()}
    rates={s:swaps(s) for s in names}
    settlements={s:settlement_prices(s) for s in names}
    if variant.startswith('daily'):
        n=int(variant.split('_')[1]);params={s:(n,{20:10,60:20,120:40}[n],5.) for s in names}
        bars={s:daily(d) for s,d in raw.items()}
    else:
        params={'USDRUBF':(70,10,5.),'IMOEXF':(45,35,6.)};bars=raw
    engines={s:St9Engine(s,*params[s],14,pv=PV[s],fee_per_lot=0,fee_pct_notional=.05) for s in names}
    bank=500000.;positions={};pending={};curve={};trades=[];last={};costs={'fee':0.,'slip':0.,'funding':0.,'rolls':0,'quarter_entries':0,'perp_entries':0,'funding_missing':0,'nonsettlement_days':0,'dividend_adjustment':0.}
    qbars={s:load(s) for s in ['SiZ5','SiH6','SiM6','SiU6','SiZ6','MXZ5','MXH6','MXM6','MXU6','MXZ6']} if variant=='route' else {}
    qspec={s:json.loads((DATA/f'{s}_spec.json').read_text()) for s in qbars}
    # Единицы: MX котируется индекс×100, STEPPRICE/MINSTEP=1; Si — рубли за $1000, pv=1.
    peak=bank;halt=False;breach=0;previous_year=None;curve_offset=0.
    daylast={s:set(d.groupby(d.index.normalize()).tail(1).index) for s,d in bars.items()}
    lookups={s:{t:r for t,r in d.iterrows()} for s,d in bars.items()}
    qlook={s:{t:r for t,r in d.iterrows()} for s,d in qbars.items()}
    union=sorted(set().union(*(d.index for d in bars.values())))
    def quote(inst,t,col):
        dic=qlook if inst in qlook else {k:lookups[k] for k in names}
        r=dic[inst].get(t)
        return float(r[col]) if r is not None else None
    def close(s,t,reason):
        nonlocal bank
        p=positions[s];px=quote(p['inst'],t,'o')
        if px is None:return False
        fill=px*(1-slip*p['sign']);fee=abs(fill*p['qty']*p['pv'])*.0005
        gross=(fill-p['entry'])*p['sign']*p['qty']*p['pv']
        bank+=(fill-p.get('cash_mark',p['entry']))*p['sign']*p['qty']*p['pv']-fee
        costs['fee']+=fee;costs['slip']+=px*slip*p['qty']*p['pv']
        trades.append({'axis':s,'instrument':p['inst'],'exit':str(t),'entry':str(p['time']),'net':gross-fee-p['entry_fee']+p['funding'],'reason':reason})
        del positions[s];return True
    for t in union:
        if previous_year is not None and t.year!=previous_year and t.year in (2024,2025):
            if curve:
                old=max(curve)
                # Независимые train/evaluation: условная ликвидация на границе, капитал снова 500к.
                boundary_cost=sum(p.get('last',p['entry'])*p['qty']*p['pv']*(.0005+slip) for p in positions.values())
                curve[old]-=boundary_cost
                curve_offset=curve[old]
            bank=500000.;positions.clear();pending.clear();peak=bank;halt=False;breach=0;entitled=0.
            for engine in engines.values():engine.position=None
        previous_year=t.year
        for s in names:
            r=lookups[s].get(t)
            if r is None:continue
            last[s]=float(r.c)
            if s in positions and positions[s]['inst'] in qspec:
                exp=pd.Timestamp(qspec[positions[s]['inst']]['LASTTRADEDATE'])
                if (exp-t.normalize()).days<=3:
                    if close(s,t,'roll'):
                        engines[s].position=None;costs['rolls']+=1
            if s in pending:
                sig=pending.pop(s)
                if s in positions and not close(s,t,sig['reason']):
                    pending[s]=sig;continue
                if engines[s].position is not None:engines[s].close(float(r.o),int(t.timestamp()*1000),'execution')
                if sig.get('new_side') and not halt:
                    sign=1 if sig['new_side']=='long' else -1
                    atr=sig.get('atr') or 0
                    cap=bank+sum(((quote(p['inst'],t,'o') or p.get('last',p['entry']))-p.get('cash_mark',p['entry']))*p['sign']*p['qty']*p['pv'] for p in positions.values())
                    if s=='USDRUBF' or variant.startswith('daily'):
                        qty=int(5000*scale/(params[s][2]*atr*PV[s])) if atr else 0
                        if variant in ('base','route') and s=='USDRUBF' and qty<5:qty=0
                    else:qty=int(max(0,cap)*.15/2*scale/(float(r.o)*PV[s]*.1046))
                    if qty:
                        inst=s;ip=float(r.o);pv=PV[s];iq=qty
                        if variant=='route':
                            prefix='Si' if s=='USDRUBF' else 'MX';norm=1000. if s=='USDRUBF' else 100.
                            candidates=[q for q in qbars if q.startswith(prefix) and t in qlook[q] and (pd.Timestamp(qspec[q]['LASTTRADEDATE'])-t.normalize()).days>10]
                            if candidates:
                                q=min(candidates,key=lambda a:qspec[a]['LASTTRADEDATE']);qp=float(qlook[q][t].o)
                                # Решение доступно на предыдущем закрытии; текущий open не используется для выбора.
                                hist=raw[s].loc[:t-pd.Timedelta(nanoseconds=1)]
                                qh=qbars[q].loc[:t-pd.Timedelta(nanoseconds=1)]
                                common=hist.index.intersection(qh.index)
                                sr=rates[s].loc[:t.normalize()-pd.Timedelta(days=1)].dropna().tail(20)
                                if len(common) and len(sr)==20:
                                    u=common[-1];pp=float(hist.loc[u,'c']);qprev=float(qh.loc[u,'c'])/norm
                                    days=(pd.Timestamp(qspec[q]['LASTTRADEDATE'])-u.normalize()).days
                                    funding_days=sum(x.weekday()<5 and x not in NON_SETTLEMENT for x in pd.date_range(t.normalize(),periods=5))
                                    perp_carry=sign*sr.mean()/pp*funding_days
                                    quart_carry=sign*(qprev/pp-1)/days*5
                                    if quart_carry<perp_carry:
                                        inst=q;ip=qp;pv=1.;iq=int(qty*float(r.o)*PV[s]/qp)
                        if iq:
                            fill=ip*(1+slip*sign);fee=fill*iq*pv*.0005;bank-=fee
                            costs['fee']+=fee;costs['slip']+=ip*slip*iq*pv
                            costs['quarter_entries' if inst!=s else 'perp_entries']+=1
                            positions[s]={'inst':inst,'qty':iq,'pv':pv,'entry':fill,'sign':sign,'time':t,'entry_fee':fee,'funding':0.,'ref':float(r.o),'perp_qty':qty}
                            engines[s].open(sig['new_side'],float(r.o),qty,int(t.timestamp()*1000),atr)
            b=Bar(int(t.timestamp()*1000),float(r.o),float(r.h),float(r.l),float(r.c))
            sig=engines[s].step(b,1)
            if sig:pending[s]=sig
            if s in positions and positions[s]['inst']==s and t in daylast[s]:
                sr=settlement_rate(rates[s],t.normalize())
                if t.normalize() not in rates[s].index:costs['nonsettlement_days']+=1
                p=positions[s];f=-p['sign']*sr*p['qty']*p['pv'];bank+=f;p['funding']+=f;costs['funding']+=f
        # Денежная серия учитывает ежедневную вармаржу, а не только закрытые сделки.
        for axis,p in positions.items():
            if t in daylast[axis] and t.normalize() in rates[axis].index:
                mark=(settlements[axis].get(t.normalize()) if p['inst']==axis else quote(p['inst'],t,'c'))
                if pd.notna(mark):
                    bank+=(float(mark)-p.get('cash_mark',p['entry']))*p['sign']*p['qty']*p['pv']
                    p['cash_mark']=float(mark)
        if t in daylast['IMOEXF'] and t.normalize() in rates['IMOEXF'].index:
            # Право на поправку фиксируется предыдущим клирингом, даже если уже закрылись.
            value=dividends.get(t.normalize())
            if pd.isna(value):raise ValueError('Нет IMOEXDIV '+str(t.date()))
            adjustment=entitled*float(value)*10
            bank+=adjustment;costs['dividend_adjustment']+=adjustment
            p=positions.get('IMOEXF')
            entitled=p['qty']*p['sign'] if p and p['inst']=='IMOEXF' else 0.
        unreal=0.
        for s,p in positions.items():
            px=quote(p['inst'],t,'c')
            if px is not None:p['last']=px
            px=p.get('last',p['entry'])
            unreal+=(px-p.get('cash_mark',p['entry']))*p['sign']*p['qty']*p['pv']
        if t>=start:curve[t]=curve_offset+bank+unreal-500000.
        # Боевой guard по денежной серии: накопленный фандинг и реализованный результат.
        peak=max(peak,bank);breach=breach+1 if bank<peak*.82 else 0
        if breach>=2 and not halt:
            halt=True
            for s in positions:pending[s]={'act':'close','new_side':None,'reason':'dd_stop','atr':0}
    # На конце одинаково учитываем стоимость условной ликвидации всех остатков.
    liquidation=0.
    for p in positions.values():
        notional=p.get('last',p['entry'])*p['qty']*p['pv']
        liquidation+=notional*(.0005+slip)
        costs['fee']+=notional*.0005;costs['slip']+=notional*slip
    curve[union[-1]]-=liquidation
    level=pd.Series(curve).groupby(lambda t:t.normalize()).last()
    series=level.diff();series.iloc[0]=level.iloc[0]
    return series,{'costs':costs,'trades':len(trades),'halted':halt,'open_positions':len(positions),'trade_rows':trades}


def stock(tk):
    d=pd.read_csv(DATA/f'd_{tk}.csv',parse_dates=['TRADEDATE']).set_index('TRADEDATE').rename(columns={'OPEN':'o','HIGH':'h','LOW':'l','CLOSE':'c'})
    return d[['o','h','l','c']].sort_index().loc['2023-01-01':END]

def beta_before_entry(stock_close, index_close, entry):
    hist=pd.concat([stock_close.pct_change(),index_close.pct_change()],axis=1,sort=True)
    hist=hist.loc[:entry-pd.Timedelta(days=1)].dropna().tail(120)
    if len(hist)<120:return None
    beta=hist.iloc[:,0].cov(hist.iloc[:,1])/hist.iloc[:,1].var()
    return float(np.clip(.5+.5*beta,0,2))


def st8(beta_hedge=False,scale=1.,slip=.0011,ex_rule='legacy'):
    # ex_rule='legacy' — как в прогоне 02.11 (торг.день строго перед регдатой = последний день С дивидендом при T+1);
    # 't1' — фикс 07.11.2026: день гэпа = последний торг.день <= регдаты (см. app/st8/service._gap_day)
    dividends=dividend_adjustments()
    prices={tk:stock(tk) for tk in TICKERS};hedge=daily(load('IMOEXF'));rates=swaps('IMOEXF')
    idx=pd.read_csv(DATA/'index_IMOEX.csv',parse_dates=['TRADEDATE']).set_index('TRADEDATE')['CLOSE']
    calendar=pd.DatetimeIndex(sorted(idx.dropna().index.unique()));pnl=pd.Series(0.,index=hedge.index)
    events=[];missing=[];stats={'fees':0.,'slip':0.,'funding':0.,'borrow':0.,'future_events':0,'stock_events':0,'dividend_adjustment':0.}
    specs={f.stem[:-5]:json.loads(f.read_text()) for f in DATA.glob('*_spec.json') if f.stem.startswith(('LK','AL'))}
    fs={sec:load(sec,24) for sec in specs if (DATA/f'{sec}_24.csv').exists()}
    for tk,d in prices.items():
        divs=pd.read_csv(DATA/f'sl_{tk}.csv').groupby('rc')['value'].sum()
        for rc,div in divs.items():
            exi=calendar.searchsorted(pd.Timestamp(rc),side='right' if ex_rule=='t1' else 'left')-1
            if exi<130 or exi+5>=len(calendar):continue
            ex=calendar[exi]
            if ex<pd.Timestamp('2024-01-01') or ex>pd.Timestamp(END):continue
            for side,ei,xi in [('long',exi-10,exi-2),('short',exi,exi+5)]:
                if side=='long' and ex.month==7 or side=='short' and ex.month in (8,12):continue
                entry,exit=calendar[ei],calendar[xi]
                if exit>pd.Timestamp(END):continue
                if entry not in d.index or exit not in d.index or entry not in hedge.index or exit not in hedge.index:continue
                prior=d.loc[:entry-pd.Timedelta(days=1),'c'].dropna()
                if prior.empty or div/prior.iloc[-1]<.01:continue
                estimated_beta=beta_before_entry(d.c,idx,entry)
                if estimated_beta is None:continue
                beta=estimated_beta if beta_hedge else 1.
                inst=tk;bars=d;pv=1.
                prefix={'LKOH':'LK','ALRS':'AL'}.get(tk)
                if prefix:
                    cs=[s for s in specs if s.startswith(prefix) and pd.Timestamp(specs[s]['LASTTRADEDATE'])>ex+pd.Timedelta(days=7) and s in fs and entry in fs[s].index]
                    if not cs:
                        missing.append((tk,str(ex.date()),side));continue # не подменяем отсутствующий квартальник спотом
                    inst=min(cs,key=lambda s:specs[s]['LASTTRADEDATE']);bars=fs[inst]
                    m=specs[inst];pv=m.get('point_value',1.)
                days=hedge.loc[entry:exit].index.intersection(bars.index)
                if len(days)<2 or days[0]!=entry or days[-1]!=exit:continue
                # Календарный вход известен заранее, fill close+издержки; β рассчитана строго до входа.
                ep=float(bars.loc[entry,'c']);qty=int(100000*scale/(ep*pv))
                if inst==tk and tk in ('SELG','LSNGP'):qty=qty//10*10
                if not qty:continue
                sign=1 if side=='long' else -1
                hqty=round(ep*pv*qty*beta/(float(hedge.loc[entry,'c'])*10)) if side=='long' else 0
                fee=(ep*pv*qty+float(hedge.loc[entry,'c'])*10*hqty)*.0005
                drag=(ep*pv*qty+float(hedge.loc[entry,'c'])*10*hqty)*slip
                event=pd.Series(0.,index=days);event.iloc[0]-=fee+drag
                totalfee=fee;totaldrag=drag;fund=0.;borrow=0.;lastp=ep;lasth=float(hedge.loc[entry,'c']);closed=exit;stop_pending=False
                for date in days[1:]:
                    px=float(bars.loc[date,'o' if stop_pending else 'c']);hp=float(hedge.loc[date,'o' if stop_pending else 'c'])
                    event.loc[date]+=sign*(px-lastp)*qty*pv-(hp-lasth)*hqty*10
                    if inst==tk and side=='short':
                        charge=lastp*qty*pv*.20*(date-days[days.get_loc(date)-1]).days/365
                        event.loc[date]-=charge;borrow+=charge
                    # Позиция хеджа прошла клиринг предыдущего дня; только фактические ставки.
                    sr=settlement_rate(rates,days[days.get_loc(date)-1]);f=sr*hqty*10
                    event.loc[date]+=f;fund+=f
                    if date in rates.index:
                        value=dividends.get(date)
                        if pd.isna(value):raise ValueError('Нет IMOEXDIV '+str(date.date()))
                        adjustment=-hqty*10*float(value)
                        event.loc[date]+=adjustment;stats['dividend_adjustment']+=adjustment
                    lastp=px;lasth=hp
                    if stop_pending:closed=date;break
                    stop_pending=event.loc[:date].sum()<-ep*qty*pv*.05
                event=event.loc[:closed]
                xf=(lastp*qty*pv+lasth*hqty*10)*.0005;xd=(lastp*qty*pv+lasth*hqty*10)*slip
                event.iloc[-1]-=xf+xd;totalfee+=xf;totaldrag+=xd
                pnl.loc[event.index]+=event
                stats['fees']+=totalfee;stats['slip']+=totaldrag;stats['funding']+=fund;stats['borrow']+=borrow
                stats['future_events' if inst!=tk else 'stock_events']+=1
                events.append({'ticker':tk,'ex':str(ex.date()),'side':side,'entry':str(entry.date()),'exit':str(closed.date()),'instrument':inst,'beta':beta,'net':event.sum(),'qty':qty,'hedge_qty':hqty})
    return pnl,{'trades':len(events),'costs':stats,'missing_events':missing,'trade_rows':events}


def compare():
    OUT.mkdir(parents=True,exist_ok=True)
    report={'model':{'fee_per_side':.0005,'slip_base':.0011,'slip_stress':.0022,'risk_train':'2024','evaluation':'2025-2026','dividend_adjustment':'IMOEXDIV; entitlement from previous clearing', 'cash_series':'daily futures variation margin; independent 500000 RUB at evaluation start', 'risk_definition':'daily P&L volatility matched on 2024; not equality of future tail risk', 'st8_calendar':'conditional; announcement timestamps unavailable','st8_execution':'daily close planned events; daily marks; not exact 17:00 live replay'},'results':{}}
    jobs=[('st9','base'),('st9','route'),('st9','daily_20'),('st9','daily_60'),('st9','daily_120'),('st8','base'),('st8','beta')]
    baseline={};allcurves={}
    for family,variant in jobs:
        fn=(lambda sc,sl:st9(variant,sc,sl)) if family=='st9' else (lambda sc,sl:st8(variant=='beta',sc,sl))
        series,details=fn(1.,.0011)
        train=series.loc['2024'];vol=float(train.std())
        if variant=='base':baseline[family]=vol;scale=1.
        else:scale=baseline[family]/vol if vol>0 else 0.
        if variant!='base' and scale:
            # Подбор ТОЛЬКО масштаба по train-vol, без просмотра доходности проверки.
            for _ in range(4):
                series,details=fn(scale,.0011)
                achieved=float(series.loc['2024'].std())
                if not achieved or abs(achieved/baseline[family]-1)<.05:break
                scale*=baseline[family]/achieved
        key=family+'_'+variant
        test=series.loc['2025':]
        row={'scale_from_train':scale,'train':summary(series.loc['2024']),'train_risk_ratio':float(series.loc['2024'].std())/baseline[family],'evaluation':summary(test),'details':details}
        stress,sd=fn(scale,.0022)
        row['stress']=summary(stress.loc['2025':]);row['stress_trades']=sd['trades']
        row['evaluation_trades']=sum(pd.Timestamp(t['exit'])>=pd.Timestamp('2025-01-01') for t in details['trade_rows'])
        if family=='st9':row['routing_window']=summary(series.loc['2025-07-01':])
        if variant=='base':row['equal_dd_net']=row['evaluation']['net']
        else:
            bd=report['results'][family+'_base']['evaluation']['mtm_dd'];cd=row['evaluation']['mtm_dd']
            row['equal_dd_net']=round(row['evaluation']['net']*bd/cd,2) if cd else None
        report['results'][key]=row;allcurves[key]=series
        (OUT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
        pd.DataFrame(allcurves).to_csv(OUT/'daily_pnl.csv')
        print(key,json.dumps({k:v for k,v in row.items() if k!='details'},ensure_ascii=False),flush=True)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--run',action='store_true');a=parser.parse_args()
    if a.prepare:prepare()
    if a.run:compare()
