#!/usr/bin/env ffpython
"""Protect dot/stem separation and remove new dot-glyph micro-holes."""
from __future__ import annotations
import hashlib,json,sys
from pathlib import Path
import fontforge
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.font-deps'),str(ROOT/'tools')]
import repair_bold_geometric_v3 as g
BASE=ROOT/'fontforge'/'regular-bold-base.sfd'; BOLD=ROOT/'fontforge'/'bold.sfd'; OUT=ROOT/'qa'/'assets'/'bold-v3-dot-protection.json'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def clean_new_holes(layer):
 result=fontforge.layer(); result.is_quadratic=True; removed=0
 for contour in layer:
  if contour.isClockwise()==0:removed+=1
  else:result+=contour.dup()
 return result,removed
def contract_exclam(layer,factor):
 contours=list(layer); dots=[i for i,c in enumerate(contours) if sum(p.on_curve for p in c)==4 and (c.boundingBox()[2]-c.boundingBox()[0])<200]
 result=fontforge.layer(); result.is_quadratic=True
 body_top=max(c.boundingBox()[3] for i,c in enumerate(contours) if i not in dots)
 for i,c in enumerate(contours):
  d=c.dup()
  if i not in dots:d.transform((1,0,0,factor,0,body_top-factor*body_top))
  result+=d
 return result

def main():
 base=fontforge.open(str(BASE)); bold=fontforge.open(str(BOLD)); before=digest(BOLD); rows={}
 try:
  for name in ('semicolon','uni0451'):
   old=bold[name].foreground.dup(); candidate,removed=clean_new_holes(old)
   structure=g.structural(candidate); comparison=g.raster_comparison(old,candidate)
   if not removed or structure['intersections'] or structure['open_contours'] or structure['invalid_handles']:raise RuntimeError(name+' micro-hole cleanup invalid')
   bold[name].foreground=candidate; rows[name]={'method':'four-anchor-dot-plus-new-micro-hole-removal','removed_holes':removed,'old_points':g.point_count(old),'new_points':g.point_count(candidate),'iou_64':comparison[64]['iou'],'iou_128':comparison[128]['iou']}
  name='exclam'; old=bold[name].foreground.dup(); selected=None
  base_top=g.raster_comparison(base[name].foreground,base[name].foreground)
  for percent in range(98,74,-2):
   candidate=contract_exclam(old,percent/100.0); comparison=g.raster_comparison(base[name].foreground,candidate); structure=g.structural(candidate)
   topology_ok=all(comparison[size]['before_topology']==comparison[size]['after_topology'] for size in (128,256,512))
   if topology_ok and not structure['intersections'] and not structure['open_contours'] and not structure['invalid_handles']:
    selected=(percent,candidate,comparison,structure);break
  if selected is None:raise RuntimeError('exclam separation could not be protected')
  percent,candidate,comparison,structure=selected; old_comparison=g.raster_comparison(old,candidate); bold[name].foreground=candidate
  rows[name]={'method':'four-anchor-dot-plus-body-contract-{}pct'.format(percent),'contraction_percent':percent,'old_points':g.point_count(old),'new_points':g.point_count(candidate),'iou_64':old_comparison[64]['iou'],'iou_128':old_comparison[128]['iou']}
  bold.save(str(BOLD))
 finally:base.close();bold.close()
 payload={'version':'bold-v3-dot-component-protection','bold_before_sha256':before,'bold_sha256':digest(BOLD),'glyphs':rows}
 OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8');print(json.dumps({'changed':len(rows),'exclam_contraction_percent':rows['exclam']['contraction_percent'],'bold_sha256':payload['bold_sha256']},sort_keys=True))
if __name__=='__main__':raise SystemExit(main())