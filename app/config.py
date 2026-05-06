from typing import Self

from pydantic import model_validator
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
    # NOTE: the dev/test default below is deliberately weak ("change-me").
    # ``_validate_prod_secrets`` rejects this value when ``app_env == "prod"``.
    secret_key: str = "change-me"  # noqa: S105 -- placeholder; required in prod, see validator
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

    @model_validator(mode="after")
    def _validate_prod_secrets(self) -> Self:
        if self.app_env == "prod":
            if not self.secret_key or self.secret_key == "change-me":  # noqa: S105
                raise ValueError(
                    "SECRET_KEY must be set to a non-default value when APP_ENV=prod"
                )
            if not self.google_client_id or not self.google_client_secret:
                raise ValueError(
                    "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required when APP_ENV=prod"
                )
        return self


settings = Settings()
