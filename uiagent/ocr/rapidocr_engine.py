"""RapidOCR（onnxruntime，CPU 推理）OCR 引擎封装。

特性
----
* 纯 CPU 推理，无需 GPU；模型随 ``rapidocr-onnxruntime`` wheel 分发，离线可用。
* 输出 OCRText 时统一换算为**屏幕绝对物理像素坐标**，可直接对接
  pyautogui 点击 / UIA 边界矩形做交叉验证。
* 首次实例化会加载 det / cls / rec 三个 onnx 模型（约 1~5 秒），
  请复用实例（:func:`get_ocr_engine` 提供进程内单例）。
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from ..base import OCRBase, OCRText, Rect
from ..logging_utils import get_logger
from ..screenshot import Screener

logger = get_logger("ocr")


class RapidOCREngine(OCRBase):
    """RapidOCR 引擎（CPU）。"""

    def __init__(
        self,
        screener: Optional[Screener] = None,
        intra_op_num_threads: Optional[int] = None,
        **engine_kwargs: Any,
    ) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self.screener = screener or Screener()
        self._engine_kwargs = dict(engine_kwargs)
        if intra_op_num_threads:
            self._engine_kwargs.setdefault("intra_op_num_threads", int(intra_op_num_threads))

        started = time.perf_counter()
        try:
            self._engine = RapidOCR(**self._engine_kwargs) if self._engine_kwargs else RapidOCR()
        except Exception as exc:  # 参数不被当前版本支持时退化
            logger.warning("RapidOCR 初始化携带参数失败(%s)，退回默认参数", exc)
            self._engine_kwargs = {}
            self._engine = RapidOCR()
        self.load_seconds = round(time.perf_counter() - started, 3)
        self.last_elapse: List[float] = []
        logger.info("RapidOCR 就绪，模型加载耗时 %.2fs", self.load_seconds)

    # ============================================================ 图像准备
    @staticmethod
    def _to_bgr(image: Any) -> np.ndarray:
        """把 PIL / ndarray / 路径统一转换为 BGR ndarray。"""
        if isinstance(image, str):
            import cv2

            data = cv2.imread(image)
            if data is None:
                raise ValueError(f"无法读取图像文件：{image}")
            return data
        if isinstance(image, np.ndarray):
            if image.ndim == 2:
                import cv2

                return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            if image.shape[2] == 4:
                import cv2

                return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
            return image
        # PIL.Image
        import cv2

        return cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)

    def _resolve_image(
        self, image: Any = None, region: Optional[Rect] = None
    ) -> Tuple[np.ndarray, Tuple[int, int]]:
        """返回 (BGR 图像, 图像左上角对应的屏幕坐标)。"""
        if image is None:
            if region is None:
                left, top, _, _ = self.screener.virtual_rect()
                bgr = self.screener.grab_numpy(None)
                return bgr, (int(left), int(top))
            left, top, width, height = (int(v) for v in region)
            bgr = self.screener.grab_numpy((left, top, width, height))
            return bgr, (left, top)

        origin = (int(region[0]), int(region[1])) if region else (0, 0)
        return self._to_bgr(image), origin

    # ============================================================ 推理
    def _infer(self, bgr: np.ndarray) -> List[Tuple[Sequence[Sequence[float]], str, float]]:
        """调用 RapidOCR，兼容不同返回结构。"""
        raw = self._engine(bgr)

        # 1.4.x: (result, elapse)
        if isinstance(raw, tuple) and len(raw) == 2:
            result, elapse = raw
            if isinstance(elapse, (list, tuple)):
                self.last_elapse = [float(v) for v in elapse if isinstance(v, (int, float))]
        else:
            result = raw

        # 2.x: 输出对象（带 boxes / txts / scores）
        if result is not None and not isinstance(result, (list, tuple)):
            boxes = getattr(result, "boxes", None)
            txts = getattr(result, "txts", None)
            scores = getattr(result, "scores", None)
            if boxes is not None and txts is not None:
                scores = scores if scores is not None else [1.0] * len(txts)
                return list(zip(boxes, txts, scores))
            return []

        if not result:
            return []

        items: List[Tuple[Sequence[Sequence[float]], str, float]] = []
        for line in result:
            try:
                box, text, score = line[0], line[1], line[2]
            except (TypeError, IndexError):
                continue
            items.append((box, str(text), float(score)))
        return items

    @staticmethod
    def _box_to_rect(box: Sequence[Sequence[float]], origin: Tuple[int, int]) -> Rect:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        left = int(min(xs)) + origin[0]
        top = int(min(ys)) + origin[1]
        width = int(max(xs) - min(xs))
        height = int(max(ys) - min(ys))
        return (left, top, max(0, width), max(0, height))

    # ============================================================ 对外接口
    def recognize(self, image: Any = None, region: Optional[Rect] = None) -> List[OCRText]:
        """识别图像或屏幕区域，返回带屏幕坐标的文本块列表。"""
        bgr, origin = self._resolve_image(image, region)
        started = time.perf_counter()
        items = self._infer(bgr)
        cost = (time.perf_counter() - started) * 1000
        blocks: List[OCRText] = []
        for box, text, score in items:
            text = text.strip()
            if not text:
                continue
            bounds = self._box_to_rect(box, origin)
            center = (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2)
            blocks.append(OCRText(text=text, confidence=score, bounds=bounds, center=center))
        logger.debug("OCR 识别 %d 段文本，耗时 %.0fms", len(blocks), cost)
        return blocks

    def find_text(
        self,
        text: str,
        image: Any = None,
        region: Optional[Rect] = None,
        fuzzy: bool = True,
    ) -> List[OCRText]:
        """查找文字，返回全部命中（含屏幕坐标）。"""
        needle = (text or "").strip().lower()
        if not needle:
            return []
        hits: List[OCRText] = []
        for block in self.recognize(image=image, region=region):
            haystack = block.text.strip().lower()
            if not haystack:
                continue
            if haystack == needle:
                hits.append(block)
            elif fuzzy and (needle in haystack or haystack in needle):
                hits.append(block)
        hits.sort(key=lambda b: (-b.confidence, -len(b.text)))
        return hits

    def find_text_position(
        self,
        text: str,
        image: Any = None,
        region: Optional[Rect] = None,
        fuzzy: bool = True,
    ) -> Optional[Tuple[int, int, int, int]]:
        """返回首个命中的 ``(center_x, center_y, width, height)``；未命中返回 None。"""
        hits = self.find_text(text, image=image, region=region, fuzzy=fuzzy)
        if not hits:
            return None
        best = hits[0]
        return (best.center[0], best.center[1], best.bounds[2], best.bounds[3])

    def recognize_region(self, region: Optional[Rect] = None) -> str:
        """识别区域并把所有文本按阅读顺序拼成字符串。"""
        blocks = self.recognize(image=None, region=region)
        if not blocks:
            return ""
        blocks.sort(key=lambda b: (b.bounds[1] // 20, b.bounds[0]))
        return "\n".join(b.text for b in blocks)

    def recognize_all(self, region: Optional[Rect] = None) -> List[OCRText]:
        """识别区域（默认全屏）并返回全部文本块。"""
        return self.recognize(image=None, region=region)


_default: Optional[RapidOCREngine] = None


def get_ocr_engine(**kwargs: Any) -> RapidOCREngine:
    """获取进程内共享的 OCR 引擎实例（避免重复加载模型）。"""
    global _default
    if _default is None:
        _default = RapidOCREngine(**kwargs)
    return _default
