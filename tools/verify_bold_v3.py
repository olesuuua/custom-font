#!/usr/bin/env ffpython
"""Verify Bold v3 repaired outlines, QA bindings, and invariant font data."""
from __future__ import annotations
import csv, hashlib, json, sys
from pathlib import Path
import fontforge
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools')]
import redraw_font as rf
REGULAR=ROOT/'fontforge'/'redrawn.sfd'; BASE=ROOT/'fontforge'/'regular-bold-base.sfd'; RAW=ROOT/'fontforge'/'bold-raw.sfd'; BOLD=ROOT/'fontforge'/'bold.sfd'
REPORT=ROOT/'qa'/'assets'/'bold-report.csv'; META=ROOT/'qa'/'assets'/'bold-report.json'; DECISIONS=ROOT/'qa'/'bold-manual-decisions.json'; READY=ROOT/'qa'/'bold-ready-to-pass.json'
OLD=ROOT/'checkpoints'/'bold-v2-reviewed'; BASE_META=ROOT/'qa'/'assets'/'bold-base-cleanup.json'
EXPECTED_REGULAR='5f0c82491cb499a1eec606c905b449129a48479703b12d5d0c56152d68fba418'; EXPECTED_BASE='9559efcd3007580146ed089a5eec3c4dd76ffd04c64c9d99445fff0d7bdb62e2'
FOCAL={'parenleft','parenright','quotesingle','comma','period','colon','asterisk','uni041D','uni0427','uni043D','eight','Z','Zeta','uni20A6','uni2116','exclam','semicolon'}
WEIGHTS={'eight':(1.40,1.50),'Z':(1.45,1.55),'Zeta':(1.45,1.55),'uni20A6':(1.35,1.45),'uni2116':(1.35,1.45)}
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def points(layer): return sum(len(c) for c in layer)
def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def main():
 errors=[]
 required=[REGULAR,BASE,RAW,BOLD,REPORT,META,DECISIONS,READY,BASE_META,OLD/'bold.sfd',OLD/'bold-report.csv',OLD/'bold-manual-decisions.json']
 for p in required:
  if not p.is_file(): errors.append('missing '+str(p))
 if errors: print('\n'.join(errors)); return 1
 if digest(REGULAR)!=EXPECTED_REGULAR: errors.append('authoritative Regular changed')
 if digest(BASE)!=EXPECTED_BASE: errors.append('cleaned base changed')
 fonts=[fontforge.open(str(p)) for p in (REGULAR,BASE,RAW,BOLD)]; regular,base,raw,bold=fonts
 try:
  names=[[g.glyphname for g in f.glyphs()] for f in fonts]
  if any(len(x)!=340 for x in names): errors.append('all sources must contain 340 glyphs')
  if any(x!=names[0] for x in names[1:]): errors.append('glyph order differs')
  maps=[{g.glyphname:g.unicode for g in f.glyphs()} for f in fonts]
  if any(x!=maps[0] for x in maps[1:]): errors.append('cmap differs')
  if sum(points(g.foreground)>0 for g in regular.glyphs())!=334 or sum(points(g.foreground)>0 for g in base.glyphs())!=334: errors.append('outlined glyph count differs')
  for name in names[0]:
   if regular[name].width!=base[name].width: errors.append(name+' cleaned-base advance width differs')
   if raw[name].width!=bold[name].width: errors.append(name+' repaired Bold advance width differs from raw Bold')
  for f in (base,raw,bold):
   if list(f.gpos_lookups)!=list(regular.gpos_lookups) or list(f.gsub_lookups)!=list(regular.gsub_lookups): errors.append('OpenType lookup coverage differs')
  if (bold.familyname,bold.weight,bold.os2_weight,bold.macstyle,bold.italicangle)!=('Olesuas Hand','Bold',700,1,0.0): errors.append('Bold metadata differs')
  for name in FOCAL:
   layer=bold[name].foreground
   if any(c.selfIntersects() for c in layer) or any(not c.closed for c in layer) or rf.invalid_handles(layer): errors.append(name+' retains outline defects')
  geometric=load(ROOT/'qa'/'assets'/'bold-v3-geometric-repairs.json')
  for name in geometric.get('dot_glyphs',[]):
   layer=bold[name].foreground; expected=geometric['glyphs'][name].get('dot_count',1)
   ellipses=sum(sum(1 for p in c if p.on_curve)==4 for c in layer)
   if ellipses<expected: errors.append(name+' does not retain required four-anchor dot ellipse(s)')
 finally:
  for f in fonts: f.close()
 with REPORT.open(encoding='utf-8-sig',newline='') as h: rows={r['glyph']:r for r in csv.DictReader(h)}
 if len(rows)!=340: errors.append('QA report must contain 340 rows')
 expected_fields={'ready_to_pass','intentional_counter_fills','short_segments','curvature_reversals','repair_method','pre_repair_points','repair_iou_64','repair_iou_128'}
 if rows and not expected_fields.issubset(next(iter(rows.values()))): errors.append('v3 report fields missing')
 for name in FOCAL:
  if rows[name]['manual_blockers']: errors.append(name+' still has Pass blocker: '+rows[name]['manual_blockers'])
 for name in {'asterisk','uni041D','uni0427','uni043D'}:
  if rows[name]['intentional_counter_fills']!='true': errors.append(name+' intentional fill policy missing')
 for name,(low,high) in WEIGHTS.items():
  ratio=float(rows[name]['ink_ratio_128'])
  if not low<=ratio<=high: errors.append('{} weight ratio {:.3f} outside target'.format(name,ratio))
 meta=load(META)
 if meta.get('version')!='bold-metrics-v3' or meta.get('regular_sfd_hash')!=EXPECTED_REGULAR or meta.get('base_sfd_hash')!=EXPECTED_BASE or meta.get('bold_sfd_hash')!=digest(BOLD): errors.append('QA metadata stale')
 decisions=load(DECISIONS); ready=load(READY).get('glyphs',{})
 if decisions.get('version')!='bold-manual-review-v3' or decisions.get('metrics_version')!='bold-metrics-v3': errors.append('decision document version differs')
 if meta.get('ready_to_pass_count')!=len(ready): errors.append('Ready-to-Pass count stale')
 for name,entry in ready.items():
  d=decisions.get('decisions',{}).get(name); row=rows.get(name,{})
  if not d or d.get('status')!='almost_done' or d.get('status')=='pass': errors.append(name+' Ready status was auto-promoted or lost')
  elif d.get('bold_hash')!=row.get('bold_hash') or row.get('ready_to_pass')!='true' or row.get('manual_blockers'): errors.append(name+' Ready binding is stale or blocked')
  if min(float(entry.get('repair_iou_64',0)),float(entry.get('repair_iou_128',0)))<.995: errors.append(name+' Ready IoU below threshold')
 old_decisions=load(OLD/'bold-manual-decisions.json').get('decisions',{})
 with (OLD/'bold-report.csv').open(encoding='utf-8-sig',newline='') as h: old_rows={r['glyph']:r for r in csv.DictReader(h)}
 for name,d in old_decisions.items():
  if name in rows and old_rows[name]['bold_hash']==rows[name]['bold_hash']:
   current=decisions.get('decisions',{}).get(name)
   if not current or current.get('status')!=d.get('status'): errors.append(name+' unchanged decision not preserved')
 print('glyphs=340; outlined=334; ready={}; errors={}'.format(len(ready),len(errors)))
 for error in errors[:150]: print(error)
 return 1 if errors else 0
if __name__=='__main__': raise SystemExit(main())