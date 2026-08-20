"""VigiEye configuration — all via environment variables (12-factor)."""
import os


class Settings:
    app_base_url: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")

    # Anthropic (AI summaries of regulatory changes) — optional at MVP stage
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    # Email (Resend) — optional; leads are logged if unset
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    email_from: str = os.getenv("EMAIL_FROM", "VigiEye <info@vigi-eye.com>")
    email_reply_to: str = os.getenv("EMAIL_REPLY_TO", "info@vigi-eye.com")

    # Admin token guarding the ingestion trigger (/admin/ingest)
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    # Stripe (billing) — optional; checkout disabled until keys are set
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_price_pro_monthly: str = os.getenv("STRIPE_PRICE_PRO_MONTHLY", "")
    stripe_price_team_annual: str = os.getenv("STRIPE_PRICE_TEAM_ANNUAL", "")

    # Sessions — signed cookie secret. Falls back to the admin token so sessions
    # still work before a dedicated secret is set (set SESSION_SECRET in prod).
    session_secret: str = os.getenv("SESSION_SECRET", "") or os.getenv("ADMIN_TOKEN", "") or "dev-insecure-secret"
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))

    # OAuth / SSO — each provider activates only when its client id+secret are set
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    microsoft_client_id: str = os.getenv("MICROSOFT_CLIENT_ID", "")
    microsoft_client_secret: str = os.getenv("MICROSOFT_CLIENT_SECRET", "")
    microsoft_tenant: str = os.getenv("MICROSOFT_TENANT", "common")

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def email_enabled(self) -> bool:
        return bool(self.resend_api_key)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_enabled(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)

    @property
    def auth_enabled(self) -> bool:
        return self.google_enabled or self.microsoft_enabled


settings = Settings()
