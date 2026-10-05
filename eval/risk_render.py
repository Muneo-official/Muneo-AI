"""
eval/risk_render.py — 품목 표를 견적서 이미지로 그린다.

원본 이미지를 직접 고칠 수 없으므로, 깨끗한 판과 결함 판을 같은 양식·같은 코드로 다시 그린다. 양식은 원본(요약표 →
공사비·간접비·합계 → 상세내역서)을 따른다. 수신인·업체명·단지명은 넣지 않는다.
"""

import pathlib

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1100
ROW_H = 26
MARGIN = 12
# 상세내역서의 칸: (제목, 왼쪽 x). 내용 칸은 가장 긴 품명이 잘리지 않을 만큼 넓게 둔다
DETAIL_COLS = [("코드", 0), ("공사명", 60), ("내 용", 160), ("금 액", 640), ("단 가", 760), ("수량", 870), ("단위", 940), ("비 고", 1000)]
SUMMARY_COLS = [("코드", 0), ("공사명", 60), ("공사내용", 160), ("금 액", 640), ("비 고", 760)]
GRAY, DARK, CYAN = (210, 210, 210), (190, 190, 190), (0, 255, 255)
_FONT_DIRS = [pathlib.Path("C:/Windows/Fonts"), pathlib.Path("/usr/share/fonts/truetype/nanum")]
_FONT_NAMES = [("malgun.ttf", "malgunbd.ttf"), ("NanumGothic.ttf", "NanumGothicBold.ttf")]


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    for folder in _FONT_DIRS:
        for regular, bold in _FONT_NAMES:
            if (folder / regular).exists():
                return (ImageFont.truetype(str(folder / regular), 14), ImageFont.truetype(str(folder / bold), 14),
                        ImageFont.truetype(str(folder / bold), 22))
    raise RuntimeError("한글 글꼴을 찾지 못했습니다 (맑은 고딕 또는 나눔고딕)")


def _money(value) -> str:
    return f"{int(value):,}" if value else "-"


def _qty(value: float) -> str:
    return f"{value:g}" if value else ""


class _Canvas:
    def __init__(self, height: int):
        self.img = Image.new("RGB", (WIDTH, height), "white")
        self.d = ImageDraw.Draw(self.img)
        self.font, self.bold, self.title = _fonts()
        self.y = MARGIN

    def text(self, x: int, text: str, bold: bool = False, right: int | None = None, fill="black") -> None:
        font = self.bold if bold else self.font
        if right is not None:
            x = right - 6 - int(self.d.textlength(text, font=font))
        self.d.text((x + 4, self.y + 4), text, font=font, fill=fill)

    def row(self, cols: list[tuple[str, int]], cells: list[tuple], fill=None) -> None:
        """표의 한 줄. cells는 칸마다 (글, 굵게, 오른쪽 정렬)."""
        right_edge = WIDTH - 2 * MARGIN
        edges = [x for _, x in cols] + [right_edge]
        if fill:
            self.d.rectangle([MARGIN, self.y, MARGIN + right_edge, self.y + ROW_H], fill=fill)
        for i, (text, bold, align_right) in enumerate(cells):
            x0, x1 = MARGIN + edges[i], MARGIN + edges[i + 1]
            self.d.rectangle([x0, self.y, x1, self.y + ROW_H], outline="black")
            clipped = text
            while clipped and self.d.textlength(clipped, font=self.bold if bold else self.font) > x1 - x0 - 10:
                clipped = clipped[:-1]
            self.text(x0, clipped, bold, right=x1 if align_right else None)
        self.y += ROW_H


def render(doc: dict, path: pathlib.Path) -> None:
    """doc(품목 표: info, sections, direct, indirect, total)을 PNG로 저장한다."""
    n_rows = 2 * len(doc["sections"]) + sum(len(s["lines"]) for s in doc["sections"]) + len(doc["indirect"]) + 12
    c = _Canvas(n_rows * ROW_H + 260)
    c.d.rectangle([MARGIN, c.y, WIDTH - MARGIN, c.y + 36], fill=DARK)
    c.d.text((WIDTH // 2 - 90, c.y + 4), "인테리어 견적서", font=c.title, fill="black")
    c.y += 50
    info = doc["info"]
    c.text(MARGIN, f"견적 대상 :  {info['지역']} {info['공간유형']} 리모델링 공사")
    c.y += ROW_H
    c.text(MARGIN, f"공사 규모 :  {info['평수']} 평")
    c.y += ROW_H
    c.text(MARGIN, f"견적 합계 :  ₩{doc['total']:,}  (부가세 별도)", bold=True)
    c.y += ROW_H + 8

    c.row(SUMMARY_COLS, [(t, True, False) for t, _ in SUMMARY_COLS], fill=GRAY)
    for i, sec in enumerate(doc["sections"], 1):
        c.row(SUMMARY_COLS, [(str(i * 100), False, False), (sec["name"], True, False), ("", False, False),
                             (_money(sec["subtotal"]), True, True), ("", False, False)])
    totals = [("공 사 비", doc["direct"])] + list(doc["indirect"].items())
    for label, value in totals:
        c.row(SUMMARY_COLS, [("", False, False), ("", False, False), (label, True, False),
                             (f"{int(value):,}", False, True), ("원", False, False)])
    c.row(SUMMARY_COLS, [("", False, False), ("", False, False), ("합계 금액(부가세 별도)", True, False),
                         (f"{doc['total']:,}", True, True), ("원", False, False)], fill=CYAN)
    c.y += 18
    c.text(MARGIN, "덧붙임1) 상세내역서", bold=True)
    c.y += ROW_H

    c.row(DETAIL_COLS, [(t, True, False) for t, _ in DETAIL_COLS], fill=GRAY)
    for i, sec in enumerate(doc["sections"], 1):
        c.row(DETAIL_COLS, [(str(i * 100), False, False), (sec["name"], True, False), ("소 계", True, False),
                            (_money(sec["subtotal"]), False, True)] + [("", False, False)] * 4, fill=DARK)
        for j, line in enumerate(sec["lines"], 1):
            c.row(DETAIL_COLS, [(str(i * 100 + j), False, False), ("", False, False), (line["desc"], False, False),
                                (_money(line["amount"]), False, True), (_money(line["unit_price"]), False, True),
                                (_qty(line["qty"]), False, True), (line["unit"] if line["qty"] else "", False, False),
                                (line.get("note", ""), False, False)])
    c.row(DETAIL_COLS, [("공 사 비", True, False), ("", False, False), ("", False, False), (f"{doc['direct']:,}", True, True)]
          + [("", False, False)] * 4, fill=DARK)
    c.img.crop((0, 0, WIDTH, c.y + MARGIN)).save(path)
