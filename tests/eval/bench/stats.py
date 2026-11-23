"""Small statistics for the benchmark report."""
import math
from typing import Tuple


def wilson(hits: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval for a proportion. (0, 1) when there is nothing to count."""
    if total <= 0:
        return 0.0, 1.0
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
