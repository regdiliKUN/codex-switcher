"""Codex 切换器 图标渲染实验室（参数化 + 20 轮迭代版本管理）。

设计母题
--------
底座  = 圆角方形（squircle），Codex / OpenAI 品牌青绿 #10A37F 垂直渐变
主体  = 上下两条反向的胶囊箭头，构成 A <-> B 互换 / 切换的经典意象
边界  = 深色外描边 + 顶部高光 + 底部内阴影 + 内圈亮边，
        保证在深色与浅色两种桌面背景下都能立住轮廓

小尺寸（<=48）走 simplified 分支：关掉高光/阴影/投影，加粗描边与箭头，
并按尺寸单独配参，保证 16px 仍可辨认。
"""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def mix(c1, c2, t):
    t = min(1.0, max(0.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def rounded_mask(n, box, r):
    m = Image.new('L', (n, n), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=max(0, int(r)), fill=255)
    return m


BASE = dict(
    corner=0.06, pad=0.05,
    bg_top=(16, 185, 129), bg_bot=(10, 127, 95),
    outline=(6, 78, 59), outline_w=0.0,
    inner_line=0.0,              # 内圈亮边 alpha
    glow=0.0, shade=0.0,
    arr_len=0.60, arr_thick=0.11, arr_gap=0.20,
    arr_head=0.16, arr_headw=1.8,
    arr_color=(255, 255, 255), arr_shadow=0.0, shadow_dy=0.010, shadow_blur=0.012,
    capsule=False,               # 杆尾是否圆头
    ss=4,
)


def _arrow_shape(d, pts_bar, tail_circle, head_pts):
    """画一条箭头：圆头尾 + 杆 + 三角头。"""
    if tail_circle:
        cx, cy, r = tail_circle
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=pts_bar.get('fill'))
    d.rectangle(pts_bar['box'], fill=pts_bar['fill'])
    d.polygon(head_pts, fill=pts_bar['fill'])


def render(size, p):
    p = dict(BASE, **p)
    ss = max(1, int(p['ss']))
    n = size * ss
    pad = int(p['pad'] * n)
    box = [pad, pad, n - 1 - pad, n - 1 - pad]
    side = box[2] - box[0] + 1
    canvas = Image.new('RGBA', (n, n), (0, 0, 0, 0))

    # 外描边
    ow = int(p['outline_w'] * n)
    if ow > 0:
        ImageDraw.Draw(canvas).rounded_rectangle(
            box, radius=int(side * p['corner']), fill=p['outline'] + (255,))

    # 底座渐变
    ibox = [pad + ow, pad + ow, n - 1 - pad - ow, n - 1 - pad - ow]
    iside = ibox[2] - ibox[0] + 1
    r_in = int(iside * p['corner'])
    inner = rounded_mask(n, ibox, r_in)
    grad = Image.new('RGBA', (n, n))
    gd = ImageDraw.Draw(grad)
    span = max(1, n - 1 - 2 * pad)
    for y in range(n):
        gd.line([(0, y), (n, y)], fill=mix(p['bg_top'], p['bg_bot'], (y - pad) / span) + (255,))
    canvas = Image.composite(grad, canvas, inner)

    if not p.get('simple'):
        # 顶部高光
        if p['glow'] > 0:
            a = Image.new('L', (n, n), 0)
            ad = ImageDraw.Draw(a)
            for y in range(n):
                t = (y - pad) / span
                v = int(255 * p['glow'] * (1 - t / 0.50)) if t < 0.50 else 0
                ad.line([(0, y), (n, y)], fill=v)
            hl = Image.new('RGBA', (n, n), (255, 255, 255, 255))
            hl.putalpha(ImageChops.multiply(a, inner))
            canvas = Image.alpha_composite(canvas, hl)
        # 底部内阴影
        if p['shade'] > 0:
            a = Image.new('L', (n, n), 0)
            ad = ImageDraw.Draw(a)
            for y in range(n):
                t = (y - pad) / span
                v = int(255 * p['shade'] * ((t - 0.52) / 0.48)) if t > 0.52 else 0
                ad.line([(0, y), (n, y)], fill=v)
            sh = Image.new('RGBA', (n, n), (0, 0, 0, 255))
            sh.putalpha(ImageChops.multiply(a, inner))
            canvas = Image.alpha_composite(canvas, sh)
        # 内圈亮边（玻璃边缘）
        if p['inner_line'] > 0:
            ring = Image.new('RGBA', (n, n), (0, 0, 0, 0))
            ImageDraw.Draw(ring).rounded_rectangle(
                ibox, radius=r_in, outline=(255, 255, 255, int(255 * p['inner_line'])),
                width=max(1, int(n * 0.006)))
            canvas = Image.alpha_composite(canvas, Image.composite(
                ring, Image.new('RGBA', (n, n), (0, 0, 0, 0)), inner))

    # 箭头
    al = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    ad = ImageDraw.Draw(al)
    L = p['arr_len'] * n
    th = p['arr_thick'] * n
    hl_ = p['arr_head'] * n
    hw = th * p['arr_headw']
    gap = p['arr_gap'] * n
    x0, x1 = (n - L) / 2, (n + L) / 2
    tint = p['arr_color'] + (255,)

    for d_ in (1, -1):
        cy = n / 2 - gap / 2 if d_ > 0 else n / 2 + gap / 2
        if d_ > 0:
            bar = [x0 + th / 2, cy - th / 2, x1 - hl_, cy + th / 2]
            head = [(x1 - hl_, cy - hw / 2), (x1, cy), (x1 - hl_, cy + hw / 2)]
        else:
            bar = [x0 + hl_, cy - th / 2, x1 - th / 2, cy + th / 2]
            head = [(x0 + hl_, cy - hw / 2), (x0, cy), (x0 + hl_, cy + hw / 2)]
        tail = (x0 + th / 2, cy, th / 2) if d_ > 0 else (x1 - th / 2, cy, th / 2)
        _arrow_shape(ad, {'box': bar, 'fill': tint},
                     tail if p['capsule'] else None, head)

    # 箭头柔和投影
    if p['arr_shadow'] > 0 and not p.get('simple'):
        sa = al.getchannel('A').point(lambda v: int(v * p['arr_shadow']))
        sh = Image.new('RGBA', (n, n), (0, 0, 0, 255))
        sh.putalpha(sa)
        sh = sh.filter(ImageFilter.GaussianBlur(max(0.5, n * p['shadow_blur'])))
        canvas = Image.alpha_composite(
            canvas, ImageChops.offset(sh, 0, int(n * p['shadow_dy'])))

    canvas = Image.alpha_composite(canvas, al)
    if ss > 1:
        canvas = canvas.resize((size, size), Image.LANCZOS)
    return canvas


# --------------------------------------------------------------------------
# 20 轮迭代
# --------------------------------------------------------------------------

REVISIONS = [
    ('r01', '初稿：纯色 #10A37F、圆角 6%、平头粗箭头无描边，先确认「双向箭头＝切换」母题成立',
     dict()),
    ('r02', '圆角 6%→14%，去掉方角硬感，向 squircle 靠拢',
     dict(corner=0.14)),
    ('r03', '纯色改垂直渐变 #10A37F→#0A7F5F，消除扁平塑料感',
     dict(corner=0.14, bg_top=(16, 163, 127), bg_bot=(10, 127, 95))),
    ('r04', '上提明度（顶 #1ECB98），深色桌面背景下更跳得出来',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98))),
    ('r05', '加 3% 深色外描边 #064E3B —— 解决浅色桌面下轮廓发飘的问题',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030)),
    ('r06', '箭头杆改胶囊圆头（capsule），去掉生硬平头，现代感更强',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True)),
    ('r07', '箭头瘦身：厚 0.11→0.095、头长 0.16、头宽比 1.8，由「粗笨」转向「精悍」',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80)),
    ('r08', '两箭头间距 0.20→0.30，中间留出通透负空间，32px 不再粘连',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30)),
    ('r09', '箭头加柔和投影（黑 18%、下移 1%、模糊 1.2%），压住白箭头边缘发虚',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18)),
    ('r10', '顶部内高光（白 22%），补一层玻璃质感',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22)),
    ('r11', '外留白 5%→3.5%，底座放大，主体占比从 84% 提到 90%',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22, pad=0.035)),
    ('r12', '底部内阴影（黑 12%），底座下缘在浅背景上不再「浮」',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22, pad=0.035, shade=0.12)),
    ('r13', '加内圈亮边（白 14%、1px 级），玻璃边缘感，浅背景下再勾一道亮线',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.030, capsule=True,
          arr_thick=0.095, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22, pad=0.035, shade=0.12, inner_line=0.14)),
    ('r14', '新增 small 分支（<=48px）：关高光/阴影/投影，描边加粗到 4.5%，箭头加厚',
     dict(corner=0.14, bg_top=(30, 203, 152), bg_bot=(12, 138, 98),
          outline=(6, 78, 59), outline_w=0.045, capsule=True,
          arr_thick=0.145, arr_head=0.16, arr_headw=1.75, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22, pad=0.035, shade=0.12, inner_line=0.14,
          simple=True)),
    ('r15', '对比度校准：描边压深 #053F30、顶部提到 #24D9A2，拉开明暗层次',
     dict(corner=0.16, bg_top=(36, 217, 162), bg_bot=(11, 122, 88),
          outline=(5, 63, 48), outline_w=0.040, capsule=True,
          arr_thick=0.115, arr_head=0.16, arr_headw=1.80, arr_gap=0.30,
          arr_shadow=0.18, glow=0.22, pad=0.035, shade=0.12, inner_line=0.14)),
    ('r16', '16px 实测偏挤：小尺寸描边收 3.8%、箭头 0.135、间距 0.32、头宽比 1.65',
     dict(corner=0.18, bg_top=(36, 217, 162), bg_bot=(11, 122, 88),
          outline=(5, 63, 48), outline_w=0.038, capsule=True,
          arr_thick=0.135, arr_head=0.16, arr_headw=1.65, arr_gap=0.32,
          arr_shadow=0.18, glow=0.22, pad=0.032, shade=0.12, inner_line=0.14)),
    ('r17', '抗锯齿：超采样 4x→8x（16/32 用 12x），斜边与圆角无锯齿毛刺',
     dict(corner=0.18, bg_top=(36, 217, 162), bg_bot=(11, 122, 88),
          outline=(5, 63, 48), outline_w=0.038, capsule=True,
          arr_thick=0.135, arr_head=0.16, arr_headw=1.65, arr_gap=0.32,
          arr_shadow=0.18, glow=0.22, pad=0.032, shade=0.12, inner_line=0.14,
          ss=8)),
    ('r18', '大尺寸细化：圆角 18%→22%，高光衰减区间拉长到 0.50，过渡更柔',
     dict(corner=0.22, bg_top=(36, 217, 162), bg_bot=(11, 122, 88),
          outline=(5, 63, 48), outline_w=0.034, capsule=True,
          arr_thick=0.115, arr_head=0.16, arr_headw=1.75, arr_gap=0.30,
          arr_shadow=0.18, glow=0.24, pad=0.032, shade=0.12, inner_line=0.14,
          ss=8)),
    ('r19', '逐尺寸单独配参：32 间距 0.31 / 48 间距 0.29，描边 4%，头宽比 1.7',
     dict(corner=0.21, bg_top=(36, 217, 162), bg_bot=(11, 122, 88),
          outline=(5, 63, 48), outline_w=0.038, capsule=True,
          arr_thick=0.125, arr_head=0.16, arr_headw=1.72, arr_gap=0.31,
          arr_shadow=0.18, glow=0.23, pad=0.032, shade=0.11, inner_line=0.14,
          ss=8)),
    ('r20', '定稿：头宽比 1.72→1.78 更锋利、箭头长度 0.60、投影收到 16%，出图',
     dict(corner=0.22, bg_top=(34, 212, 158), bg_bot=(10, 116, 84),
          outline=(5, 63, 48), outline_w=0.034, capsule=True,
          arr_thick=0.115, arr_head=0.16, arr_headw=1.78, arr_gap=0.30,
          arr_len=0.60, arr_shadow=0.16, glow=0.23, pad=0.032, shade=0.11,
          inner_line=0.14, ss=8)),
]

