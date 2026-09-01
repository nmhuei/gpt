"""metrics: text normalization and frequency utilities.

Known issue: ``text.py`` and ``stats.py`` carry copy-pasted duplicates of
``normalize_token`` / ``tokenize``. See task.json for the refactor goal.
"""

from . import stats, text

__all__ = ["stats", "text"]
