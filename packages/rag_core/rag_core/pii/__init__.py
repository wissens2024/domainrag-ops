"""PII Detection (ADR-020).

WiSentinel dlp-core 룰 포팅 + 자체 룰. 4-layer 처리에 모두 사용.
"""

from .detector import RegexPIIDetector

__all__ = ["RegexPIIDetector"]
