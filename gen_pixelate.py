"""把你自己的图片像素化转绘成桌宠精灵（八方旅人式）。

用法: 把你的原图（Q 版人物、白底 JPG/PNG）命名为 original_image.jpg
     放在本脚本同目录，运行 python gen_pixelate.py。
流程: 抠白底 → 降采样到 GRID 网格（BOX 均值）→ 量化到有限调色板
     → 轮廓 1px 深色描边 → NEAREST 整数倍放大（锐利色块）。
输出: pet_sprite.png / pet_sprite_blink.png / preview_grid.png（校准用）
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

HERE = Path(__file__).parent
SRC = HERE / "original_image.jpg"      # 放你自己的原图
OUT = HERE / "pet_sprite.png"
OUT_BLINK = HERE / "pet_sprite_blink.png"
PREVIEW = HERE / "preview_grid.png"

GRID = 48          # 像素网格（原图 96 → 48，脸部/眼睛仍可辨）
SCALE = 4          # NEAREST 放大倍数 → 192x192
COLORS = 26        # 调色板色数（八方旅人式有限色板）
DARK = (44, 32, 62, 255)   # 轮廓描边深紫

# 1) 原图 + 抠白底（floodfill 只清背景，保住白衣服）
rgba = Image.open(SRC).convert("RGBA")
for seed in ((0, 0), (rgba.width - 1, 0), (0, rgba.height - 1),
             (rgba.width - 1, rgba.height - 1)):
    ImageDraw.floodfill(rgba, seed, (0, 0, 0, 0), thresh=48)
r, g, b, a = rgba.split()
a = a.filter(ImageFilter.MinFilter(3))     # 去白毛边
rgba = Image.merge("RGBA", (r, g, b, a))

# 1b) 清除贴着透明边缘的白色 halo（两轮迭代，只杀贴边白，白衣服不受影响）
for _ in range(2):
    p2 = rgba.load()
    ach = rgba.getchannel("A").load()
    kill = []
    for yy in range(rgba.height):
        for xx in range(rgba.width):
            if ach[xx, yy] == 0:
                continue
            rr, gg, bb, _ = p2[xx, yy]
            if rr > 185 and gg > 185 and bb > 185:
                for nx, ny in ((xx - 1, yy), (xx + 1, yy),
                               (xx, yy - 1), (xx, yy + 1)):
                    if 0 <= nx < rgba.width and 0 <= ny < rgba.height \
                            and ach[nx, ny] < 200:
                        kill.append((xx, yy))
                        break
    for xx, yy in kill:
        p2[xx, yy] = (0, 0, 0, 0)

# 2) 降采样到网格 + alpha 二值化
small = rgba.resize((GRID, GRID), Image.BOX)
small = small.convert("RGBA")
sr, sg, sb, sa = small.split()
sa = sa.point(lambda v: 255 if v > 110 else 0)

# 3) 颜色提亮 + 量化（有限调色板）
rgb = Image.merge("RGB", (sr, sg, sb))
rgb = ImageEnhance.Color(rgb).enhance(1.22)
rgb = ImageEnhance.Brightness(rgb).enhance(1.06)
rgb = ImageEnhance.Contrast(rgb).enhance(1.04)
quant = rgb.quantize(colors=COLORS, method=Image.Quantize.MEDIANCUT)
quant = quant.convert("RGBA")
quant.putalpha(sa)


# 4) 轮廓描边：不透明且四邻有透明的像素 → 深紫
def outline(im):
    px, apx2 = im.load(), im.getchannel("A").load()
    pts = []
    for y in range(GRID):
        for x in range(GRID):
            if apx2[x, y] == 0:
                continue
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < GRID and 0 <= ny < GRID:
                    if apx2[nx, ny] == 0:
                        pts.append((x, y))
                        break
    for x, y in pts:
        px[x, y] = DARK
    return im


# 5) 输出睁眼帧（NEAREST 放大，色块锐利）
out = outline(quant).resize((GRID * SCALE, GRID * SCALE), Image.NEAREST)
out.save(OUT)

# 6) 闭眼帧：瞳孔/眼白填肤色但保留眼眶深色描边，闭眼线紧贴上睑
#   （上睑+闭眼线相邻变"厚眼皮"，下方保留下睑 → 自然的一条缝，不再吓人）
blink = quant.copy()
bp = blink.load()
skin = bp[20, 29][:3]
LID = DARK
for x1, x2, ly, y2 in ((14, 22, 20, 25), (24, 33, 22, 27)):
    for y in range(ly, y2 + 1):
        for x in range(x1, x2 + 1):
            r, g, b, al = bp[x, y]
            if not al or r + g + b < 210:      # 深描边结构保留
                continue
            blue = b > r + 55 and b > 120
            white = r > 210 and g > 210 and b > 210
            if blue or white:
                bp[x, y] = (*skin, 255)
    for x in range(x1, x2 + 1):                # 闭眼线：贴上睑整行
        if bp[x, ly][3]:
            bp[x, ly] = LID
blink_out = outline(blink).resize((GRID * SCALE, GRID * SCALE),
                                  Image.NEAREST)
blink_out.save(OUT_BLINK)

# 6) 校准预览（品红底 + 原始网格坐标标注）
pv = Image.new("RGBA", out.size, (255, 0, 255, 255))
pv.alpha_composite(out)
pd = ImageDraw.Draw(pv)
for gx in range(0, GRID, 4):
    pd.line([(gx * SCALE, 0), (gx * SCALE, out.height)], fill=(0, 0, 0, 70))
    pd.line([(0, gx * SCALE), (out.width, gx * SCALE)], fill=(0, 0, 0, 70))
    if gx % 8 == 0:
        pd.text((gx * SCALE + 2, 2), str(gx), fill=(0, 0, 0, 210))
        pd.text((2, gx * SCALE + 2), str(gx), fill=(0, 0, 0, 210))
pv.convert("RGB").save(PREVIEW)
print("sprite:", out.size, "| grid:", GRID)
