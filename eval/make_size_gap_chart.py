"""labels.csv에서 |쿼리 평수 - 사례 평수|별 relevant 비율을 막대 차트(PNG/SVG)로 그린다.

막대 색은 필터 경계와 맞춘다: 새 필터 안 / 기존 ±7 안에만 / 둘 다 밖.
새 경계를 정하기 전(--new both)에는 ±5·±6 경계를 모두 점선으로 긋고, 6평 막대는
"±6 전환 시에만 포함"으로 빗금 처리한다.

실행 (matplotlib 필요):
    python -m eval.make_size_gap_chart               # 결정 전: ±5, ±6 둘 다
    python -m eval.make_size_gap_chart --new 6       # ±6 채택 시
"""

import argparse
import collections
import csv
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

ROOT = pathlib.Path(__file__).parent
LABELS_CSV_PATH = ROOT / "test_inputs" / "labels.csv"
OUT_DIR = ROOT / "results"
OLD_RANGE = 7
MAX_GAP = 15

INSIDE, INSIDE_LIGHT, OLD_ONLY, OUTSIDE = "#1f5fae", "#1f5fae", "#9cc3ee", "#cfcec8"
INK, INK2, SURF, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new", choices=["5", "6", "both"], default="both")
    args = parser.parse_args()
    new_lines = [5, 6] if args.new == "both" else [int(args.new)]

    rows = list(csv.DictReader(LABELS_CSV_PATH.open(encoding="utf-8-sig")))
    bins = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        d = abs(int(float(r["query_size"])) - int(float(r["size_pyeong"])))
        bins[d][0] += 1
        bins[d][1] += int(r["label"])
    n_queries = len({r["query_id"] for r in rows})

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["svg.fonttype"] = "path"  # 한글 글꼴이 없는 환경에서도 SVG가 깨지지 않게 윤곽선으로 저장
    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=160)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)

    xs = list(range(MAX_GAP + 1))
    for d in xs:
        n, rel = bins.get(d, (0, 0))
        if n == 0:
            ax.text(d, 1.5, "n=0", ha="center", va="bottom", fontsize=7.5, color=INK2)
            continue
        hatch = None
        if d <= min(new_lines):
            color = INSIDE
        elif d <= max(new_lines):  # 결정 전의 6평: ±6 전환 시에만 새 필터 안
            color, hatch = INSIDE_LIGHT, "////"
        elif d <= OLD_RANGE:
            color = OLD_ONLY
        else:
            color = OUTSIDE
        y = rel / n * 100
        ax.bar(d, y, width=0.72, color=color, edgecolor=SURF if not hatch else "#ffffff",
               linewidth=0 if hatch else 2, hatch=hatch)
        ax.text(d, y + 1.5, f"{y:.0f}%\n{rel}/{n}", ha="center", va="bottom",
                fontsize=7.5 if d not in (6, 7) else 8.5,
                color=INK, fontweight="bold" if d in (6, 7) else "normal", linespacing=1.1)

    line_specs = [(x + 0.5, f"±{x}평", INK) for x in new_lines] + [(OLD_RANGE + 0.5, "기존 ±7평", INK2)]
    for x, label, c in line_specs:
        ax.axvline(x, color=c, linestyle=(0, (4, 3)), linewidth=1.1)
        ax.text(x, 113, label, fontsize=8.5, color=c, ha="center", va="bottom")

    handles = [Patch(facecolor=INSIDE, label=f"새 필터 안 (±{min(new_lines)}평 이내)")]
    if len(new_lines) == 2:
        handles.append(Patch(facecolor=INSIDE_LIGHT, hatch="////", edgecolor="#ffffff",
                             label="±6 전환 시에만 새 필터 안"))
    handles += [Patch(facecolor=OLD_ONLY, label="기존 ±7 필터 안에만"),
                Patch(facecolor=OUTSIDE, label="두 필터 모두 밖")]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=8.5,
              labelcolor=INK2, bbox_to_anchor=(1.0, 0.98))

    ax.set_xticks(xs)
    ax.set_xlim(-0.6, MAX_GAP + 0.6)
    ax.set_ylim(0, 112)
    ax.set_yticks(range(0, 101, 20))
    ax.set_xlabel("쿼리-사례 평수 차이 (평)", color=INK2)
    ax.set_ylabel("relevant 비율 (%)", color=INK2)
    ax.set_title("평수 차이별 관련도 — 6평과 7평 사이에서 급락", loc="left", color=INK,
                 fontsize=13, pad=26)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#bdbcb6")
    ax.tick_params(colors=INK2)

    fig.text(0.01, 0.045,
             "라벨 기준(docs/LABELING_GUIDE.md §4①): relevant 조건은 평수 ±5평 이내. 단 6~7평 차이라도 "
             "'구조가 명백히 비슷한 사례'(같은 단지, 평형 표기 오차 등)면 사람이 1로 바꿀 수 있음.",
             fontsize=7.3, color=INK2)
    fig.text(0.01, 0.015,
             f"출처: eval/test_inputs/labels.csv ({len(rows):,}건, {n_queries}개 쿼리) · 막대 위 = relevant 비율, "
             f"relevant/라벨 수 · 평수 차이 {MAX_GAP}평까지 표시",
             fontsize=7.3, color=INK2)
    fig.tight_layout(rect=(0, 0.07, 1, 1))

    suffix = "" if args.new == "both" else f"_new{args.new}"
    for ext in ("png", "svg"):
        out = OUT_DIR / f"size_gap_relevance{suffix}.{ext}"
        fig.savefig(out, facecolor=SURF)
        print(f"[OK] {out}")


if __name__ == "__main__":
    main()
