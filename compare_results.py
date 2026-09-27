#!/usr/bin/env python3
import argparse, json
from pathlib import Path

def load(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))

def v(d,*keys,default=None):
    for k in keys:
        if k in d: return d[k]
    return default

ap=argparse.ArgumentParser(description='Compara reporte AION LAB/R1H contra AION CORE')
ap.add_argument('lab_json')
ap.add_argument('core_json')
a=ap.parse_args(); lab=load(a.lab_json); core=load(a.core_json)
print('\nAION — COMPARACIÓN R1H')
print('='*64)
for name,d in [('LAB',lab),('CORE',core)]:
    print(f"{name:5s} markets={v(d,'resolved_markets',default=len(d.get('markets',[])))} closed={v(d,'closed',default=0)} netR={v(d,'net_r',default=0):+.3f} avgR={v(d,'avg_r',default=None)} WR={v(d,'win_rate_closed_pct','win_rate_pct',default=None)}")
print('='*64)
print('No declara ganador: compara frecuencia, netR, avgR, WR y drawdown/excursiones disponibles.')
