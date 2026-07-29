#!/usr/bin/env ffpython
"""Verify the Regular/Bold family artifacts, report, metadata, and checkpoint."""
from __future__ import annotations
import csv, hashlib, json, sys
from pathlib import Path
import fontforge

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import redraw_font as rf
REGULAR=ROOT/'fontforge'/'redrawn.sfd'; BOLD=ROOT/'fontforge'/'bold.sfd'
REPORT=ROOT/'qa'/'assets'/'bold-report.csv'; META=ROOT/'qa'/'assets'/'bold-report.json'
DECISIONS=ROOT/'qa'/'bold-manual-decisions.json'; CHECKPOINT=ROOT/'checkpoints'/'bold-v1'

def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
 return h.hexdigest()
def count(layer): return sum(len(c) for c in layer)

def main():
 errors=[]
 for path in (REGULAR,BOLD,REPORT,META,DECISIONS,CHECKPOINT/'manifest.json'):
  if not path.is_file(): errors.append('missing '+str(path))
 if errors:
  print('\n'.join(errors)); return 1
 r=fontforge.open(str(REGULAR)); b=fontforge.open(str(BOLD))
 try:
  rg=list(r.glyphs()); bg={g.glyphname:g for g in b.glyphs()}
  if len(rg)!=340 or len(bg)!=340: errors.append('expected 340 glyphs')
  if set(g.glyphname for g in rg)!=set(bg): errors.append('glyph sets differ')
  outlined=sum(count(g.foreground)>0 for g in rg)
  if outlined!=334: errors.append('expected 334 outlined glyphs')
  for g in rg:
   other=bg.get(g.glyphname)
   if not other: continue
   if g.unicode!=other.unicode: errors.append(g.glyphname+' unicode differs')
   if bool(count(g.foreground))!=bool(count(other.foreground)): errors.append(g.glyphname+' empty state differs')
  if (r.fontname,r.fullname,r.weight,r.os2_weight,r.os2_stylemap,r.macstyle,r.version)!=('OlesuasHand-Regular','Olesuas Hand Regular','Regular',400,64,0,'001.001'):
   errors.append('Regular metadata differs')
  if (b.fontname,b.fullname,b.weight,b.os2_weight,b.os2_stylemap,b.macstyle,b.version)!=('OlesuasHand-Bold','Olesuas Hand Bold','Bold',700,32,1,'001.001'):
   errors.append('Bold metadata differs')
  if len(r.gpos_lookups)!=len(b.gpos_lookups) or len(r.gsub_lookups)!=len(b.gsub_lookups): errors.append('OpenType lookup coverage differs')
  rows=list(csv.DictReader(REPORT.open(encoding='utf-8-sig',newline='')))
  if len(rows)!=340: errors.append('report does not contain 340 rows')
  for row in rows:
   rglyph=r[row['glyph']]; bglyph=b[row['glyph']]
   if row['regular_hash']!=rf.layer_hash(rglyph.foreground): errors.append(row['glyph']+' regular hash stale')
   if row['bold_hash']!=rf.layer_hash(bglyph.foreground): errors.append(row['glyph']+' bold hash stale')
   if int(row['regular_points'])!=count(rglyph.foreground) or int(row['bold_points'])!=count(bglyph.foreground): errors.append(row['glyph']+' point report stale')
  meta=json.loads(META.read_text(encoding='utf-8'))
  if meta['regular_sfd_hash']!=digest(REGULAR) or meta['bold_sfd_hash']!=digest(BOLD): errors.append('report metadata hashes stale')
  decisions=json.loads(DECISIONS.read_text(encoding='utf-8-sig'))
  by_name={row['glyph']:row for row in rows}
  for name,value in decisions.get('decisions',{}).items():
   row=by_name.get(name)
   if not row or value.get('regular_hash')!=row['regular_hash'] or value.get('bold_hash')!=row['bold_hash'] or value.get('metrics_version')!=meta['version']: errors.append(name+' decision is stale')
 finally:
  r.close(); b.close()
 manifest=json.loads((CHECKPOINT/'manifest.json').read_text(encoding='utf-8'))
 for name,expected in manifest.get('artifact_hashes',{}).items():
  path=CHECKPOINT/name
  if not path.is_file() or digest(path)!=expected: errors.append('checkpoint hash differs: '+name)
 print('glyphs=340; outlined=334; errors={}'.format(len(errors)))
 for error in errors[:50]: print(error)
 return 1 if errors else 0
if __name__=='__main__': raise SystemExit(main())
