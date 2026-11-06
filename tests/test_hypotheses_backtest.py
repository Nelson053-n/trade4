"""Проверки финансовой механики исследовательского раннера без сети."""
import datetime as dt
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest
from backtests import hypotheses_20261102 as bt


def test_cache_timestamp_round_trip():
    original=dt.datetime(2026,10,29,23)
    result=bt.frame([(original.timestamp()*1000,1,1,1,1,10)])
    assert result.index[0]==pd.Timestamp(original)


def test_missing_funding_is_not_assumed_zero_on_workday():
    empty=pd.Series(dtype=float)
    assert bt.settlement_rate(empty,pd.Timestamp('2026-10-25'))==0
    with pytest.raises(ValueError,match='Нет ставки'):
        bt.settlement_rate(empty,pd.Timestamp('2026-10-26'))


def test_equity_drawdown_includes_open_loss():
    assert bt.dd([100,-140,80])==140


def test_next_open_cash_fees_funding_and_first_day(monkeypatch):
    times=pd.to_datetime(['2024-01-03 10:00','2024-01-03 11:00','2024-01-03 12:00','2024-01-04 10:00'])
    prices=pd.DataFrame({'o':[100,200,205,220],'c':[100,205,210,225],
                         'h':[100,205,210,225],'l':[100,200,205,220],'v':[10000]*4},index=times)
    monkeypatch.setattr(bt,'load',lambda *a:prices.copy())
    monkeypatch.setattr(bt,'settlement_prices',lambda *a:pd.Series(dtype=float))
    monkeypatch.setattr(bt,'dividend_adjustments',lambda:pd.Series(0.,index=times.normalize().unique()))
    monkeypatch.setattr(bt,'swaps',lambda *a:pd.Series(.01,index=pd.to_datetime(['2024-01-03','2024-01-04'])))
    class Engine:
        def __init__(self,sec,*a,**kw):self.sec=sec;self.position=None;self.i=0
        def step(self,bar,lots):
            self.i+=1
            if self.sec!='USDRUBF':return None
            if self.i==1:return {'new_side':'long','reason':'entry','atr':.1}
            if self.i==3:return {'new_side':None,'reason':'exit','atr':.1}
        def open(self,side,px,qty,ts,atr):self.position=SimpleNamespace(side=side)
        def close(self,*a):self.position=None
    monkeypatch.setattr(bt,'St9Engine',Engine)
    pnl,details=bt.st9('base')
    assert pnl.sum()==pytest.approx(193180.11)
    assert details['costs']['fee']==pytest.approx(2099.89)
    assert details['costs']['funding']==pytest.approx(-100)
    assert details['trades']==1
    monkeypatch.setattr(bt,'settlement_prices',lambda *a:pd.Series([210.,220.],index=times.normalize().unique()))
    settled,_=bt.st9('base')
    assert settled.sum()==pytest.approx(pnl.sum())


def test_beta_never_uses_entry_day_or_future():
    dates=pd.date_range('2023-01-01',periods=260)
    rng=np.random.default_rng(17)
    returns=rng.normal(0,.01,len(dates))
    index=pd.Series(100*np.cumprod(1+returns),index=dates)
    stock=pd.Series(100*np.cumprod(1+2*returns),index=dates)
    entry=dates[180]
    before=bt.beta_before_entry(stock,index,entry)
    assert before==pytest.approx(1.5)
    stock.loc[entry:]*=3
    index.loc[entry:]*=.1
    assert bt.beta_before_entry(stock,index,entry)==before


def test_index_dividend_is_paid_after_position_already_closed(monkeypatch):
    times=pd.to_datetime(['2024-01-03 10:00','2024-01-04 10:00','2024-01-05 10:00'])
    prices=pd.DataFrame({c:[100.]*3 for c in ['o','c','h','l']},index=times)
    prices['v']=100000
    monkeypatch.setattr(bt,'load',lambda *a:prices.copy())
    monkeypatch.setattr(bt,'settlement_prices',lambda *a:pd.Series(dtype=float))
    monkeypatch.setattr(bt,'swaps',lambda *a:pd.Series(0.,index=times.normalize()))
    dividends=pd.Series(0.,index=times.normalize())
    monkeypatch.setattr(bt,'dividend_adjustments',lambda:dividends.copy())
    class Engine:
        def __init__(self,sec,*a,**kw):self.sec=sec;self.i=0;self.position=None
        def step(self,bar,lots):
            self.i+=1
            if self.sec!='IMOEXF':return None
            if self.i==1:return {'new_side':'long','reason':'entry','atr':1}
            if self.i==2:return {'new_side':None,'reason':'exit','atr':1}
        def open(self,side,*a):self.position=SimpleNamespace(side=side)
        def close(self,*a):self.position=None
    monkeypatch.setattr(bt,'St9Engine',Engine)
    base,_=bt.st9('base')
    dividends.iloc[-1]=10
    corrected,details=bt.st9('base')
    assert corrected.sum()-base.sum()==pytest.approx(35800)
    assert details['costs']['dividend_adjustment']==35800
    assert details['open_positions']==0


def test_training_profit_does_not_increase_evaluation_capital(monkeypatch):
    times=pd.to_datetime(['2024-01-03 10:00','2024-01-03 11:00','2024-01-03 12:00',
                          '2025-01-03 10:00','2025-01-03 11:00','2025-01-03 12:00'])
    prices=pd.DataFrame({'o':[100,100,200,100,100,100],'c':[100,100,200,100,100,100],
                         'h':[100,100,200,100,100,100],'l':[100,100,200,100,100,100],
                         'v':[100000]*6},index=times)
    monkeypatch.setattr(bt,'load',lambda *a:prices.copy())
    monkeypatch.setattr(bt,'settlement_prices',lambda *a:pd.Series(dtype=float))
    days=times.normalize().unique()
    monkeypatch.setattr(bt,'swaps',lambda *a:pd.Series(0.,index=days))
    monkeypatch.setattr(bt,'dividend_adjustments',lambda:pd.Series(0.,index=days))
    quantities=[]
    class Engine:
        def __init__(self,sec,*a,**kw):self.sec=sec;self.i=0;self.position=None
        def step(self,bar,lots):
            self.i+=1
            if self.sec!='IMOEXF':return None
            if self.i in (1,4):return {'new_side':'long','reason':'entry','atr':1}
            if self.i in (2,5):return {'new_side':None,'reason':'exit','atr':1}
        def open(self,side,px,qty,*a):self.position=SimpleNamespace(side=side);quantities.append(qty)
        def close(self,*a):self.position=None
    monkeypatch.setattr(bt,'St9Engine',Engine)
    bt.st9('base')
    assert quantities==[358,358]
