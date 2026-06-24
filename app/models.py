from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    TEAM_ADMIN = "team_admin"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FixtureStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class TransferStatus(str, Enum):
    PENDING = "pending"
    PENDING_RESPONSE = "pending"
    REJECTED = "rejected"
    APPROVED = "approved"
    AGREED = "approved"
    REGISTERED = "approved"


class AppAsset(Base):
    __tablename__ = "app_assets"

    asset_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    photo_path: Mapped[str | None] = mapped_column(String(500))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verification_code_hash: Mapped[str | None] = mapped_column(String(255))
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    login_code_hash: Mapped[str | None] = mapped_column(String(255))
    login_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_recovery_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    account_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    account_suspension_expiry: Mapped[datetime | None] = mapped_column(DateTime)
    password_recovery_code_hash: Mapped[str | None] = mapped_column(String(255))
    password_recovery_expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    team_admin_profile: Mapped[TeamAdmin | None] = relationship(
        back_populates="user", uselist=False
    )
    super_admin_profile: Mapped[SuperAdmin | None] = relationship(
        back_populates="user", uselist=False
    )


class TeamAdmin(Base):
    __tablename__ = "team_admins"

    team_admin_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), unique=True)
    national_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_team_name: Mapped[str] = mapped_column(String(150), nullable=False)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    admin_code: Mapped[str | None] = mapped_column(String(80))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)

    user: Mapped[User] = relationship(back_populates="team_admin_profile")
    teams: Mapped[list[Team]] = relationship(
        back_populates="team_admin",
        foreign_keys="Team.team_admin_id"
    )
    assigned_team: Mapped[Team | None] = relationship(
        foreign_keys=[team_id],
        viewonly=True
    )
    match_result_submissions: Mapped[list[MatchResultSubmission]] = relationship(
        back_populates="submitted_by"
    )


class SuperAdmin(Base):
    __tablename__ = "super_admins"

    admin_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), unique=True)

    user: Mapped[User] = relationship(back_populates="super_admin_profile")
    news_posts: Mapped[list[News]] = relationship(back_populates="published_by")
    announcements: Mapped[list[Announcement]] = relationship(back_populates="published_by")
    result_verifications: Mapped[list[ResultVerification]] = relationship(
        back_populates="verified_by"
    )


class Season(Base):
    __tablename__ = "seasons"

    season_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_name: Mapped[str] = mapped_column(String(120), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    categories: Mapped[list[Category]] = relationship(back_populates="season")
    fixtures: Mapped[list[Fixture]] = relationship(back_populates="season")
    team_seasons: Mapped[list[TeamSeason]] = relationship(back_populates="season")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("season_id", "category_name"),)

    category_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"))
    category_name: Mapped[str] = mapped_column(String(80), nullable=False)

    season: Mapped[Season] = relationship(back_populates="categories")
    teams: Mapped[list[Team]] = relationship(back_populates="category")
    fixtures: Mapped[list[Fixture]] = relationship(back_populates="category")


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_admin_id: Mapped[int] = mapped_column(ForeignKey("team_admins.team_admin_id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.category_id"))
    team_name: Mapped[str] = mapped_column(String(150), nullable=False)
    logo: Mapped[str | None] = mapped_column(String(500))
    team_code: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)
    contact_information: Mapped[str] = mapped_column(String(255), nullable=False)
    team_address: Mapped[str | None] = mapped_column(String(255))
    training_ground: Mapped[str | None] = mapped_column(String(150))
    home_ground: Mapped[str | None] = mapped_column(String(150))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)

    team_admin: Mapped[TeamAdmin] = relationship(
        back_populates="teams",
        foreign_keys=[team_admin_id]
    )
    category: Mapped[Category] = relationship(back_populates="teams")
    team_seasons: Mapped[list[TeamSeason]] = relationship(back_populates="team")
    players: Mapped[list[Player]] = relationship(
        back_populates="team",
        foreign_keys="Player.team_id"
    )
    coaches: Mapped[list[Coach]] = relationship(back_populates="team")
    home_fixtures: Mapped[list[Fixture]] = relationship(
        back_populates="home_team", foreign_keys="Fixture.home_team_id"
    )
    away_fixtures: Mapped[list[Fixture]] = relationship(
        back_populates="away_team", foreign_keys="Fixture.away_team_id"
    )


class TeamSeason(Base):
    __tablename__ = "team_seasons"

    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), primary_key=True)
    registration_date: Mapped[date] = mapped_column(Date, default=date.today)

    team: Mapped[Team] = relationship(back_populates="team_seasons")
    season: Mapped[Season] = relationship(back_populates="team_seasons")


class Parent(Base):
    __tablename__ = "parents"

    parent_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    contact: Mapped[str] = mapped_column(String(80), nullable=False)

    players: Mapped[list[Player]] = relationship(back_populates="parent")


