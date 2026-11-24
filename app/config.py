from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    adrobot_mode: Literal["demo", "live"] = "demo"
    database_url: str = "sqlite:///./adrobot.db"
    demo_database_url: str = "sqlite:///./demo-keitaro.db"
    campaign_prefix: str = "test_terentev_"
    keitaro_base_url: str = "https://tracker.example.com"
    keitaro_api_key: SecretStr = SecretStr("")
    keitaro_timeout_seconds: float = 15
    app_username: str = ""
    app_password: SecretStr = SecretStr("")
    allowed_hosts: str = "localhost,127.0.0.1,testserver"

    @model_validator(mode="after")
    def validate_config(self):
        if not self.campaign_prefix.startswith(
            "test_"
        ) or not self.campaign_prefix.endswith("_"):
            raise ValueError("CAMPAIGN_PREFIX must have the form test_surname_")
        if bool(self.app_username) != bool(self.app_password.get_secret_value()):
            raise ValueError("Set both APP_USERNAME and APP_PASSWORD")
        if self.adrobot_mode == "live":
            url = urlparse(self.keitaro_base_url)
            if (
                url.scheme != "https"
                or not url.netloc
                or url.path not in ("", "/")
                or url.query
                or url.fragment
                or url.username
            ):
                raise ValueError(
                    "KEITARO_BASE_URL must be an HTTPS origin, without /admin"
                )
            if not self.keitaro_api_key.get_secret_value():
                raise ValueError("KEITARO_API_KEY is required in live mode")
        return self
