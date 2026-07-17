"""Development-only DAEMON Score v2 training/evaluation tooling.

Never imported by `app.py`, `bridge.py`, or any other shipped runtime
path -- only by `scripts/score_v2/*.py` and tests. See `score_v2/__init__.py`
for the runtime/training layering rationale.
"""

from __future__ import annotations