class Player(Base):
    __tablename__ = "players"

    player_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("parents.parent_id"))
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    gender: Mapped[str] = mapped_column(String(20), nullable=False)
    dob: Mapped[date] = mapped_column(Date, nullable=False)
    nationality: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    residential_address: Mapped[str | None] = mapped_column(String(255))
    school_name: Mapped[str | None] = mapped_column(String(150))
    position: Mapped[str | None] = mapped_column(String(80))
    photo_path: Mapped[str | None] = mapped_column(String(500))
    age_group: Mapped[str | None] = mapped_column(String(10))
    registration_type: Mapped[str] = mapped_column(String(40), default="new")
    registration_period: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    agreement_form_path: Mapped[str | None] = mapped_column(String(500))
    player_code: Mapped[str | None] = mapped_column(String(80))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_on_loan: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    original_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"))
    loan_end_date: Mapped[date | None] = mapped_column(Date)

    team: Mapped[Team] = relationship(back_populates="players", foreign_keys="Player.team_id")
    original_team: Mapped[Team | None] = relationship(foreign_keys="Player.original_team_id")
    parent: Mapped[Parent | None] = relationship(back_populates="players")
    documents: Mapped[list[PlayerDocument]] = relationship(back_populates="player")
    registration_requests: Mapped[list[PlayerRegistrationRequest]] = relationship(
        back_populates="player"
    )
    training_attendance: Mapped[list[TrainingAttendance]] = relationship(
        back_populates="player"
    )
    pdi_records: Mapped[list[PlayerPDI]] = relationship(back_populates="player")
    qr_player_card: Mapped[QRPlayerCard | None] = relationship(
        back_populates="player", uselist=False
    )
    match_events: Mapped[list[MatchEvent]] = relationship(back_populates="player")
    awards: Mapped[list[PlayerAward]] = relationship(back_populates="player")


class PlayerDocument(Base):
    __tablename__ = "player_documents"

    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    document_type: Mapped[str] = mapped_column(String(80), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)

    player: Mapped[Player] = relationship(back_populates="documents")


class PlayerRegistrationRequest(Base):
    __tablename__ = "player_registration_requests"

    registration_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    requested_by_team_admin_id: Mapped[int] = mapped_column(
        ForeignKey("team_admins.team_admin_id")
    )
    registration_type: Mapped[str] = mapped_column(String(40), nullable=False)
    agreement_form_path: Mapped[str] = mapped_column(String(500), nullable=False)
    registration_period: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)

    player: Mapped[Player] = relationship(back_populates="registration_requests")
    team: Mapped[Team] = relationship()
    requested_by: Mapped[TeamAdmin] = relationship()


class PlayerTransferRequest(Base):
    __tablename__ = "player_transfer_requests"

    transfer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    from_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    to_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    requested_by_team_admin_id: Mapped[int] = mapped_column(
        ForeignKey("team_admins.team_admin_id")
    )
    transfer_type: Mapped[str] = mapped_column(String(40), nullable=False)
    player_details: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_conditions: Mapped[str] = mapped_column(Text, nullable=False)
    registration_period: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    loan_period: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(40), default=ApprovalStatus.PENDING.value
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    response_explanation: Mapped[str | None] = mapped_column(Text)
    agreement_form_path: Mapped[str | None] = mapped_column(String(500))
    consent_form_uploaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    loan_end_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)
    approved_by_super_admin_at: Mapped[datetime | None] = mapped_column(DateTime)

    player: Mapped[Player] = relationship()
    from_team: Mapped[Team] = relationship(foreign_keys=[from_team_id])
    to_team: Mapped[Team] = relationship(foreign_keys=[to_team_id])
    requested_by: Mapped[TeamAdmin] = relationship()


class PlayerRequest(Base):
    __tablename__ = "player_requests"

    request_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    from_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    to_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    requested_by_team_admin_id: Mapped[int] = mapped_column(
        ForeignKey("team_admins.team_admin_id")
    )
    request_type: Mapped[str] = mapped_column(String(40), nullable=False)
    request_details: Mapped[str] = mapped_column(Text, nullable=False)
    registration_period: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_loan_period: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(40), default=ApprovalStatus.PENDING.value
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    response_explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("super_admins.admin_id"), nullable=True)
    approved_by_super_admin_at: Mapped[datetime | None] = mapped_column(DateTime)

    player: Mapped[Player] = relationship()
    from_team: Mapped[Team] = relationship(foreign_keys=[from_team_id])
    to_team: Mapped[Team] = relationship(foreign_keys=[to_team_id])
    requested_by: Mapped[TeamAdmin] = relationship()


class TrainingAttendance(Base):
    __tablename__ = "training_attendance"

    attendance_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)

    player: Mapped[Player] = relationship(back_populates="training_attendance")


