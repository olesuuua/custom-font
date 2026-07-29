#!/usr/bin/env ffpython
"""Migrate reviewed Bold v2 decisions to hash-bound Bold v3 decisions."""
from __future__ import annotations
import csv, json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
import fontforge
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.font-deps'),str(ROOT/'tools')]
import redraw_font as rf
import simplify_font as sf
OLD=ROOT/'checkpoints'/'bold-v2-reviewed'
REPORT=ROOT/'qa'/'assets'/'bold-report.csv'
DECISIONS=ROOT/'qa'/'bold-manual-decisions.json'
READY=ROOT/'qa'/'bold-ready-to-pass.json'
VERSION='bold-manual-review-v3'
METRICS='bold-metrics-v3'

def read_json(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def write_atomic(path,value):
    fd,tmp=tempfile.mkstemp(prefix=path.stem+'-',suffix='.json',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as h:
            json.dump(value,h,indent=2,sort_keys=True,ensure_ascii=False); h.write('\n'); h.flush(); os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def sets(layer):
    pen=sf.FlattenPen(error=1.0,spacing=4.0); layer.draw(pen)
    if pen.points: pen.endPath()
    return pen.contours

def bbox(a,b):
    values=[c for c in a+b if c]
    if not values:return (-16,-16,16,16)
    x0,y0,x1,y1=sf.bbox_of_sets(values); pad=max(8,((x1-x0)**2+(y1-y0)**2)**.5*.04)
    return x0-pad,y0-pad,x1+pad,y1+pad

def ious(a,b):
    aa,bb=sets(a),sets(b); box=bbox(aa,bb); result={}
    for size in (64,128):
        am=sf.rasterize(aa,box,size) if aa else bytes(size*size)
        bm=sf.rasterize(bb,box,size) if bb else bytes(size*size)
        result[size]=sf.raster_metrics(am,bm)['ink_iou']
    return result

def repair_names():
    names=set()
    p=read_json(ROOT/'qa'/'assets'/'bold-v3-cleanup.json')
    names.update(r['glyph'] for r in p.get('glyphs',[]) if r.get('status')=='ok')
    p=read_json(ROOT/'qa'/'assets'/'bold-v3-geometric-repairs.json')
    names.update(p.get('glyphs',{}))
    p=read_json(ROOT/'qa'/'assets'/'bold-v3-target-fixes.json')
    names.update(r['glyph'] for r in p.get('glyphs',[]) if r.get('status')=='ok')
    return names

def main():
    with REPORT.open(encoding='utf-8-sig',newline='') as h: current={r['glyph']:r for r in csv.DictReader(h)}
    with (OLD/'bold-report.csv').open(encoding='utf-8-sig',newline='') as h: previous={r['glyph']:r for r in csv.DictReader(h)}
    old_values=read_json(OLD/'bold-manual-decisions.json').get('decisions',{})
    old_font=fontforge.open(str(OLD/'bold.sfd')); new_font=fontforge.open(str(ROOT/'fontforge'/'bold.sfd'))
    old_glyphs={g.glyphname:g for g in old_font.glyphs()}; new_glyphs={g.glyphname:g for g in new_font.glyphs()}
    repairs=repair_names(); migrated={}; ready={}; changed=0; invalidated=0
    now=datetime.now(timezone.utc).isoformat()
    try:
        for name,decision in old_values.items():
            if name not in current or name not in previous: continue
            row=current[name]; old_row=previous[name]
            outline_changed=old_row['bold_hash']!=row['bold_hash']
            if not outline_changed:
                migrated[name]={**decision,'regular_hash':row['regular_hash'],'base_hash':row['base_hash'],'bold_hash':row['bold_hash'],'metrics_version':METRICS}
                continue
            changed+=1
            qualifies=False; scores={64:0.0,128:0.0}
            if decision.get('status')=='almost_done' and name in repairs and not row.get('manual_blockers'):
                scores=ious(old_glyphs[name].foreground,new_glyphs[name].foreground)
                qualifies=scores[64]>=.995 and scores[128]>=.995
            if qualifies:
                migrated[name]={
                    'status':'almost_done','regular_hash':row['regular_hash'],'base_hash':row['base_hash'],
                    'bold_hash':row['bold_hash'],'metrics_version':METRICS,'updated_at':now,
                    'ready_to_pass':True,'previous_bold_hash':old_row['bold_hash'],
                    'repair_iou_64':scores[64],'repair_iou_128':scores[128],
                }
                ready[name]={'previous_status':'almost_done','repair_iou_64':scores[64],'repair_iou_128':scores[128],'bold_hash':row['bold_hash']}
            else: invalidated+=1
    finally:
        old_font.close(); new_font.close()
    write_atomic(DECISIONS,{'version':VERSION,'metrics_version':METRICS,'decisions':migrated})
    write_atomic(READY,{'version':'bold-ready-to-pass-v1','metrics_version':METRICS,'created_at':now,'glyphs':ready})
    print(json.dumps({'preserved_or_rebound':len(migrated),'changed_reviewed':changed,'ready_to_pass':len(ready),'invalidated':invalidated},sort_keys=True))
if __name__=='__main__': raise SystemExit(main())