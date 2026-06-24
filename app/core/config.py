from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


class Settings:
    app_name: str = os.getenv("APP_NAME", "Mafeteng Youth Development League")
    secret_key: str = os.getenv("SECRET_KEY", "change-this-before-production")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5432/youth_league",
    )
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", "storage/uploads"))
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    supabase_admin_photos_bucket: str = os.getenv(
        "SUPABASE_ADMIN_PHOTOS_BUCKET", "admin photos"
    )
    supabase_team_logos_bucket: str = os.getenv(
        "SUPABASE_TEAM_LOGOS_BUCKET", "team logos"
    )
    supabase_player_documents_bucket: str = os.getenv(
        "SUPABASE_PLAYER_DOCUMENTS_BUCKET", "player documents"
    )
    supabase_player_photos_bucket: str = os.getenv(
        "SUPABASE_PLAYER_PHOTOS_BUCKET", "player photos"
    )
    supabase_player_agreements_bucket: str = os.getenv(
        "SUPABASE_PLAYER_AGREEMENTS_BUCKET", "player agreements"
    )
    super_admin_name: str = os.getenv("SUPER_ADMIN_NAME", "League Super Admin")
    super_admin_email: str = os.getenv("SUPER_ADMIN_EMAIL", "admin@ydl.local")
    super_admin_password: str = os.getenv("SUPER_ADMIN_PASSWORD", "Admin123!")
    default_season_name: str = os.getenv(
        "DEFAULT_SEASON_NAME", "2026 Youth Development League"
    )
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", smtp_username)
    smtp_from_name: str = os.getenv("SMTP_FROM_NAME", "Mafeteng Youth League")
    email_code_minutes: int = int(os.getenv("EMAIL_CODE_MINUTES", "15"))
    login_code_minutes: int = int(os.getenv("LOGIN_CODE_MINUTES", "10"))
    session_cookie_name: str = "ydl_session"


settings = Settings()
