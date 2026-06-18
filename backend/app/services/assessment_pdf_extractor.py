"""PyMuPdfExamExtractor — 기출 PDF에서 페이지 텍스트 + 그림 영역을 추출 (ADR-025 §2).

fitz(PyMuPDF) 의존 어댑터. 순수 파싱(ExamPaperParser)과 분리 — 본 어댑터는 좌표가 필요한
작업(그림 영역 탐지·crop·문항 위치 링크)만 책임진다.

그림 탐지:
  - 비-로고 래스터 이미지(page.get_images) + 벡터 드로잉(page.get_drawings) 클러스터.
  - 페이지 상단 머리말 로고·얇은 구분선·전면 배경은 휴리스틱으로 제외.
  - 각 그림 후보는 get_pixmap(clip=rect)로 PNG 렌더.
문항 링크:
  - 줄 시작 `N.` 텍스트의 y 위치를 모아, 그림 top 바로 위 문항 번호에 귀속.
모든 휴리스틱은 사람 검수 전제(ADR-025) — 오탐/누락은 import 검수 단계에서 보정.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_QNUM_RE = re.compile(r"^\s*(\d{1,3})\.\s")


@dataclass
class ExtractedFigure:
    page_number: int  # 1-based
    bbox: tuple[float, float, float, float]
    png_bytes: bytes
    near_question_number: int | None = None


@dataclass
class ExtractedPdf:
    page_texts: list[str] = field(default_factory=list)
    figures: list[ExtractedFigure] = field(default_factory=list)


class PyMuPdfExamExtractor:
    def __init__(
        self,
        *,
        render_zoom: float = 2.0,        # ~144dpi
        min_figure_area: float = 3000.0,  # pt^2 — 작은 장식 제외
        min_figure_side: float = 28.0,    # 얇은 선·아이콘 제외
        header_band: float = 90.0,         # 상단 머리말(로고) 밴드 높이(pt)
        merge_gap: float = 12.0,           # 클러스터 병합 허용 간격(pt)
        max_area_ratio: float = 0.85,      # 페이지 대비 과대 영역(배경) 제외
    ) -> None:
        self._zoom = render_zoom
        self._min_area = min_figure_area
        self._min_side = min_figure_side
        self._header_band = header_band
        self._merge_gap = merge_gap
        self._max_area_ratio = max_area_ratio

    # ------------------------------------------------------------------ #
    def extract(
        self, pdf_bytes: bytes, *, skip_page_indices: set[int] | None = None
    ) -> ExtractedPdf:
        """skip_page_indices(0-based): 그림 탐지를 건너뛸 페이지(예: 정답표).
        텍스트는 모든 페이지에서 추출하되, 그림은 해당 페이지에서 추출하지 않는다."""
        import fitz  # 지연 import — 순수 계층 비의존

        skip = skip_page_indices or set()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        result = ExtractedPdf()
        for i in range(doc.page_count):
            page = doc[i]
            result.page_texts.append(page.get_text())
            if i in skip:
                continue
            for fig in self._page_figures(fitz, page, i + 1):
                result.figures.append(fig)
        doc.close()
        return result

    # ------------------------------------------------------------------ #
    def _page_figures(self, fitz, page, page_no: int) -> list[ExtractedFigure]:
        page_rect = page.rect
        page_area = float(page_rect.width * page_rect.height)
        rects: list[fitz.Rect] = []

        # 1) 래스터 이미지 (로고/머리말 제외)
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                for r in page.get_image_rects(xref):
                    if self._is_header(r):
                        continue
                    rects.append(fitz.Rect(r))
            except Exception:
                continue

        # 2) 벡터 드로잉
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None:
                continue
            rects.append(fitz.Rect(r))

        # 3) 클러스터 병합
        clusters = self._merge(fitz, rects)

        figures: list[ExtractedFigure] = []
        qpos = self._question_positions(page)
        mid_x = float(page_rect.x0 + page_rect.width / 2)
        for rect in clusters:
            if self._is_header(rect):
                continue
            w, h = rect.width, rect.height
            area = w * h
            if w < self._min_side or h < self._min_side:
                continue
            if area < self._min_area or area > page_area * self._max_area_ratio:
                continue
            png = self._render(fitz, page, rect)
            if png is None:
                continue
            figures.append(
                ExtractedFigure(
                    page_number=page_no,
                    bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                    png_bytes=png,
                    near_question_number=self._nearest_question(
                        qpos, fig_x0=rect.x0, fig_y0=rect.y0, mid_x=mid_x
                    ),
                )
            )
        return figures

    # ------------------------------------------------------------------ #
    def _is_header(self, r) -> bool:
        return r.y1 <= self._header_band

    def _merge(self, fitz, rects: list):
        """겹치거나 가까운(merge_gap 이내) rect를 하나로 병합."""
        boxes = [fitz.Rect(r) for r in rects if r.width > 0 and r.height > 0]
        changed = True
        while changed:
            changed = False
            out: list = []
            while boxes:
                cur = boxes.pop()
                merged = True
                while merged:
                    merged = False
                    rest = []
                    for b in boxes:
                        infl = fitz.Rect(cur)
                        infl.x0 -= self._merge_gap
                        infl.y0 -= self._merge_gap
                        infl.x1 += self._merge_gap
                        infl.y1 += self._merge_gap
                        if infl.intersects(b):
                            cur |= b  # union
                            merged = True
                            changed = True
                        else:
                            rest.append(b)
                    boxes = rest
                out.append(cur)
            boxes = out
        return boxes

    def _render(self, fitz, page, rect) -> bytes | None:
        try:
            mat = fitz.Matrix(self._zoom, self._zoom)
            pix = page.get_pixmap(matrix=mat, clip=rect, alpha=False)
            return pix.tobytes("png")
        except Exception:
            return None

    def _question_positions(self, page) -> list[tuple[int, float, float]]:
        """페이지 내 `N.` 문항 시작의 (번호, x0, y0) 목록 — 2단 컬럼 링크용."""
        out: list[tuple[int, float, float]] = []
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans)
                m = _QNUM_RE.match(text)
                if m:
                    bbox = line.get("bbox", [0, 0, 0, 0])
                    out.append((int(m.group(1)), float(bbox[0]), float(bbox[1])))
        out.sort(key=lambda t: t[2])
        return out

    @staticmethod
    def _nearest_question(
        qpos: list[tuple[int, float, float]],
        *,
        fig_x0: float,
        fig_y0: float,
        mid_x: float,
    ) -> int | None:
        """그림이 속한 컬럼(좌/우)에서 그림 top 바로 위 문항 번호.

        2단 레이아웃에서 같은 y의 다른 컬럼 문항에 오링크되는 것을 막는다.
        같은 컬럼 후보가 없으면 컬럼 무시 fallback.
        """
        if not qpos:
            return None
        fig_col = fig_x0 >= mid_x
        same_col = [
            (n, y) for (n, x, y) in qpos if (x >= mid_x) == fig_col and y <= fig_y0 + 2.0
        ]
        if same_col:
            return max(same_col, key=lambda t: t[1])[0]
        any_above = [(n, y) for (n, _x, y) in qpos if y <= fig_y0 + 2.0]
        if any_above:
            return max(any_above, key=lambda t: t[1])[0]
        return qpos[0][0]
