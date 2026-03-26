"""Configuration for LinkedIn Tenure Weekly (Phantom Buster + tenure filter)."""
import os
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings from environment variables."""

    # PhantomBuster
    phantombuster_api_key: Optional[str] = None
    phantombuster_api_url: str = "https://api.phantombuster.com/api/v2"
    phantombuster_webhook_url: Optional[str] = None

    # LinkedIn session (for Phantom Buster agent)
    linkedin_session_cookie: Optional[str] = None
    linkedin_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    )

    # Optional: output paths
    output_dir: str = "output"

    # OpenAI (for stakeholder selection)
    openai_api_key: Optional[str] = None

    # Serper (Google search API – e.g. market research, URL discovery)
    serper_api_key: Optional[str] = None

    # RapidAPI (LinkedIn person_deep – existing stakeholders job-change detection)
    rapidapi_key: Optional[str] = None
    rapidapi_person_deep_url: str = "https://linkedin-data-scraper.p.rapidapi.com/person_deep"
    rapidapi_company_url: str = "https://linkedin-data-scraper.p.rapidapi.com/company"

    # SEC EDGAR API
    sec_user_agent: str = "LinkedInTenureWeekly manikaran@vieu.com"  # Required by SEC
    sec_rate_limit_delay: float = 0.1  # Seconds between requests (SEC recommends 10 requests/second max)

    # SEC filings insight window (used by Rubrik, Connect, Genesys)
    # First run: use 180 days of history; weekly runs: use last 7 days. Override via --days or --first-run in scripts.
    sec_window_first_run_days: int = 180
    sec_window_weekly_days: int = 7

    # Postgres (product backend – Company table for Jobs API CompanyID lookup)
    postgres_user: Optional[str] = None
    postgres_password: Optional[str] = None
    postgres_db: Optional[str] = None
    postgres_host: Optional[str] = None
    postgres_port: Optional[str] = None

    # Jobs API v2 (GET /api/v2/company/hiring) – target-account jobs, last 1 week
    vieu_api_key: Optional[str] = None
    jobs_api_base_url: str = "https://api-dev.cloud.seeqe.dev"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
