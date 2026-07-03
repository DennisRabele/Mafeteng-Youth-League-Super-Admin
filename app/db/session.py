from datetime import date

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.models import (
    AppAsset,
    Category,
    Season,
    SuperAdmin,
    User,
    UserRole,
)


connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine_kwargs = {"connect_args": connect_args, "future": True}
if ".pooler.supabase.com" in settings.database_url:
    engine_kwargs["poolclass"] = NullPool

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_schema_columns()
    with SessionLocal() as db:
        _seed_app_assets(db)
        _seed_super_admin(db)
        _seed_season_and_categories(db)
        db.commit()


def _ensure_schema_columns() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    missing_user_columns = {
        "photo_path": "ALTER TABLE users ADD COLUMN photo_path VARCHAR(500)",
        "email_verified": "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT FALSE NOT NULL",
        "email_verification_code_hash": "ALTER TABLE users ADD COLUMN email_verification_code_hash VARCHAR(255)",
        "email_verification_expires_at": "ALTER TABLE users ADD COLUMN email_verification_expires_at TIMESTAMP",
        "login_code_hash": "ALTER TABLE users ADD COLUMN login_code_hash VARCHAR(255)",
        "login_code_expires_at": "ALTER TABLE users ADD COLUMN login_code_expires_at TIMESTAMP",
    }
    with engine.begin() as connection:
        for column_name, statement in missing_user_columns.items():
            if column_name not in user_columns:
                connection.execute(text(statement))

    team_admin_columns = {column["name"] for column in inspector.get_columns("team_admins")}
    missing_team_admin_columns = {
        "requested_team_name": "ALTER TABLE team_admins ADD COLUMN requested_team_name VARCHAR(150) DEFAULT 'Unassigned Team' NOT NULL",
        "team_id": "ALTER TABLE team_admins ADD COLUMN team_id INTEGER REFERENCES teams(team_id)",
        "admin_code": "ALTER TABLE team_admins ADD COLUMN admin_code VARCHAR(80)",
        "rejection_reason": "ALTER TABLE team_admins ADD COLUMN rejection_reason TEXT",
        "approved_by_super_admin_id": "ALTER TABLE team_admins ADD COLUMN approved_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
    }
    with engine.begin() as connection:
        for column_name, statement in missing_team_admin_columns.items():
            if column_name not in team_admin_columns:
                connection.execute(text(statement))

    team_columns = {column["name"] for column in inspector.get_columns("teams")}
    missing_team_columns = {
        "team_address": "ALTER TABLE teams ADD COLUMN team_address VARCHAR(255)",
        "training_ground": "ALTER TABLE teams ADD COLUMN training_ground VARCHAR(150)",
        "home_ground": "ALTER TABLE teams ADD COLUMN home_ground VARCHAR(150)",
        "rejection_reason": "ALTER TABLE teams ADD COLUMN rejection_reason TEXT",
        "approved_by_super_admin_id": "ALTER TABLE teams ADD COLUMN approved_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
    }
    with engine.begin() as connection:
        for column_name, statement in missing_team_columns.items():
            if column_name not in team_columns:
                connection.execute(text(statement))

    player_columns = {column["name"] for column in inspector.get_columns("players")}
    missing_player_columns = {
        "email": "ALTER TABLE players ADD COLUMN email VARCHAR(255)",
        "residential_address": "ALTER TABLE players ADD COLUMN residential_address VARCHAR(255)",
        "registration_type": "ALTER TABLE players ADD COLUMN registration_type VARCHAR(40) DEFAULT 'new' NOT NULL",
        "registration_period": "ALTER TABLE players ADD COLUMN registration_period INTEGER DEFAULT 1 NOT NULL",
        "agreement_form_path": "ALTER TABLE players ADD COLUMN agreement_form_path VARCHAR(500)",
        "player_code": "ALTER TABLE players ADD COLUMN player_code VARCHAR(80)",
        "rejection_reason": "ALTER TABLE players ADD COLUMN rejection_reason TEXT",
        "approved_by_super_admin_id": "ALTER TABLE players ADD COLUMN approved_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
        "approved_at": "ALTER TABLE players ADD COLUMN approved_at TIMESTAMP",
        "is_on_loan": "ALTER TABLE players ADD COLUMN is_on_loan BOOLEAN DEFAULT FALSE NOT NULL",
        "original_team_id": "ALTER TABLE players ADD COLUMN original_team_id INTEGER REFERENCES teams(team_id)",
        "loan_end_date": "ALTER TABLE players ADD COLUMN loan_end_date DATE",
    }
    with engine.begin() as connection:
        for column_name, statement in missing_player_columns.items():
            if column_name not in player_columns:
                connection.execute(text(statement))

    if inspector.has_table("fixtures"):
        fixture_columns = {column["name"] for column in inspector.get_columns("fixtures")}
        missing_fixture_columns = {
            "created_by_super_admin_id": "ALTER TABLE fixtures ADD COLUMN created_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
        }
        with engine.begin() as connection:
            for column_name, statement in missing_fixture_columns.items():
                if column_name not in fixture_columns:
                    connection.execute(text(statement))

    if inspector.has_table("player_registration_requests"):
        registration_request_columns = {
            column["name"] for column in inspector.get_columns("player_registration_requests")
        }
        missing_registration_request_columns = {
            "agreement_form_path": "ALTER TABLE player_registration_requests ADD COLUMN agreement_form_path VARCHAR(500) DEFAULT '' NOT NULL",
            "registration_period": "ALTER TABLE player_registration_requests ADD COLUMN registration_period INTEGER DEFAULT 1 NOT NULL",
            "rejection_reason": "ALTER TABLE player_registration_requests ADD COLUMN rejection_reason TEXT",
            "submitted_at": "ALTER TABLE player_registration_requests ADD COLUMN submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL",
            "approved_by_super_admin_id": "ALTER TABLE player_registration_requests ADD COLUMN approved_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
        }
        with engine.begin() as connection:
            for column_name, statement in missing_registration_request_columns.items():
                if column_name not in registration_request_columns:
                    connection.execute(text(statement))

    if inspector.has_table("player_transfer_requests"):
        transfer_request_columns = {
            column["name"] for column in inspector.get_columns("player_transfer_requests")
        }
        missing_transfer_request_columns = {
            "registration_period": "ALTER TABLE player_transfer_requests ADD COLUMN registration_period INTEGER DEFAULT 1 NOT NULL",
            "loan_period": "ALTER TABLE player_transfer_requests ADD COLUMN loan_period VARCHAR(40)",
            "rejection_reason": "ALTER TABLE player_transfer_requests ADD COLUMN rejection_reason TEXT",
            "response_explanation": "ALTER TABLE player_transfer_requests ADD COLUMN response_explanation TEXT",
            "agreement_form_path": "ALTER TABLE player_transfer_requests ADD COLUMN agreement_form_path VARCHAR(500)",
            "consent_form_uploaded": "ALTER TABLE player_transfer_requests ADD COLUMN consent_form_uploaded BOOLEAN DEFAULT FALSE NOT NULL",
            "loan_end_date": "ALTER TABLE player_transfer_requests ADD COLUMN loan_end_date DATE",
            "created_at": "ALTER TABLE player_transfer_requests ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL",
            "responded_at": "ALTER TABLE player_transfer_requests ADD COLUMN responded_at TIMESTAMP",
            "completed_at": "ALTER TABLE player_transfer_requests ADD COLUMN completed_at TIMESTAMP",
            "approved_by_super_admin_id": "ALTER TABLE player_transfer_requests ADD COLUMN approved_by_super_admin_id INTEGER REFERENCES super_admins(admin_id)",
            "approved_by_super_admin_at": "ALTER TABLE player_transfer_requests ADD COLUMN approved_by_super_admin_at TIMESTAMP",
        }
        with engine.begin() as connection:
            for column_name, statement in missing_transfer_request_columns.items():
                if column_name not in transfer_request_columns:
                    connection.execute(text(statement))

    if inspector.has_table("match_result_submissions"):
        result_columns = {
            column["name"] for column in inspector.get_columns("match_result_submissions")
        }
        missing_result_columns = {
            "scorer_names_text": "ALTER TABLE match_result_submissions ADD COLUMN scorer_names_text TEXT",
            "goal_types_text": "ALTER TABLE match_result_submissions ADD COLUMN goal_types_text TEXT",
            "assist_names_text": "ALTER TABLE match_result_submissions ADD COLUMN assist_names_text TEXT",
        }
        with engine.begin() as connection:
            for column_name, statement in missing_result_columns.items():
                if column_name not in result_columns:
                    connection.execute(text(statement))