class PlayerPDI(Base):
    __tablename__ = "player_pdi"

    pdi_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[str] = mapped_column(String(40), nullable=False)

    player: Mapped[Player] = relationship(back_populates="pdi_records")


class QRPlayerCard(Base):
    __tablename__ = "qr_player_cards"

    card_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"), unique=True)
    qr_code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)

    player: Mapped[Player] = relationship(back_populates="qr_player_card")


class PlayerAward(Base):
    __tablename__ = "player_awards"

    award_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    award_name: Mapped[str] = mapped_column(String(120), nullable=False)
    season: Mapped[str] = mapped_column(String(120), nullable=False)

    player: Mapped[Player] = relationship(back_populates="awards")


class Coach(Base):
    __tablename__ = "coaches"

    coach_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    qualification: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)

    team: Mapped[Team] = relationship(back_populates="coaches")
    awards: Mapped[list[CoachAward]] = relationship(back_populates="coach")


class CoachAward(Base):
    __tablename__ = "coach_awards"

    award_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coach_id: Mapped[int] = mapped_column(ForeignKey("coaches.coach_id"))
    award_name: Mapped[str] = mapped_column(String(120), nullable=False)
    season: Mapped[str] = mapped_column(String(120), nullable=False)

    coach: Mapped[Coach] = relationship(back_populates="awards")


class Referee(Base):
    __tablename__ = "referees"

    referee_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    qualification: Mapped[str] = mapped_column(String(150), nullable=False)

    assignments: Mapped[list[MatchOfficialAssignment]] = relationship(
        back_populates="referee"
    )


class Fixture(Base):
    __tablename__ = "fixtures"

    fixture_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.category_id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    fixture_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    venue: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=FixtureStatus.DRAFT.value)

    season: Mapped[Season] = relationship(back_populates="fixtures")
    category: Mapped[Category] = relationship(back_populates="fixtures")
    home_team: Mapped[Team] = relationship(
        back_populates="home_fixtures", foreign_keys=[home_team_id]
    )
    away_team: Mapped[Team] = relationship(
        back_populates="away_fixtures", foreign_keys=[away_team_id]
    )
    match: Mapped[Match | None] = relationship(back_populates="fixture", uselist=False)


class Match(Base):
    __tablename__ = "matches"

    match_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.fixture_id"), unique=True)
    match_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)

    fixture: Mapped[Fixture] = relationship(back_populates="match")
    match_events: Mapped[list[MatchEvent]] = relationship(back_populates="match")
    official_assignments: Mapped[list[MatchOfficialAssignment]] = relationship(
        back_populates="match"
    )
    result_submissions: Mapped[list[MatchResultSubmission]] = relationship(
        back_populates="match"
    )


class MatchEvent(Base):
    __tablename__ = "match_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.player_id"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)

    match: Mapped[Match] = relationship(back_populates="match_events")
    player: Mapped[Player | None] = relationship(back_populates="match_events")


class MatchOfficialAssignment(Base):
    __tablename__ = "match_official_assignments"

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"))
    referee_id: Mapped[int] = mapped_column(ForeignKey("referees.referee_id"))
    role: Mapped[str] = mapped_column(String(80), nullable=False)

    match: Mapped[Match] = relationship(back_populates="official_assignments")
    referee: Mapped[Referee] = relationship(back_populates="assignments")


class MatchResultSubmission(Base):
    __tablename__ = "match_result_submissions"

    submission_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"))
    submitted_by_team_admin_id: Mapped[int] = mapped_column(
        ForeignKey("team_admins.team_admin_id")
    )
    submitted_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(30), default=ApprovalStatus.PENDING.value)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)

    match: Mapped[Match] = relationship(back_populates="result_submissions")
    submitted_by: Mapped[TeamAdmin] = relationship(back_populates="match_result_submissions")
    verification: Mapped[ResultVerification | None] = relationship(
        back_populates="submission", uselist=False
    )


class ResultVerification(Base):
    __tablename__ = "result_verifications"

    verification_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("match_result_submissions.submission_id"), unique=True
    )
    verified_by_admin_id: Mapped[int] = mapped_column(ForeignKey("super_admins.admin_id"))
    verification_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)

    submission: Mapped[MatchResultSubmission] = relationship(back_populates="verification")
    verified_by: Mapped[SuperAdmin] = relationship(back_populates="result_verifications")


class News(Base):
    __tablename__ = "news"

    news_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    published_by_admin_id: Mapped[int] = mapped_column(ForeignKey("super_admins.admin_id"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    date_posted: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    published_by: Mapped[SuperAdmin] = relationship(back_populates="news_posts")


class Announcement(Base):
    __tablename__ = "announcements"

    announcement_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    published_by_admin_id: Mapped[int] = mapped_column(ForeignKey("super_admins.admin_id"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    date_posted: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    published_by: Mapped[SuperAdmin] = relationship(back_populates="announcements")
