#!/usr/bin/env ffpython
"""Replace v3 isolated dots with native four-on-curve quadratic ellipses."""
from __future__ import annotations
import hashlib,json,math,sys
from pathlib import Path
import fontforge
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.font-deps'),str(ROOT/'tools')]
import repair_bold_geometric_v3 as g
BASE=ROOT/'fontforge'/'regular-bold-base.sfd'; BOLD=ROOT/'fontforge'/'bold.sfd'; OUT=ROOT/'qa'/'assets'/'bold-v3-dot-repairs.json'
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def ellipse(cx,cy,diameter):
 r=diameter/2.0; c=fontforge.contour(); c.is_quadratic=True
 c.moveTo(cx,cy+r); c.quadraticTo((cx+r,cy+r),(cx+r,cy)); c.quadraticTo((cx+r,cy-r),(cx,cy-r)); c.quadraticTo((cx-r,cy-r),(cx-r,cy)); c.quadraticTo((cx-r,cy+r),(cx,cy+r)); c.closed=True
 for p in c:
  if p.on_curve:p.type=fontforge.splineCurve
 return c
def replace(base_layer,bold_layer):
 base_dots=g.dot_contours(base_layer); contours=list(bold_layer); boxes=[g.contour_bbox(c) for c in contours]; remove=set(); replacements=[]
 for _,box in base_dots:
  cx,cy=g.center(box); width,height=box[2]-box[0],box[3]-box[1]
  choices=[(math.hypot(g.center(b)[0]-cx,g.center(b)[1]-cy),i) for i,b in enumerate(boxes) if i not in remove]
  if not choices:continue
  distance,index=min(choices)
  if distance>max(100.0,max(width,height)):continue
  remove.add(index); replacements.append(ellipse(cx,cy,max(width,height)+40.0))
 result=fontforge.layer(); result.is_quadratic=True
 for i,c in enumerate(contours):
  if i not in remove: result+=c.dup()
 for c in replacements:result+=c
 return result,len(replacements)
def main():
 base=fontforge.open(str(BASE)); bold=fontforge.open(str(BOLD)); old_hash=digest(BOLD); rows={}
 names=json.loads((ROOT/'qa'/'assets'/'bold-v3-geometric-repairs.json').read_text(encoding='utf-8'))['dot_glyphs']
 try:
  for name in names:
   old=bold[name].foreground.dup(); candidate,count=replace(base[name].foreground,old)
   metrics=g.raster_comparison(old,candidate); structure=g.structural(candidate)
   if not count or structure['intersections'] or structure['open_contours'] or structure['invalid_handles']: raise RuntimeError(name+' four-anchor dot repair invalid')
   bold[name].foreground=candidate
   rows[name]={'method':'four-anchor-quadratic-dot','dot_count':count,'old_points':g.point_count(old),'new_points':g.point_count(candidate),'iou_64':metrics[64]['iou'],'iou_128':metrics[128]['iou']}
  bold.save(str(BOLD))
 finally: base.close(); bold.close()
 payload={'version':'bold-v3-four-anchor-dots','bold_before_sha256':old_hash,'bold_sha256':digest(BOLD),'glyphs':rows}
 OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8'); print(json.dumps({'changed':len(rows),'bold_sha256':payload['bold_sha256']},sort_keys=True))
if __name__=='__main__':raise SystemExit(main())