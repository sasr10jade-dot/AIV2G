"""프로젝트 공용 경로 정의."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
MODEL_REGISTRY_DIR = DATA_DIR / "model_registry"
CURATED_DB = DATA_DIR / "curated.db"
OUTPUT_DIR = PROJECT_ROOT / "output"

for _dir in (RAW_DIR, MODEL_REGISTRY_DIR, OUTPUT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
