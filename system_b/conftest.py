import sys
from pathlib import Path

# src-layout bootstrap so tests run without an installed package
SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# System B's iflow archive vendor reads the download cache + file listing via
# System A's `shared.iflow_history`, so B cannot actually run standalone. This
# is the seam the A-into-B unification closes; until then, bootstrap A's src too
# so `cd system_b && pytest` matches root-level `pytest`.
SRC_A = Path(__file__).parent.parent / "system_a" / "src"
if str(SRC_A) not in sys.path:
    sys.path.insert(0, str(SRC_A))