def _seed_app_assets(db: Session) -> None:
    assets = {
        "league_logo": (
            "/static/images/logo.jpg",
            "Main league logo used in the header and loading screen.",
            {"/static/images/logo.png"},
        ),
    }
    existing = {
        asset.asset_key: asset
        for asset in db.scalars(select(AppAsset)).all()
    }
    for asset_key, (url, description, old_local_urls) in assets.items():
        asset = existing.get(asset_key)
        if not asset:
            db.add(AppAsset(asset_key=asset_key, url=url, description=description))
        elif asset.url in old_local_urls:
            asset.url = url
            asset.description = description


def _seed_super_admin(db: Session) -> None:
    existing = db.scalar(
        select(User).where(User.email == settings.super_admin_email.lower())
    )
    if existing:
        return

    user = User(
        full_name=settings.super_admin_name,
        email=settings.super_admin_email.lower(),
        password_hash=hash_password(settings.super_admin_password),
        role=UserRole.SUPER_ADMIN.value,
        photo_path=None,
        email_verified=True,
    )
    db.add(user)
    db.flush()
    db.add(SuperAdmin(user_id=user.user_id))


def _seed_season_and_categories(db: Session) -> None:
    season = db.scalar(
        select(Season).where(Season.season_name == settings.default_season_name)
    )
    if not season:
        current_year = date.today().year
        season = Season(
            season_name=settings.default_season_name,
            start_date=date(current_year, 1, 1),
            end_date=date(current_year, 12, 31),
        )
        db.add(season)
        db.flush()

    category_names = [
        "Male U13",
        "Male U15",
        "Male U17",
        "Female U13",
        "Female U15",
        "Female U17",
        "Female U20",
    ]
    existing_names = set(
        db.scalars(select(Category.category_name).where(Category.season_id == season.season_id))
    )
    for category_name in category_names:
        if category_name not in existing_names:
            db.add(Category(category_name=category_name, season_id=season.season_id))
