"""홈 화면 아이콘 생성.

신용등급이 한 단계 내려앉는 모습 — 계단형 등급 막대를 화살표가 가로질러 내려간다.
32px 파비콘에서도 읽혀야 하므로 요소는 셋(막대 3개, 화살표)으로 제한한다.

    python make_icon.py
"""
from PIL import Image, ImageDraw

GROUND = (23, 30, 33)      # --surface (dark)
BAR = (62, 76, 81)
BAR_LIT = (233, 238, 239)
ARROW = (242, 131, 122)    # --down (dark) — 하향이 이 앱의 주된 신호다
SIZES = {"apple-touch-icon.png": 180, "icon-512.png": 512, "favicon.png": 32}
SUPER = 8                  # 안티에일리어싱용 배율


def draw(size: int) -> Image.Image:
    s = size * SUPER
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=GROUND)

    # 등급 막대 3개 — 위에서 아래로 짧아진다(등급이 낮아진다)
    x0 = s * 0.20
    for i, (w, y) in enumerate(((0.60, 0.30), (0.44, 0.50), (0.28, 0.70))):
        top = s * y
        h = s * 0.075
        d.rounded_rectangle([x0, top, x0 + s * w, top + h],
                            radius=h / 2, fill=BAR_LIT if i == 0 else BAR)

    # 막대를 가로질러 내려가는 화살표
    cx = s * 0.71
    d.line([(cx, s * 0.26), (cx, s * 0.70)], fill=ARROW, width=int(s * 0.075))
    d.polygon([(cx - s * 0.115, s * 0.655), (cx + s * 0.115, s * 0.655),
               (cx, s * 0.825)], fill=ARROW)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for name, size in SIZES.items():
        draw(size).save(name)
        print(f"  {name} ({size}x{size})")
