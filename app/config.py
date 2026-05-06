from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application settings.

    Loaded from `.env` in dev; in prod the same vars come from the host
    environment. See `.env.example` for the full list of recognised keys.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite:///./dev.db"
    secret_key: str = "change-me"  # noqa: S105 -- placeholder; real value via env
    app_base_url: str = "http://localhost:8000"
    app_env: str = "dev"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_hosted_domain: str = ""

    email_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "inventory@example.com"
    smtp_use_tls: bool = True

    bootstrap_admin_email: str = ""


settings = Settings()
