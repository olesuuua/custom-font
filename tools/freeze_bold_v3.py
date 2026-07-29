#!/usr/bin/env python3
"""Freeze Bold v3 sources, builds, repair reports, review state, and hashes."""
from __future__ import annotations
import argparse,hashlib,json,shutil
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; CHECKPOINT=ROOT/'checkpoints'/'bold-v3'
ARTIFACTS={
 'redrawn.sfd':ROOT/'fontforge'/'redrawn.sfd','regular-bold-base.sfd':ROOT/'fontforge'/'regular-bold-base.sfd','bold-raw.sfd':ROOT/'fontforge'/'bold-raw.sfd','bold.sfd':ROOT/'fontforge'/'bold.sfd',
 'redrawn.ttf':ROOT/'qa'/'assets'/'redrawn.ttf','regular-bold-base.ttf':ROOT/'qa'/'assets'/'regular-bold-base.ttf','bold-raw.ttf':ROOT/'qa'/'assets'/'bold-raw.ttf','bold.ttf':ROOT/'qa'/'assets'/'bold.ttf',
 'bold-base-cleanup.csv':ROOT/'qa'/'assets'/'bold-base-cleanup.csv','bold-base-cleanup.json':ROOT/'qa'/'assets'/'bold-base-cleanup.json','bold-build.json':ROOT/'qa'/'assets'/'bold-build.json',
 'bold-report.csv':ROOT/'qa'/'assets'/'bold-report.csv','bold-report.json':ROOT/'qa'/'assets'/'bold-report.json','bold-manual-decisions.json':ROOT/'qa'/'bold-manual-decisions.json','bold-ready-to-pass.json':ROOT/'qa'/'bold-ready-to-pass.json',
 'bold-v3-cleanup.json':ROOT/'qa'/'assets'/'bold-v3-cleanup.json','bold-v3-geometric-repairs.json':ROOT/'qa'/'assets'/'bold-v3-geometric-repairs.json','bold-v3-hole-policy.json':ROOT/'qa'/'assets'/'bold-v3-hole-policy.json','bold-v3-target-fixes.json':ROOT/'qa'/'assets'/'bold-v3-target-fixes.json','bold-v3-dot-repairs.json':ROOT/'qa'/'assets'/'bold-v3-dot-repairs.json','bold-v3-dot-protection.json':ROOT/'qa'/'assets'/'bold-v3-dot-protection.json',
 'qa-index.html':ROOT/'qa'/'index.html','README.md':ROOT/'README.md',
}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--force',action='store_true');args=ap.parse_args()
 if (CHECKPOINT/'manifest.json').exists() and not args.force:raise SystemExit('Bold v3 checkpoint already exists; use --force to replace it intentionally')
 CHECKPOINT.mkdir(parents=True,exist_ok=True); hashes={}
 for name,source in ARTIFACTS.items():
  if not source.is_file():raise SystemExit('Missing checkpoint artifact: '+str(source))
  target=CHECKPOINT/name;shutil.copy2(source,target);hashes[name]=sha(target)
 manifest={'version':'bold-v3','created_at':datetime.now(timezone.utc).isoformat(),'source':'fontforge/redrawn.sfd','cleaned_base':'fontforge/regular-bold-base.sfd','reviewed_predecessor':'checkpoints/bold-v2-reviewed','settings':{'weight_change':40,'type':'LCG','counter_type':'retain','metrics_version':'bold-metrics-v3','decision_version':'bold-manual-review-v3','ready_iou_minimum':{'64':.995,'128':.995},'intentional_fill_always':['asterisk','uni041D','uni0427','uni043D'],'counter_area_minimum':.75,'counter_width_proxy_minimum':.70,'point_growth_warning':{'ratio':3,'delta':100}},'artifact_hashes':hashes}
 (CHECKPOINT/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8');print('Frozen {} artifacts in {}'.format(len(hashes),CHECKPOINT))
if __name__=='__main__':raise SystemExit(main())