"""OCR 后端（CPU 推理）。

默认实现：:class:`~uiagent.ocr.rapidocr_engine.RapidOCREngine`（RapidOCR + onnxruntime）。
"""

from .rapidocr_engine import RapidOCREngine, get_ocr_engine

__all__ = ["RapidOCREngine", "get_ocr_engine"]
