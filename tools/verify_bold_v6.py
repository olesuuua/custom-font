#!/usr/bin/env ffpython
"""Verify the full Bold v6 candidate, review migration, and simplified QA."""
from __future__ import annotations
import csv,hashlib,json,sys
from collections import Counter
from pathlib import Path
import fontforge
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import redraw_font as rf
import repair_bold_v4 as v4
SOURCE=ROOT/'fontforge'/'bold.sfd'; V6=ROOT/'fontforge'/'bold-v6.sfd'; REPAIRS=ROOT/'qa'/'assets'/'bold-v6-repairs.json'; META=ROOT/'qa'/'assets'/'bold-report.json'; REPORT=ROOT/'qa'/'assets'/'bold-report.csv'; DECISIONS=ROOT/'qa'/'bold-manual-decisions.json'; QA=ROOT/'qa'/'index.html'; SOURCE_DECISIONS=ROOT/'checkpoints'/'bold-v6'/'revision-00-source'/'bold-manual-decisions.json'
QUOTES=['quoteleft','quoteright','quotesinglbase','quotereversed','quotedblleft','quotedblright','quotedblbase','uni201F']; IDENTITY={'quoteleft','quotedblleft'}
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def sig(c,rotate=False):
 b=c.boundingBox(); w=max(b[2]-b[0],1e-9); h=max(b[3]-b[1],1e-9); values=[]
 for p in c:
  x=(p.x-b[0])/w; y=(p.y-b[1])/h
  if rotate: x,y=1-x,1-y
  values.append((round(x,4),round(y,4),bool(p.on_curve)))
 return values
def main():
 errors=[]; repairs=load(REPAIRS); changed=set(repairs['changed_glyphs']); old_dec=load(SOURCE_DECISIONS)['decisions']; current_dec=load(DECISIONS)
 source=fontforge.open(str(SOURCE)); candidate=fontforge.open(str(V6))
 try:
  names=[g.glyphname for g in source.glyphs()]; vnames=[g.glyphname for g in candidate.glyphs()]
  if names!=vnames or len(names)!=340: errors.append('glyph order/count differs')
  actual=set()
  for n in names:
   if source[n].unicode!=candidate[n].unicode: errors.append(n+' cmap changed')
   if source[n].width!=candidate[n].width: errors.append(n+' advance changed')
   if rf.layer_hash(source[n].foreground)!=rf.layer_hash(candidate[n].foreground): actual.add(n)
  if actual!=changed: errors.append('repair changed set differs from font')
  if candidate.version!='1.003': errors.append('candidate version is not 1.003')
  passed={n for n,d in old_dec.items() if d.get('status')=='pass'}
  touched_passed=passed&changed
  if touched_passed: errors.append('passed glyphs changed: '+','.join(sorted(touched_passed)))
  for n in changed:
   defects=v4.structural(candidate[n].foreground)
   if any(defects[k] for k in ('intersections','open_contours','invalid_handles')): errors.append(n+' structural defect')
  box=candidate['uni2010'].foreground.boundingBox()
  if candidate['uni2010'].width!=431: errors.append('uni2010 advance differs')
  if abs((box[2]-box[0])-300)>1: errors.append('uni2010 ink width differs')
  if abs((box[3]-box[1])-102)>2: errors.append('uni2010 thickness differs')
  proto=list(candidate['quoteleft'].foreground)[0]; ps=sig(proto)
  for n in QUOTES:
   contours=list(candidate[n].foreground); expected=1 if n in {'quoteleft','quoteright','quotesinglbase','quotereversed'} else 2
   if len(contours)!=expected: errors.append(n+' component count differs'); continue
   target=ps if n in IDENTITY else sig(proto,rotate=True)
   for i,c in enumerate(contours):
    if sig(c)!=target: errors.append(f'{n} contour {i} is not exact prototype transform')
 finally: source.close(); candidate.close()
 if repairs.get('candidate_sha256')!=sha(V6): errors.append('repair candidate hash differs')
 meta=load(META)
 if meta.get('version')!='bold-metrics-v6' or meta.get('bold_sfd_hash')!=sha(V6): errors.append('QA metadata differs')
 with REPORT.open(encoding='utf-8-sig',newline='') as h: rows={r['glyph']:r for r in csv.DictReader(h)}
 if len(rows)!=340: errors.append('report row count differs')
 report_changed={n for n,r in rows.items() if r.get('changed_in_version')=='true'}
 if report_changed!=changed: errors.append('report changed set differs')
 if current_dec.get('version')!='bold-manual-review-v6' or current_dec.get('metrics_version')!='bold-metrics-v6': errors.append('decision contract differs')
 active=current_dec.get('decisions',{})
 if changed&set(active): errors.append('changed glyph retained previous decision')
 for n,d in active.items():
  if d.get('bold_hash')!=rows[n]['bold_hash'] or d.get('metrics_version')!='bold-metrics-v6': errors.append(n+' decision binding differs')
 html=QA.read_text(encoding='utf-8')
 forbidden=['toggleBase','toggleRaw','toggleBatch1','toggleBatch2','togglePilot','Pilot alternatives','Batch 1','Batch 2','bold-v5-pilot']
 for token in forbidden:
  if token in html: errors.append('historical QA control remains: '+token)
 required=['Changed in current version','Structural blocker','Counter or white-space problem','Smoothness problem','Spacing problem','No warnings']
 for token in required:
  if token not in html: errors.append('QA filter missing: '+token)
 counts=Counter(d['status'] for d in active.values()); counts['unreviewed']=340-len(active)
 print(f"glyphs=340 changed={len(changed)} pass={counts['pass']} almost={counts['almost_done']} rework={counts['needs_rework']} unreviewed={counts['unreviewed']} errors={len(errors)}")
 for e in errors: print(e)
 return 1 if errors else 0
if __name__=='__main__': raise SystemExit(main())