# 逐尺寸微调
SIZE_TWEAK = {
    24: dict(simple=True, outline_w=0.042, arr_thick=0.118, arr_gap=0.38,
             arr_len=0.58, arr_head=0.185, arr_headw=1.95, glow=0.0, shade=0.0,
             arr_shadow=0.0, inner_line=0.0, ss=12),
    16: dict(simple=True, outline_w=0.044, arr_thick=0.115, arr_gap=0.40,
             arr_len=0.58, arr_head=0.190, arr_headw=2.00, glow=0.0, shade=0.0,
             arr_shadow=0.0, inner_line=0.0, ss=12),
    32: dict(simple=True, outline_w=0.040, arr_thick=0.115, arr_gap=0.36,
             arr_len=0.60, arr_head=0.180, arr_headw=1.90, glow=0.0, shade=0.0,
             arr_shadow=0.0, inner_line=0.0, ss=12),
    48: dict(simple=True, outline_w=0.038, arr_thick=0.120, arr_gap=0.33,
             arr_len=0.60, arr_head=0.170, arr_headw=1.80, glow=0.0, shade=0.0,
             arr_shadow=0.0, inner_line=0.0, ss=10),
    64: dict(outline_w=0.036, arr_thick=0.120, arr_gap=0.30, arr_len=0.60, ss=8),
    128: dict(ss=8),
    256: dict(ss=6),
    512: dict(ss=2),
}


