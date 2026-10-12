"""Optional FAISS loading without unsafe OpenMP override on macOS."""
import importlib.util
import logging
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


def load_faiss():
    # Some Apple Silicon wheels bundle different libomp copies. Initializing
    # FAISS's copy before InsightFace imports Torch through Albumentations can
    # abort the process (not a catchable Python exception). NumPy is the existing
    # exact fallback. Never set KMP_DUPLICATE_LIB_OK to hide this runtime conflict.
    if platform.system() == 'Darwin':
        torch_spec = importlib.util.find_spec('torch')
        faiss_spec = importlib.util.find_spec('faiss')
        if torch_spec and faiss_spec:
            torch_omp = Path(torch_spec.origin).parent / 'lib/libomp.dylib'
            faiss_omp = Path(faiss_spec.origin).parent / '.dylibs/libomp.dylib'
            if torch_omp.exists() and faiss_omp.exists() and torch_omp.resolve() != faiss_omp.resolve():
                logger.warning('Using exact NumPy centroid matching: installed Torch/FAISS wheels bundle conflicting OpenMP runtimes')
                return None
    try:
        import faiss
        return faiss
    except ImportError:
        return None
