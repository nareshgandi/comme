"""
pytest root conftest.
Adds the repo root to sys.path so that `from python.factories.X import Y`
works without installing the package, whether tests are run from the repo
root or from within python/factories/tests/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
