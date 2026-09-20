"""서버 프로그램 아이콘 생성 (주식 상승 이미지).

  python tools/make_icon.py   ->  server/assets/stock_svr.ico (16~256px 다중 크기), server/assets/stock_svr.png (256px)
                                  web/assets/img/favicon.ico, favicon-32.png, apple-touch-icon.png (웹 파비콘, 같은 이미지)

디자인: 짙은 남색 라운드 사각형 배경 + 오르는 캔들 3개(한국 관례: 상승 = 빨강) + 금색 상승 화살표.
4배 크기로 그린 뒤 축소해 계단 현상을 줄인다. 디자인을 바꾸려면 이 스크립트만 고치면 된다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "server" / "assets"
SS = 4                 # 슈퍼샘플링 배율
BASE = 256 * SS

BG_TOP = (20, 33, 61)
BG_BOTTOM = (10, 18, 38)
UP = (229, 72, 77)      # 상승 캔들(빨강)
UP_DARK = (176, 40, 46)
GRID = (255, 255, 255, 22)
ARROW = (255, 200, 60)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def overlay(base: Image.Image, draw_fn) -> Image.Image:
    """투명 레이어에 그린 뒤 alpha 합성(반투명 요소가 실제로 섞이도록)."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return Image.alpha_composite(base, layer)


def draw() -> Image.Image:
    img = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))

    # 배경: 세로 그라데이션 + 라운드 사각형 마스크
    grad = Image.new("RGBA", (BASE, BASE))
    gd = ImageDraw.Draw(grad)
    for y in range(BASE):
        gd.line([(0, y), (BASE, y)], fill=lerp(BG_TOP, BG_BOTTOM, y / BASE) + (255,))
    mask = Image.new("L", (BASE, BASE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, BASE - 1, BASE - 1], radius=int(BASE * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)

    # 옅은 가로 격자선 (반투명)
    def grid(d):
        for i in range(1, 5):
            y = int(BASE * (0.2 + i * 0.14))
            d.line([(int(BASE * 0.12), y), (int(BASE * 0.88), y)], fill=GRID, width=SS * 2)
    img = overlay(img, grid)

    # 오르는 캔들 3개: (중심 x 비율, 몸통 top, 몸통 bottom, 꼬리 top, 꼬리 bottom) — 0~1 비율
    candles = [
        (0.27, 0.62, 0.80, 0.56, 0.84),
        (0.50, 0.44, 0.66, 0.36, 0.72),
        (0.73, 0.22, 0.48, 0.14, 0.54),
    ]
    w = int(BASE * 0.13)

    def candle_layer(d):
        for cx, bt, bb, wt, wb in candles:
            x = int(BASE * cx)
            d.line([(x, int(BASE * wt)), (x, int(BASE * wb))], fill=UP_DARK, width=SS * 7)
            d.rounded_rectangle([x - w // 2, int(BASE * bt), x + w // 2, int(BASE * bb)],
                                radius=SS * 8, fill=UP, outline=UP_DARK, width=SS * 3)
    img = overlay(img, candle_layer)

    # 금색 상승 추세선 + 끝에 화살촉(마지막 구간 방향에 맞춰 붙임)
    pts = [(0.10, 0.78), (0.34, 0.58), (0.50, 0.66), (0.78, 0.31)]
    line = [(int(BASE * px), int(BASE * py)) for px, py in pts]
    (x0, y0), (x1, y1) = line[-2], line[-1]
    dx, dy = x1 - x0, y1 - y0
    ln = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / ln, dy / ln                 # 진행 방향 단위벡터
    nx, ny = -uy, ux                          # 수직 벡터
    hs = int(BASE * 0.15)                     # 화살촉 크기
    tip = (x1 + ux * hs * 0.55, y1 + uy * hs * 0.55)
    bc = (x1 - ux * hs * 0.30, y1 - uy * hs * 0.30)
    head = [tip, (bc[0] + nx * hs * 0.62, bc[1] + ny * hs * 0.62), (bc[0] - nx * hs * 0.62, bc[1] - ny * hs * 0.62)]

    def arrow_layer(d):
        sh = SS * 4
        d.line([(px, py + sh) for px, py in line], fill=(0, 0, 0, 90), width=SS * 16, joint="curve")
        d.polygon([(px, py + sh) for px, py in head], fill=(0, 0, 0, 90))
        d.line(line, fill=ARROW, width=SS * 14, joint="curve")
        d.polygon(head, fill=ARROW)
    img = overlay(img, arrow_layer)
    return img.resize((256, 256), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    img = draw()
    png = OUT_DIR / "stock_svr.png"
    ico = OUT_DIR / "stock_svr.ico"
    img.save(png)
    img.save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"생성: {png.relative_to(ROOT)} ({png.stat().st_size:,}B), {ico.relative_to(ROOT)} ({ico.stat().st_size:,}B)")

    # 웹 파비콘 세트 (서버 아이콘과 동일한 이미지)
    web = ROOT / "web" / "assets" / "img"
    web.mkdir(parents=True, exist_ok=True)
    img.save(web / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    img.resize((32, 32), Image.LANCZOS).save(web / "favicon-32.png")
    img.resize((180, 180), Image.LANCZOS).save(web / "apple-touch-icon.png")
    print("웹 파비콘:", ", ".join(f"{f.name}({f.stat().st_size:,}B)" for f in sorted(web.glob("*")) if f.suffix in (".ico", ".png")))


if __name__ == "__main__":
    main()
