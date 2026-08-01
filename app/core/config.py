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


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("cockroachdb+psycopg://"):
        return "cockroachdb://" + database_url.removeprefix("cockroachdb+psycopg://")
    if database_url.startswith("cockroachdb://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return "cockroachdb://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgresql+psycopg://"):
        return "cockroachdb://" + database_url.removeprefix("postgresql+psycopg://")
    if database_url.startswith("postgres://"):
        return "cockroachdb://" + database_url.removeprefix("postgres://")
    return database_url


class Settings:
    app_name: str = os.getenv("APP_NAME", "Mafeteng Youth Development League")
    secret_key: str = os.getenv("SECRET_KEY", "change-this-before-production")
    database_url: str = _normalize_database_url(os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/youth_league",
    ))
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", "storage/uploads"))
    cloudinary_cloud_name: str = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    cloudinary_api_key: str = os.getenv("CLOUDINARY_API_KEY", "")
    cloudinary_api_secret: str = os.getenv("CLOUDINARY_API_SECRET", "")
    cloudinary_folder_prefix: str = os.getenv(
        "CLOUDINARY_FOLDER_PREFIX", "Mafeteng Youth League"
    ).strip().strip("/")
    seed_initial_super_admin: bool = os.getenv("SEED_INITIAL_SUPER_ADMIN", "false").strip().lower() in {"1", "true", "yes", "on"}
    super_admin_name: str = os.getenv("SUPER_ADMIN_NAME", "League Super Admin")
    super_admin_email: str = os.getenv("SUPER_ADMIN_EMAIL", "")
    super_admin_password: str = os.getenv("SUPER_ADMIN_PASSWORD", "")
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
