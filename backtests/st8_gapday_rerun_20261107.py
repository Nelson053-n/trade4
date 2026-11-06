"""Перепроверка st8 после фикса дня гэпа (07.11.2026): прогон 02.11 (legacy) против правила T+1.

Данные — замороженные входы out/hypotheses_20261102/data (тот же раннер, те же издержки).
Меняется ТОЛЬКО определение ex: legacy = торг.день строго перед регдатой (при T+1 это последний
день С дивидендом), t1 = последний торг.день <= регдаты (фактический гэп, проверено по ISS).
Раннер не начисляет шортисту дивиденд — в legacy шорт, открытый ДО гэпа, получал гэп даром.
Запуск: .venv/bin/python backtests/st8_gapday_rerun_20261107.py
"""
import json
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hypotheses_20261102 as H  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / 'out' / 'st8_gapday_check_20261107'
OUT.mkdir(parents=True, exist_ok=True)
res = {}
for rule in ('legacy', 't1'):
    for slip in (.0011, .0022):
        pnl, det = H.st8(False, 1., slip, ex_rule=rule)
        rows = pd.DataFrame(det['trade_rows'])
        rows['exit'] = pd.to_datetime(rows['exit'])
        rows['period'] = rows['exit'].dt.year.map(lambda y: 'train_2024' if y == 2024 else 'eval_2025_26')
        g = rows.groupby(['period', 'side'])['net'].agg(['count', 'sum', 'mean']).round(0)
        key = f'{rule}_slip{slip}'
        res[key] = {'by_period_side': {f'{p}|{s}': v for (p, s), v in g.to_dict('index').items()},
                    'total': {p: round(float(rows.loc[rows.period == p, 'net'].sum()), 0)
                              for p in ('train_2024', 'eval_2025_26')},
                    'missing': len(det['missing_events'])}
        if slip == .0011:
            rows.to_csv(OUT / f'trades_{rule}.csv', index=False)
        print(key, json.dumps(res[key], ensure_ascii=False))
(OUT / 'rerun.json').write_text(json.dumps(res, ensure_ascii=False, indent=1))
