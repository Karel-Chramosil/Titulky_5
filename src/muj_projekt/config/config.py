from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_DIR.parents[1]
MODELS_DIR = PACKAGE_DIR / "models"
SRT_PATH = PROJECT_ROOT / "titulky.srt"

TARGET_RATE = 16_000
WHISPER_MODEL = "large-v3"
CPU_WHISPER_MODEL = "small"
NLLB_TARGET = "ces_Latn"

# Klíč = kód Whisperu, hodnota = (kód NLLB, popisek).
NLLB_SOURCES = {
    "en": ("eng_Latn", "angličtina"),
    "de": ("deu_Latn", "němčina"),
    "fr": ("fra_Latn", "francouzština"),
    "es": ("spa_Latn", "španělština"),
    "it": ("ita_Latn", "italština"),
    "pl": ("pol_Latn", "polština"),
    "zh": ("zho_Hans", "čínština"),
    "ja": ("jpn_Jpan", "japonština"),
}
