import opus_auth
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Infra bootstrap, read from env at startup. User-editable settings
    (integrations, library paths, quality, subtitle policy) live in the DB —
    see opus.settings_store."""

    model_config = {"env_prefix": "OPUS_"}

    database_url: str
    cookie_domain: str = ""
    # what sessions are signed with, the same in all three modules. There is no
    # fallback: without it no module could read another's session.
    session_key: str = Field(min_length=opus_auth.SESSION_KEY_MIN)
    # Required only while the roster is empty. It is deliberately separate
    # from the session-signing key so the credential that creates an admin has
    # one purpose and can be removed after initial setup.
    bootstrap_key: str = ""

    @field_validator("bootstrap_key")
    @classmethod
    def _bootstrap_key_is_long_enough(cls, value: str) -> str:
        if value and len(value) < opus_auth.SESSION_KEY_MIN:
            raise ValueError(f"bootstrap key must be at least {opus_auth.SESSION_KEY_MIN} characters")
        return value

    poll_interval_seconds: int = 10
    subtitle_retry_minutes: int = 30

    # how often each half looks for something new to fetch on its own: a
    # newly-aired episode, a released film, a record a followed artist put out
    monitor_interval_seconds: int = 6 * 3600
    # how recently a record must have come out to count as new. A followed
    # artist's back catalogue is not new music merely because the catalogue
    # only mentioned it today.
    monitor_new_release_days: int = 90


settings = Settings()
