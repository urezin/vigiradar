"""VigiRadar configuration — all via environment variables (12-factor)."""
import os


class Settings:
    app_base_url: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")

    # Anthropic (AI summaries of regulatory changes) — optional at MVP stage
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    # Email (Resend) — optional; leads are logged if unset
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    email_from: str = os.getenv("EMAIL_FROM", "VigiRadar <info@vigiradar.com>")
    email_reply_to: str = os.getenv("EMAIL_REPLY_TO", "info@vigiradar.com")

    # Admin token guarding the ingestion trigger (/admin/ingest)
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    # Stripe (billing) — optional; checkout disabled until keys are set
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_price_pro_monthly: str = os.getenv("STRIPE_PRICE_PRO_MONTHLY", "")
    stripe_price_team_annual: str = os.getenv("STRIPE_PRICE_TEAM_ANNUAL", "")

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def email_enabled(self) -> bool:
        return bool(self.resend_api_key)


settings = Settings()
