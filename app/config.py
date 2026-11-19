from functools import lru_cache

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment / .env. Nothing secret has a default."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    keitaro_url: HttpUrl
    keitaro_api_key: SecretStr
    keitaro_timeout: float = Field(15.0, gt=0, le=120)
    keitaro_connect_timeout: float = Field(5.0, gt=0, le=60)

    default_domain_id: int = Field(gt=0)
    default_group_id: int = Field(gt=0)
    default_traffic_source_id: int = Field(gt=0)

    campaign_prefix: str = Field(min_length=1)
    fallback_redirect_url: str = "https://google.com"

    database_url: str = "sqlite:///./app.db"

    # Host header allow-list (DNS-rebinding protection). Comma separated, no ports.
    allowed_hosts: str = "127.0.0.1,localhost"

    # Link for "View in KT". Placeholders: {base} = KEITARO_URL without trailing slash, {id} = campaign id.
    # Format verified by the owner in the Keitaro admin (2026-11-19). Empty value -> the button is hidden.
    keitaro_campaign_url_template: str | None = "{base}/admin/#!/campaign/{id}/main"

    @field_validator("campaign_prefix")
    @classmethod
    def _prefix_shape(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("test_") or not v.endswith("_"):
            raise ValueError("CAMPAIGN_PREFIX must look like test_<lastname>_")
        return v

    @field_validator("fallback_redirect_url")
    @classmethod
    def _redirect_url(cls, v: str) -> str:
        # kept as a plain string so Keitaro gets exactly the configured value (HttpUrl would add "/")
        v = v.strip()
        HttpUrl(v)  # validate only
        if not v.startswith(("http://", "https://")):
            raise ValueError("FALLBACK_REDIRECT_URL must be http(s)")
        return v

    @field_validator("keitaro_campaign_url_template")
    @classmethod
    def _url_template(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if "{id}" not in v:
            raise ValueError("KEITARO_CAMPAIGN_URL_TEMPLATE must contain {id}")
        try:
            v.format(base="x", id=1)
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError("KEITARO_CAMPAIGN_URL_TEMPLATE: only {base} and {id} placeholders allowed") from e
        return v

    def campaign_url(self, campaign_id: int) -> str | None:
        if not self.keitaro_campaign_url_template:
            return None
        base = str(self.keitaro_url).rstrip("/")
        return self.keitaro_campaign_url_template.format(base=base, id=int(campaign_id))

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def keitaro_api_base(self) -> str:
        return str(self.keitaro_url).rstrip("/") + "/admin_api/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
