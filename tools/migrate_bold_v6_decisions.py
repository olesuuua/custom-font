#!/usr/bin/env python3
"""Atomically migrate active review state to the full Bold v6 candidate."""
from __future__ import annotations
import csv, json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'qa'/'assets'/'bold-report.csv'
DECISIONS=ROOT/'qa'/'bold-manual-decisions.json'
READY=ROOT/'qa'/'bold-ready-to-pass.json'
VERSION='bold-manual-review-v6'
METRICS='bold-metrics-v6'

def atomic(path,value):
 fd,tmp=tempfile.mkstemp(prefix=path.stem+'-',suffix='.json',dir=str(path.parent))
 try:
  with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as h:
   json.dump(value,h,ensure_ascii=False,indent=2,sort_keys=True); h.write('\n'); h.flush(); os.fsync(h.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp): os.unlink(tmp)

def main():
 with REPORT.open(encoding='utf-8-sig',newline='') as h: rows={r['glyph']:r for r in csv.DictReader(h)}
 old=json.loads(DECISIONS.read_text(encoding='utf-8-sig'))
 changed={name for name,row in rows.items() if row.get('changed_in_version')=='true'}
 migrated={}
 now=datetime.now(timezone.utc).isoformat()
 for name,row in rows.items():
  if name in changed: continue
  decision=old.get('decisions',{}).get(name)
  if not decision: continue
  migrated[name]={
   'status':decision['status'],'regular_hash':row['regular_hash'],'base_hash':row['base_hash'],
   'bold_hash':row['bold_hash'],'metrics_version':METRICS,
   'updated_at':decision.get('updated_at',now),
  }
 payload={'version':VERSION,'metrics_version':METRICS,'decisions':migrated,'migrated_at':now}
 atomic(DECISIONS,payload)
 ready=json.loads(READY.read_text(encoding='utf-8-sig')) if READY.exists() else {'glyphs':{}}
 glyphs=ready.get('glyphs',{})
 if not isinstance(glyphs,dict): glyphs={name:{} for name in glyphs}
 kept={}
 for name,value in glyphs.items():
  if name in changed or name not in rows: continue
  row=rows[name]; value=dict(value) if isinstance(value,dict) else {}
  value.update({'regular_hash':row['regular_hash'],'base_hash':row['base_hash'],'bold_hash':row['bold_hash']})
  kept[name]=value
 atomic(READY,{'version':'bold-ready-to-pass-v6','metrics_version':METRICS,'glyphs':kept,'migrated_at':now})
 counts={'pass':0,'almost_done':0,'needs_rework':0,'unreviewed':len(changed)}
 for value in migrated.values(): counts[value['status']]+=1
 counts['unreviewed'] += len(rows)-len(changed)-len(migrated)
 print(json.dumps({'changed_reset':len(changed),'decisions_preserved':len(migrated),'counts':counts},sort_keys=True))
 return 0
if __name__=='__main__': raise SystemExit(main())
