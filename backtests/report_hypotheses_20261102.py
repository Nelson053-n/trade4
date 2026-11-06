import json,hashlib
from pathlib import Path
import numpy as np,pandas as pd
root=Path(__file__).resolve().parents[1];out=root/'out/hypotheses_20261102'
r=json.loads((out/'results.json').read_text())
r['model'].update(dividend_adjustment='IMOEXDIV, previous clearing entitlement',cash_series='daily variation margin, independent 500000 RUB at evaluation start',risk_definition='2024 daily P&L volatility, scales frozen before 2025',limitations=['No point-in-time dividend announcement archive; ST8 conditional event study.','ST8 daily-close fills and marks approximate live execution window; no intraday drawdown guarantee.','Historical order books absent; slippage assumptions 0.11% and 0.22% per side.','Historical margin rates approximated; exact historical broker rejection/collateral not reconstructed.','Current parameters/universe already selected using part of history; test is not pristine OOS.','Routing quarterly data starts June 2025; earlier routing equals baseline.','Equal-DD profit is descriptive linear normalization, not a resimulation or a deployable sizing rule.'])
rows=[]
for name,v in r['results'].items():
 rows.append({'variant':name,'test_net_rub':v['evaluation']['net'],'daily_mark_dd_rub':v['evaluation']['mtm_dd'],'equal_dd_diagnostic_net_rub':v['equal_dd_net'],'stress_net_rub':v['stress']['net'],'train_vol_ratio':v['train_risk_ratio'],'test_closed_trades':v['evaluation_trades'],'scale_from_2024':v['scale_from_train'],'halted_base':v['details'].get('halted',False)})
pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False)
x=pd.read_csv(out/'daily_pnl.csv',index_col=0,parse_dates=True).loc['2025':].fillna(0)
rng=np.random.default_rng(20261102);ci={}
for name in x:
 if name.endswith('_base'):continue
 base=name[:3]+'_base';a=x[base].to_numpy();b=x[name].to_numpy();n=len(a);samples=[]
 for _ in range(2000):
  starts=rng.integers(0,n,size=(n+19)//20);ids=((starts[:,None]+np.arange(20))%n).ravel()[:n]
  samples.append(float((b[ids]-a[ids]).sum()))
 ci[name]={'paired_net_difference_rub':round(float((b-a).sum()),2),'block20_95pct_interval':np.quantile(samples,[.025,.975]).round(2).tolist()}
(out/'bootstrap.json').write_text(json.dumps(ci,ensure_ascii=False,indent=2))
r['bootstrap']=ci
(out/'results.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
manifest={f.name:{'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'bytes':f.stat().st_size} for f in (out/'data').iterdir()}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2))
labels={'st9_base':'ST9 текущие правила','st9_route':'ST9 перп / квартальник','st9_daily_20':'ST9 дневной 20/10','st9_daily_60':'ST9 дневной 60/20','st9_daily_120':'ST9 дневной 120/40','st8_base':'ST8 текущие правила','st8_beta':'ST8 β-хедж'}
body=''
for row in rows:
 body+='<tr><td>'+labels[row['variant']]+'</td>'+''.join('<td>'+f"{row[k]:,.2f}".replace(',',' ')+'</td>' for k in ['test_net_rub','daily_mark_dd_rub','equal_dd_diagnostic_net_rub','stress_net_rub'])+'<td>'+str(row['test_closed_trades'])+'</td></tr>'
report='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Проверка гипотез ST8/ST9</title><style>body{font:16px/1.6 system-ui;max-width:1150px;margin:50px auto;padding:0 24px;color:#20242a}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;text-align:right;border-bottom:1px solid #ddd}td:first-child,th:first-child{text-align:left}p{max-width:900px}.note{background:#f3f5f7;padding:18px}</style><h1>Проверка гипотез ST8 / ST9</h1><p>Обучение: 2024. Проверка: 01.01.2025–29.10.2026. Риск — волатильность дневного P&amp;L на обучении; масштаб фиксируется до проверки. Начальный капитал ST9 одинаков: 500 000 ₽.</p><p class="note">Результат: оснований менять стратегии нет. β-хедж ST8 уступил базе; маршрутизация ST9 дала около 2% после условной нормировки к равной просадке. Дневной 20/10 устойчивее к издержкам, но не обогнал базу при основной модели.</p><table><thead><tr><th>Вариант</th><th>Net, ₽</th><th>Просадка по дневным оценкам, ₽</th><th>Net при равной DD*</th><th>Net, стресс</th><th>Закрытий в проверке</th></tr></thead><tbody>'''+body+'''</tbody></table><p>* Линейная описательная нормировка к просадке базы своей стратегии. Это не доходность повторного прогона с известным заранее размером.</p><h2>Издержки</h2><p>Комиссия: 0,05% номинала за сторону каждой ноги. Неблагоприятное отклонение исполнения: 0,11% за сторону; стресс 0,22%. ST9 — следующий доступный open. Учитываются SWAPRATE, дивидендная поправка IMOEXDIV по позиции предыдущего клиринга, ежедневная вармаржа и расходы роллов. Для шорта акций ST8 заложена стоимость займа 20% годовых как допущение. Налоги и доход свободного кэша не включены.</p><h2>Пределы интерпретации</h2><p>ST8 — условное исследование событий: нет истории дат объявления дивидендов. Дневные отметки не измеряют внутридневную просадку; точное исполнение окна 17:00–18:45 не воспроизводится. Нет исторического стакана, точного ряда ГО и брокерских отказов. Параметры базы и набор бумаг уже выбраны с использованием части истории: полностью нетронутого OOS нет. Дневные варианты 60/20 и 120/40 остановлены защитой капитала. Стоп 18% не гарантирует такую же максимальную просадку: позиции закрываются следующим доступным исполнением.</p><p>Источник правил поправки и фандинга: <a href="https://www.moex.com/a8803">Московская биржа</a>. Прод и настройки торговли не изменены.</p><p><a href="comparison.csv">CSV сравнения</a> · <a href="results.json">Полные результаты</a> · <a href="daily_pnl.csv">Дневной P&amp;L</a> · <a href="manifest.json">Хэши исходных данных</a></p></html>'''
(out/'report.html').write_text(report)
print('REPORT',out/'report.html');print('DATA_FILES',len(manifest));print('BOOTSTRAP',json.dumps(ci,ensure_ascii=False))
