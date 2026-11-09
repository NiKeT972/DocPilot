"""Runtime configuration, read from environment variables (and `.env` locally)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

try:  # local dev convenience; on Vercel env vars are injected directly
    from dotenv import load_dotenv

    if not os.getenv("DOTENV_DISABLED"):
        load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class ConfigError(RuntimeError):
    """Raised when the deployment is misconfigured (fails loudly at startup)."""


@dataclass(frozen=True)
class Settings:
    # Keys
    groq_api_key: str = ""
    gemini_api_key: str = ""
    tavily_api_key: str = ""
    supabase_url: str = ""
    supabase_service_key: str = ""

    # Models
    answer_model: str = "openai/gpt-oss-120b"
    fast_model: str = "openai/gpt-oss-20b"
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768
    ocr_model: str = "gemini-3.5-flash-lite"

    # Retrieval / chunking
    top_k: int = 6
    candidate_k: int = 8
    chunk_size: int = 1000
    chunk_overlap: int = 150

    # Limits
    max_pdf_mb: int = 50
    max_pages: int = 1000
    max_files_per_session: int = 10
    max_pages_per_session: int = 2000
    max_ocr_pages_per_file: int = 30
    max_batch_pages: int = 40
    max_batch_bytes: int = 2_500_000
    max_upload_mb: int = 4

    # Rate limits
    rate_chat_per_hour: int = 30
    rate_ingest_per_day: int = 300
    rate_ocr_per_day: int = 40

    # Security / ops
    ip_hash_salt: str = "docpilot-dev-salt"
    cron_secret: str = ""
    retention_days: int = 7
    on_vercel: bool = False

    @property
    def web_enabled(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def ocr_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    @property
    def llm_configured(self) -> bool:
        return bool(self.groq_api_key and self.gemini_api_key)

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.groq_api_key:
            missing.append("GROQ_API_KEY")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        return missing

    def validate(self) -> None:
        """Fail loudly on configurations that would silently lose data."""
        if self.on_vercel and not self.supabase_enabled:
            raise ConfigError(
                "DocPilot is running on Vercel without Supabase. The in-memory store loses data "
                "between invocations; set SUPABASE_URL and SUPABASE_SERVICE_KEY."
            )
        if self.on_vercel and self.ip_hash_salt == "docpilot-dev-salt":
            raise ConfigError("Set IP_HASH_SALT to a random secret on Vercel.")


def load_settings() -> Settings:
    return Settings(
        groq_api_key=_str("GROQ_API_KEY"),
        gemini_api_key=_str("GEMINI_API_KEY"),
        tavily_api_key=_str("TAVILY_API_KEY"),
        supabase_url=_str("SUPABASE_URL").rstrip("/"),
        supabase_service_key=_str("SUPABASE_SERVICE_KEY") or _str("SUPABASE_SECRET_KEY"),
        answer_model=_str("ANSWER_MODEL", "openai/gpt-oss-120b"),
        fast_model=_str("FAST_MODEL", "openai/gpt-oss-20b"),
        embed_model=_str("EMBED_MODEL", "gemini-embedding-001"),
        embed_dim=_int("EMBED_DIM", 768),
        ocr_model=_str("OCR_MODEL", "gemini-3.5-flash-lite"),
        top_k=_int("TOP_K", 6),
        candidate_k=_int("CANDIDATE_K", 8),
        chunk_size=_int("CHUNK_SIZE", 1000),
        chunk_overlap=_int("CHUNK_OVERLAP", 150),
        max_pdf_mb=_int("MAX_PDF_MB", 50),
        max_pages=_int("MAX_PAGES", 1000),
        max_files_per_session=_int("MAX_FILES_PER_SESSION", 10),
        max_pages_per_session=_int("MAX_PAGES_PER_SESSION", 2000),
        max_ocr_pages_per_file=_int("MAX_OCR_PAGES_PER_FILE", 30),
        rate_chat_per_hour=_int("RATE_CHAT_PER_HOUR", 30),
        rate_ingest_per_day=_int("RATE_INGEST_PER_DAY", 300),
        rate_ocr_per_day=_int("RATE_OCR_PER_DAY", 40),
        ip_hash_salt=_str("IP_HASH_SALT", "docpilot-dev-salt"),
        cron_secret=_str("CRON_SECRET"),
        retention_days=_int("RETENTION_DAYS", 7),
        on_vercel=bool(_str("VERCEL")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
