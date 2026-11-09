"""Configuration. Secrets come ONLY from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


@dataclass(frozen=True)
class Settings:
    gnani_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    telegram_token: str = ""
    groq_key: str = ""
    hmac_secret: str = "dev-only-secret-change-me"
    db_path: str = "data/munim.db"
    credit_floor: int = 300
    starting_credits: int = 5000
    # product constants (see PRD)
    max_clip_seconds: float = 30.0
    soft_undo_seconds: int = 30
    block_wait_seconds: int = 90
    low_snr_db: float = 10.0
    short_clip_seconds: float = 1.0
    duplicate_window_seconds: int = 120
    outlier_floor_paise: int = 500_000          # Rs 5,000
    outlier_floor_no_history_paise: int = 1_000_000  # Rs 10,000
    outlier_multiple: int = 5
    reminder_start_hour: int = 8
    reminder_end_hour: int = 19
    reminder_min_gap_hours: int = 72
    reminder_max_per_30d: int = 3
    llm_spans: bool = False            # OFF by default: transcripts contain names (see README disclosure)
    llm_model: str = "llama-3.3-70b-versatile"
    briefing_hour: int = 8
    briefing_minute: int = 30
    allow_scan: bool = False           # photo of a paper khata goes to the LLM provider: opt-in
    open_signup: bool = True
    max_new_shops_per_day: int = 50
    root_dir: str = "data"
    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    _load_dotenv()
    e = os.environ
    return Settings(
        gnani_key=e.get("GNANI_API_KEY_ID", ""),
        gnani_base_url=e.get("GNANI_BASE_URL", "https://api.vachana.ai"),
        telegram_token=e.get("TELEGRAM_BOT_TOKEN", ""),
        groq_key=e.get("GROQ_API_KEY", ""),
        hmac_secret=e.get("MUNIM_HMAC_SECRET", "dev-only-secret-change-me"),
        db_path=e.get("MUNIM_DB", "data/munim.db"),
        credit_floor=int(e.get("MUNIM_CREDIT_FLOOR", "300")),
        llm_spans=e.get("MUNIM_LLM_SPANS", "0") == "1",
        llm_model=e.get("MUNIM_LLM_MODEL", "llama-3.3-70b-versatile"),
        starting_credits=int(e.get("MUNIM_STARTING_CREDITS", "5000")),
        allow_scan=e.get("MUNIM_SCAN", "0") == "1",
        open_signup=e.get("MUNIM_OPEN_SIGNUP", "1") == "1",
        max_new_shops_per_day=int(e.get("MUNIM_MAX_NEW_SHOPS_PER_DAY", "50")),
        root_dir=e.get("MUNIM_ROOT", "data"),
    )