def params_for(size, p):
    q = dict(p)
    q.update(SIZE_TWEAK.get(size, {}))
    return q


def make(size, p=None):
    p = p or dict(REVISIONS[-1][2])
    return render(size, params_for(size, p))


# --------------------------------------------------------------------------
# 导出：多尺寸 .ico（BMP 帧 + 256 PNG 帧）、PNG 预览、迭代演进图
# --------------------------------------------------------------------------

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _bmp_frame(img):
    """ICO 内嵌的 32bpp BMP 帧（自下而上 BGRA + 全零 AND mask）。"""
    import struct
    w, h = img.size
    px = img.convert('RGBA').load()
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b''.join(rows)
    mask_row = ((w + 31) // 32) * 4
    and_mask = bytes(mask_row * h)
    hdr = struct.pack('<IiiHHIIiiII', 40, w, h * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return hdr + xor + and_mask


def write_ico(path, images):
    """手写 ICO：<=128 用 BMP 帧保证 Tk / 老资源管理器兼容，256 用 PNG 帧压体积。"""
    import struct
    blobs, entries = [], []
    offset = 6 + 16 * len(images)
    for im in images:
        w, h = im.size
        if w >= 256:
            import io
            buf = io.BytesIO()
            im.save(buf, format='PNG', optimize=True)
            blob = buf.getvalue()
        else:
            blob = _bmp_frame(im)
        entries.append(struct.pack('<BBBBHHII', w if w < 256 else 0, h if h < 256 else 0,
                                   0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
        blobs.append(blob)
    header = struct.pack('<HHH', 0, 1, len(images))
    with open(path, 'wb') as f:
        f.write(header + b''.join(entries) + b''.join(blobs))
    return path


def _font(size):
    from PIL import ImageFont
    for name in ('msyh.ttc', 'msyhl.ttc', 'simhei.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype('C:/Windows/Fonts/' + name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _sheet(sizes, dark, pad=26, gap=26, label=True):
    from PIL import Image, ImageDraw
    bg = (18, 20, 24) if dark else (244, 245, 247)
    fg = (226, 232, 240) if dark else (32, 36, 42)
    w = pad * 2 + sum(sizes) + gap * (len(sizes) - 1)
    h = pad * 2 + max(sizes) + (26 if label else 0)
    im = Image.new('RGB', (w, h), bg)
    d = ImageDraw.Draw(im)
    x = pad
    f = _font(13)
    for s in sizes:
        ic = make(s, dict(REVISIONS[-1][2]))
        im.paste(ic, (x, pad + (max(sizes) - s) // 2), ic)
        if label:
            d.text((x, pad + max(sizes) + 8), f'{s}px', font=f, fill=fg)
        x += s + gap
    return im


def build(outdir):
    import pathlib
    from PIL import Image, ImageDraw
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    final = dict(REVISIONS[-1][2])

    # 1) 多尺寸 ICO
    write_ico(outdir / 'icon.ico', [make(s, final) for s in ICO_SIZES])

    # 2) 各尺寸 PNG
    for s in ICO_SIZES + [512]:
        make(s, final).save(outdir / f'icon-{s}.png')

    # 3) 尺寸总览（深/浅两版）
    light = _sheet([16, 24, 32, 48, 64, 128, 256], dark=False)
    dark = _sheet([16, 24, 32, 48, 64, 128, 256], dark=True)
    comb = Image.new('RGB', (max(light.width, dark.width), light.height + dark.height), (255, 255, 255))
    comb.paste(light, (0, 0))
    comb.paste(dark, (0, light.height))
    comb.save(outdir / 'preview-sizes.png')

    # 4) 深浅桌面模拟（48px 真实桌面尺寸 + 文件名）
    def desk(bg, fg):
        im = Image.new('RGB', (520, 132), bg)
        d = ImageDraw.Draw(im)
        ic = make(48, final)
        f = _font(12)
        for i, name in enumerate(['Codex 切换器']):
            x = 28 + i * 150
            im.paste(ic, (x, 20), ic)
            d.text((x - 6 + 30 - d.textlength(name, font=f) / 2, 78), name, font=f, fill=fg)
        ic2 = make(32, final)
        ic3 = make(16, final)
        im.paste(ic2, (250, 28), ic2)
        im.paste(ic3, (320, 36), ic3)
        d.text((244, 78), '32px', font=f, fill=fg)
        d.text((312, 78), '16px', font=f, fill=fg)
        return im

    dl = desk((240, 242, 245), (40, 44, 50))
    dd = desk((22, 24, 28), (225, 230, 236))
    dst = Image.new('RGB', (520, 264), (255, 255, 255))
    dst.paste(dl, (0, 0))
    dst.paste(dd, (0, 132))
    dst.save(outdir / 'preview-desktop.png')

    # 5) 20 轮演进图
    cols, cell = 5, 132
    rows = (len(REVISIONS) + cols - 1) // cols
    evo = Image.new('RGB', (cols * cell, rows * cell + 54), (26, 28, 32))
    d = ImageDraw.Draw(evo)
    d.text((18, 16), 'Codex 切换器 · 图标 20 轮迭代（每格 96px 渲染）', font=_font(17), fill=(236, 242, 248))
    for i, (name, _note, p) in enumerate(REVISIONS):
        ic = render(96, params_for(96, p))
        x = (i % cols) * cell + 18
        y = 54 + (i // cols) * cell + 6
        evo.paste(ic, (x, y), ic)
        d.text((x, y + 100), name, font=_font(13), fill=(150, 200, 180))
    evo.save(outdir / 'evolution.png')
    return outdir


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'assets'
    build(target)
    print('assets written ->', target)
