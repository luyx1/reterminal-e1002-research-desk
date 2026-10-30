from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).with_name("e1002-status-snapshot.png")
SCALE = 2
W, H = 800, 480
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)
PALETTE = (WHITE, BLACK, RED, YELLOW, BLUE, GREEN)
FONT_ZH = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"


def main():
    canvas = Image.new("RGB", (W * SCALE, H * SCALE), WHITE)
    draw = ImageDraw.Draw(canvas)

    def px(value):
        return round(value * SCALE)

    def rect(box, fill, outline=None, width=1):
        draw.rectangle(tuple(px(value) for value in box), fill=fill, outline=outline, width=px(width))

    def line(points, fill=BLACK, width=1):
        draw.line([(px(x), px(y)) for x, y in points], fill=fill, width=px(width))

    def text(pos, value, size, fill=BLACK, zh=False, anchor=None):
        font_path = FONT_ZH if zh else FONT_SANS
        font = ImageFont.truetype(font_path, size * SCALE)
        draw.text((px(pos[0]), px(pos[1])), value, font=font, fill=fill, anchor=anchor)

    # Header.
    rect((32, 28, 48, 44), GREEN)
    text((60, 18), "reTerminal E1002", 25)
    text((61, 53), "本机组件总览  /  静态页面", 14, zh=True)
    rect((621, 25, 768, 62), GREEN)
    text((694.5, 43.5), "Wi-Fi 已连接", 16, zh=True, anchor="mm")
    line([(32, 82), (768, 82)], BLACK, 2)

    cards = [
        ((32, 99, 388, 188), RED, "显示屏", "7.3\" Spectra 6 · 800 × 480", "完整刷新约 15–20 秒 · 不支持局部刷新"),
        ((412, 99, 768, 188), BLUE, "无线连接", "Wi-Fi 2.4 GHz · 已连接", "Bluetooth 5.0 · 本页通过 Wi-Fi 连接"),
        ((32, 202, 388, 291), YELLOW, "环境传感", "温度 · 湿度", "板载 SHT4x 传感器"),
        ((412, 202, 768, 291), GREEN, "供电与存储", "2000 mAh 电池 · 电压监测", "microSD 最大 32 GB · USB-C 5 V / 1 A"),
        ((32, 305, 388, 394), GREEN, "本机交互", "3 个按键 · 状态灯", "充电指示灯 · 蜂鸣器 · 背部电源开关"),
        ((412, 305, 768, 394), BLUE, "扩展接口", "8-pin · UART / I²C / GPIO", "麦克风预留 · 可外接模块"),
    ]

    for box, accent, title, main_value, detail in cards:
        x1, y1, x2, y2 = box
        rect(box, WHITE, BLACK, 2)
        rect((x1 + 15, y1 + 15, x1 + 25, y1 + 34), accent)
        text((x1 + 36, y1 + 9), title, 16, zh=True)
        text((x1 + 16, y1 + 37), main_value, 15, zh=True)
        text((x1 + 16, y1 + 65), detail, 12, zh=True)

    # E-paper update guidance.
    line([(32, 410), (768, 410)], BLACK, 1)
    rect((32, 424, 768, 469), YELLOW)
    text((50, 433), "墨水屏策略", 14, zh=True)
    text((177, 433), "低频更新 · 页面保持静止 · 更换画面时预留完整刷新时间", 13, zh=True)

    # Keep the output to the six native Spectra 6 colors.
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette([channel for color in PALETTE for channel in color] + [0] * (768 - 18))
    canvas = canvas.resize((W, H), Image.Resampling.LANCZOS)
    canvas.quantize(palette=palette_image, dither=Image.Dither.NONE).save(OUT, optimize=True)
    print(f"Wrote {OUT} ({W}x{H}, six-color Spectra 6 palette)")


if __name__ == "__main__":
    main()
