#!/usr/bin/env python3
"""Freeze complete Bold v6 source or active revision artifacts."""
from __future__ import annotations
import argparse,hashlib,json,shutil
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
 p=argparse.ArgumentParser(); p.add_argument('--revision',required=True); p.add_argument('--source',action='store_true'); p.add_argument('--force',action='store_true'); a=p.parse_args()
 target=ROOT/'checkpoints'/'bold-v6'/a.revision; manifest=target/'manifest.json'
 if manifest.exists() and not a.force: raise SystemExit(str(target)+' exists; use --force')
 common={
  'redrawn.sfd':ROOT/'fontforge'/'redrawn.sfd','regular-bold-base.sfd':ROOT/'fontforge'/'regular-bold-base.sfd','bold-raw.sfd':ROOT/'fontforge'/'bold-raw.sfd',
  'bold-manual-decisions.json':ROOT/'qa'/'bold-manual-decisions.json','bold-ready-to-pass.json':ROOT/'qa'/'bold-ready-to-pass.json',
 }
 if a.source:
  artifacts={**common,'bold.sfd':ROOT/'fontforge'/'bold.sfd','bold.ttf':ROOT/'qa'/'assets'/'bold.ttf','bold-report.csv':ROOT/'qa'/'assets'/'bold-report.csv','bold-report.json':ROOT/'qa'/'assets'/'bold-report.json'}
  version='bold-v6-source-v1'
 else:
  artifacts={**common,'bold.sfd':ROOT/'fontforge'/'bold.sfd','bold-v6.sfd':ROOT/'fontforge'/'bold-v6.sfd','bold-v6.ttf':ROOT/'qa'/'assets'/'bold-v6.ttf','bold-report.csv':ROOT/'qa'/'assets'/'bold-report.csv','bold-report.json':ROOT/'qa'/'assets'/'bold-report.json','bold-v6-repairs.json':ROOT/'qa'/'assets'/'bold-v6-repairs.json','qa-index.html':ROOT/'qa'/'index.html','build_bold_v6.py':ROOT/'tools'/'build_bold_v6.py','refresh_bold_qa.py':ROOT/'tools'/'refresh_bold_qa.py','migrate_bold_v6_decisions.py':ROOT/'tools'/'migrate_bold_v6_decisions.py'}
  version='bold-v6-full-revision-v1'
 target.mkdir(parents=True,exist_ok=True); hashes={}
 for name,src in artifacts.items():
  if not src.is_file(): raise SystemExit('missing '+str(src))
  dst=target/name; shutil.copy2(src,dst); hashes[name]=sha(dst)
 payload={'version':version,'revision':a.revision,'created_at':datetime.now(timezone.utc).isoformat(),'candidate_promoted':False,'artifact_hashes':hashes}
 if not a.source:
  repairs=json.loads((ROOT/'qa'/'assets'/'bold-v6-repairs.json').read_text(encoding='utf-8')); payload['changed_glyphs']=repairs['changed_glyphs']; payload['changed_count']=repairs['changed_count']
 manifest.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8')
 print(f'frozen={len(hashes)} target={target}')
 return 0
if __name__=='__main__': raise SystemExit(main())
