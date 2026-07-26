import fontforge

font = fontforge.open("qa/assets/original.ttf")
for name in ("uni0414", "uni0416", "A", "M", "X", "I", "uni0425", "uni041C"):
    glyph = font[name]
    print(name, "width", glyph.width, "bbox", glyph.boundingBox(), "points", sum(len(c) for c in glyph.foreground), "contours", len(glyph.foreground))
font.close()
