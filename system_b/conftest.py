import sys
from pathlib import Path

# src-layout bootstrap so tests run without an installed package
SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
