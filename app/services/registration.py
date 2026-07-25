from collections import defaultdict
from datetime import date, datetime, timedelta
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.security import (
    generate_numeric_code,
    hash_one_time_code,
    hash_password,
    verify_one_time_code,
)
from app.models import (
    ApprovalStatus,
    Category,
    Parent,
    Player,
    PlayerDocument,
    PlayerRegistrationRequest,
    PlayerTransferRequest,
    QRPlayerCard,
    Season,
    SuperAdmin,
    Team,
    TeamAdmin,
    TeamSeason,
    TransferStatus,
    User,
    UserRole,
)
from app.services.team_access import load_team_admin_approved_team_ids, load_team_admin_primary_team


class RegistrationError(ValueError):
    pass


MAX_SUPER_ADMINS = 5
ID_PREFIX = "MDL00"
TRANSFERRED_STATUS = "transferred"
AGE_GROUP_MAX_AGE = {
    "U13": 13,
    "U15": 15,
    "U17": 17,
    "U20": 20,
}
PERSON_NAME_PATTERN = re.compile(r"^[A-Za-z]+(?:[A-Za-z\s'\-]*[A-Za-z])?$")
TEAM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[A-Za-z0-9\s'\-&]*[A-Za-z0-9])?$")
PHONE_PATTERN = re.compile(r"^[0-9+\-\s]+$")
logger = logging.getLogger(__name__)


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _validate_text(
    value: str | None,
    *,
    field_name: str,
    pattern: re.Pattern[str] | None = None,
    pattern_error: str | None = None,
    allow_empty: bool = False,
) -> str:
    normalized = _normalize_text(value)
    if not normalized and not allow_empty:
        raise RegistrationError(f"{field_name} is required.")
    if normalized and pattern and not pattern.fullmatch(normalized):
        raise RegistrationError(pattern_error or f"{field_name} contains invalid characters.")
    return normalized


def _validate_person_name(value: str | None, field_name: str) -> str:
    return _validate_text(
        value,
        field_name=field_name,
        pattern=PERSON_NAME_PATTERN,
        pattern_error=f"{field_name} can only contain letters, spaces, apostrophes, and hyphens.",
    )


def _validate_team_name(value: str | None) -> str:
    return _validate_text(
        value,
        field_name="Team name",
        pattern=TEAM_NAME_PATTERN,
        pattern_error="Team name can only contain letters, numbers, spaces, apostrophes, and hyphens.",
    )


def _validate_phone(value: str | None, field_name: str = "Phone number") -> str:
    return _validate_text(
        value,
        field_name=field_name,
        pattern=PHONE_PATTERN,
        pattern_error=f"{field_name} can only contain numbers, +, -, or spaces.",
    )


def _normalize_team_code(team_code: str | None) -> str | None:
    normalized = _normalize_text(team_code)
    return normalized.upper() if normalized else None


def _resolve_team_reference(
    db: Session,
    *,
    team_id: int | None = None,
    team_code: str | None = None,
) -> Team | None:
    if team_id is not None:
        return db.get(Team, team_id)
    normalized_code = _normalize_team_code(team_code)
    if not normalized_code:
        return None
    return db.scalar(
        select(Team).where(func.upper(Team.team_code) == normalized_code)
    )


def issue_email_verification_code(db: Session, user: User, *, commit: bool = True) -> str:
    code = generate_numeric_code()
    user.email_verification_code_hash = hash_one_time_code(code)
    user.email_verification_expires_at = datetime.utcnow() + timedelta(
        minutes=settings.email_code_minutes
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return code


def verify_email_code(db: Session, user: User, code: str) -> None:
    if user.email_verified:
        return
    if not user.email_verification_expires_at:
        raise RegistrationError("Verification code was not requested.")
    if user.email_verification_expires_at < datetime.utcnow():
        raise RegistrationError("Verification code has expired.")
    if not verify_one_time_code(code, user.email_verification_code_hash):
        raise RegistrationError("Invalid verification code.")

    user.email_verified = True
    user.email_verification_code_hash = None
    user.email_verification_expires_at = None
    db.commit()


def issue_login_code(db: Session, user: User) -> str:
    code = generate_numeric_code()
    user.login_code_hash = hash_one_time_code(code)
    user.login_code_expires_at = datetime.utcnow() + timedelta(
        minutes=settings.login_code_minutes
    )
    db.commit()
    return code


def verify_login_code(db: Session, user: User, code: str) -> None:
    if not user.login_code_expires_at:
        raise RegistrationError("Login code was not requested.")
    if user.login_code_expires_at < datetime.utcnow():
        raise RegistrationError("Login code has expired.")
    if not verify_one_time_code(code, user.login_code_hash):
        raise RegistrationError("Invalid one-time login code.")

    user.login_code_hash = None
    user.login_code_expires_at = None
    db.commit()


def issue_password_recovery_code(db: Session, user: User) -> str:
    """Issue a password recovery code. Max 2 per account lifetime, then 30-day suspension."""
    if user.account_suspended and user.account_suspension_expiry:
        if user.account_suspension_expiry > datetime.utcnow():
            raise RegistrationError(
                "Your account is suspended. You can request password recovery again after the suspension period."
            )
        else:
            # Suspension expired, reset counter
            user.account_suspended = False
            user.account_suspension_expiry = None
            user.password_recovery_count = 0

    if user.password_recovery_count >= 2:
        # Suspend account for 30 days
        user.account_suspended = True
        user.account_suspension_expiry = datetime.utcnow() + timedelta(days=30)
        db.commit()
        raise RegistrationError(
            "You have exceeded the maximum password recovery attempts (2). Your account is suspended for 30 days."
        )

    code = generate_numeric_code()
    user.password_recovery_code_hash = hash_one_time_code(code)
    user.password_recovery_expires_at = datetime.utcnow() + timedelta(
        minutes=settings.email_code_minutes
    )
    user.password_recovery_count += 1
    db.commit()
    return code


def verify_password_recovery_code(db: Session, user: User, code: str) -> None:
    """Verify password recovery code."""
    if not user.password_recovery_expires_at:
        raise RegistrationError("Password recovery code was not requested.")
    if user.password_recovery_expires_at < datetime.utcnow():
        raise RegistrationError("Password recovery code has expired.")
    if not verify_one_time_code(code, user.password_recovery_code_hash):
        raise RegistrationError("Invalid password recovery code.")


def reset_password(db: Session, user: User, new_password: str, code: str) -> None:
    """Reset user password after verifying recovery code."""
    verify_password_recovery_code(db, user, code)
    
    if len(new_password) < 8:
        raise RegistrationError("Password must be at least 8 characters long.")

    user.password_hash = hash_password(new_password)
    user.password_recovery_code_hash = None
    user.password_recovery_expires_at = None
    db.commit()


def _team_initials(team_name: str, *, max_letters: int | None = None) -> str:
    words = re.findall(r"[A-Za-z0-9]+", team_name.upper())
    initials = "".join(word[0] for word in words if word)
    if max_letters is not None:
        initials = initials[:max_letters]
    if initials:
        return initials

    compact = re.sub(r"[^A-Za-z0-9]", "", team_name.upper())
    fallback = compact[: max_letters or 2]
    return fallback or "TM"


def _first_two_team_initials(team_name: str) -> str:
    initials = _team_initials(team_name, max_letters=2)
    if len(initials) >= 2:
        return initials

    compact = re.sub(r"[^A-Za-z0-9]", "", team_name.upper())
    return (initials + compact[1:2] + "X")[:2]


def _next_code_number(db: Session, column) -> int:
    codes = db.scalars(select(column).where(column.is_not(None))).all()
    highest = 0
    for code in codes:
        match = re.match(rf"^{ID_PREFIX}(\d+)", code or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _player_category_code(player: Player) -> str:
    gender_prefix = "F" if player.gender.lower().startswith("f") else "M"
    if player.age_group and player.age_group.startswith("U"):
        return f"{gender_prefix}{player.age_group[1:]}"

    category_name = player.team.category.category_name if player.team and player.team.category else ""
    number_match = re.search(r"(13|15|17|20)", category_name)
    category_number = number_match.group(1) if number_match else "00"
    if category_name.lower().startswith("female"):
        gender_prefix = "F"
    elif category_name.lower().startswith("male"):
        gender_prefix = "M"
    return f"{gender_prefix}{category_number}"


def generate_team_code(db: Session, team: Team) -> str:
    """Generate team code in format: XX-CAT101MDL where XX = first 2 letters of team name, CAT = category name, 101+ = sequential number"""
    team_initials = _first_two_team_initials(team.team_name)
    category_code = team.category.category_name if team.category else "GEN"
    
    # Get all team codes for this category and extract the highest number
    all_teams = db.scalars(select(Team).where(Team.category_id == team.category_id)).all()
    highest_number = 100
    
    for t in all_teams:
        if t.team_code:
            # Extract number from code like "CA-U13101MDL"
            match = re.search(r"(\d{3})MDL$", t.team_code)
            if match:
                num = int(match.group(1))
                highest_number = max(highest_number, num)
    
    next_number = highest_number + 1
    team_code = f"{team_initials}-{category_code}{next_number}MDL"
    return team_code


def age_on(dob: date, reference: date | None = None) -> int:
    reference = reference or date.today()
    age = reference.year - dob.year
    if (reference.month, reference.day) < (dob.month, dob.day):
        age -= 1
    return age


def determine_age_group(dob: date, reference: date | None = None) -> str | None:
    age = age_on(dob, reference)
    if age < 0:
        return None
    if age <= 13:
        return "U13"
    if age <= 15:
        return "U15"
    if age <= 17:
        return "U17"
    if age <= 20:
        return "U20"
    return None


def suggested_registration_period(dob: date, reference: date | None = None) -> int | None:
    """Suggest the longest registration period the player's age still allows."""
    age_group = determine_age_group(dob, reference)
    if not age_group:
        return None
    max_age = AGE_GROUP_MAX_AGE.get(age_group)
    if max_age is None:
        return None
    age = age_on(dob, reference)
    return max(1, min(3, max_age - age + 1))


def determine_player_club_category(gender: str, dob: date, reference: date | None = None) -> str | None:
    age_group = determine_age_group(dob, reference)
    if not age_group:
        return None

    normalized_gender = (gender or "").strip().lower()
    if normalized_gender.startswith("male"):
        return f"Male {age_group}"
    if normalized_gender.startswith("female"):
        return f"Female {age_group}"
    return None


def _is_loan_transfer(transfer_type: str) -> bool:
    return "loan" in (transfer_type or "").strip().lower()


def _add_years(base_date: date, years: int) -> date:
    try:
        return base_date.replace(year=base_date.year + years)
    except ValueError:
        return base_date.replace(month=2, day=28, year=base_date.year + years)


def _loan_end_date_from_period(loan_period: str | None) -> date:
    normalized = (loan_period or "").strip().lower()
    if "6" in normalized and "month" in normalized:
        return date.today() + timedelta(days=180)
    if "1" in normalized and "year" in normalized:
        return date.today() + timedelta(days=365)
    return date.today() + timedelta(days=365)


def _player_registration_window(db: Session, player: Player) -> tuple[date | None, int | None]:
    if player.approved_at:
        return player.approved_at.date(), player.registration_period

    latest_approved_request = db.scalar(
        select(PlayerRegistrationRequest)
        .where(
            PlayerRegistrationRequest.player_id == player.player_id,
            PlayerRegistrationRequest.status == ApprovalStatus.APPROVED.value,
        )
        .order_by(PlayerRegistrationRequest.registration_id.desc())
    )
    if latest_approved_request:
        return latest_approved_request.submitted_at.date(), latest_approved_request.registration_period

    if player.registration_requests:
        first_request = min(player.registration_requests, key=lambda request: request.registration_id)
        return first_request.submitted_at.date(), player.registration_period

    return None, None


def _player_registration_expiry_date(db: Session, player: Player) -> date | None:
    start_date, registration_period = _player_registration_window(db, player)
    if not start_date or not registration_period:
        return None
    return _add_years(start_date, registration_period)


def get_player_registration_expiry_date(db: Session, player: Player) -> date | None:
    """Return the date when the player's current registration expires."""
    return _player_registration_expiry_date(db, player)


def _send_registration_expiry_reminders(db: Session) -> int:
    reminder_deadline = date.today() + timedelta(days=30)
    players = db.scalars(
        select(Player)
        .options(selectinload(Player.team))
        .where(
            Player.status == ApprovalStatus.APPROVED.value,
            Player.approved_at.is_not(None),
            Player.registration_reminder_sent_at.is_(None),
        )
    ).all()

    grouped: dict[int, list[tuple[Player, date]]] = defaultdict(list)
    for player in players:
        expiry_date = _player_registration_expiry_date(db, player)
        if not expiry_date or expiry_date <= date.today() or expiry_date > reminder_deadline:
            continue
        if not player.team or not player.team.team_admin_id:
            continue
        grouped[player.team.team_admin_id].append((player, expiry_date))

    reminders_sent = 0
    for team_admin_id, team_players in grouped.items():
        team_ids = sorted({player.team_id for player, _ in team_players if player.team_id})
        if not team_ids:
            continue

        lines = [
            f"{player.full_name} ({expiry_date.isoformat()})"
            for player, expiry_date in sorted(
                team_players,
                key=lambda item: (item[0].full_name.lower(), item[1]),
            )
        ]

        try:
            from app.services.league import notify_team_admins_for_teams

            notify_team_admins_for_teams(
                db,
                team_ids,
                "Player registration expiry reminder",
                (
                    "The following player registrations will expire within the next 30 days:\n"
                    + "\n".join(lines)
                    + "\n\nPlease renew them before the expiration date."
                ),
                "/team-admin/dashboard#my-players",
            )
        except Exception:
            continue

        for player, _ in team_players:
            player.registration_reminder_sent_at = datetime.utcnow()
        reminders_sent += 1

    if reminders_sent:
        db.commit()
    return reminders_sent


def process_player_registration_lifecycle(db: Session) -> dict[str, int]:
    """Process player registration reminders and expiry maintenance."""
    stats = {"reminders_sent": 0}
    try:
        stats["reminders_sent"] = _send_registration_expiry_reminders(db)
    except Exception:
        logger.exception("Player registration reminder processing failed")
    return stats


def _release_player_for_transfer(transfer: PlayerTransferRequest) -> None:
    """Hide or blur the source-team player once the owning team approves."""
    player = transfer.player
    if _is_loan_transfer(transfer.transfer_type):
        loan_end_date = _loan_end_date_from_period(transfer.loan_period)
        player.is_on_loan = True
        player.original_team_id = transfer.from_team_id
        player.loan_end_date = loan_end_date
        transfer.loan_end_date = loan_end_date
        return

    player.is_on_loan = False
    player.original_team_id = None
    player.loan_end_date = None
    player.status = TRANSFERRED_STATUS
    player.registration_type = "transferred_out"


def restore_expired_loans(db: Session) -> None:
    expired_players = db.scalars(
        select(Player).where(
            Player.is_on_loan.is_(True),
            Player.loan_end_date.is_not(None),
            Player.loan_end_date <= date.today(),
        )
    ).all()
    if not expired_players:
        return

    for player in expired_players:
        player.is_on_loan = False
        player.loan_end_date = None
    db.commit()


def create_super_admin_registration(
    db: Session,
    *,
    full_name: str,
    email: str,
    password: str,
    photo_path: str | None,
    commit: bool = True,
) -> SuperAdmin:
    super_admin_count = len(db.scalars(select(SuperAdmin.admin_id)).all())
    if super_admin_count >= MAX_SUPER_ADMINS:
        raise RegistrationError("The system already has the maximum of 5 Super Admins.")

    full_name = _validate_text(full_name, field_name="Full name")
    normalized_email = _validate_text(email, field_name="Email address").lower()
    existing_user = db.scalar(select(User).where(User.email == normalized_email))
    if existing_user:
        raise RegistrationError("This email is already registered.")
    if len(password) < 8:
        raise RegistrationError("Password must be at least 8 characters long.")

    user = User(
        full_name=full_name.strip(),
        email=normalized_email,
        password_hash=hash_password(password),
        role=UserRole.SUPER_ADMIN.value,
        photo_path=photo_path,
        email_verified=False,
    )
    db.add(user)
    db.flush()

    super_admin = SuperAdmin(user_id=user.user_id)
    db.add(super_admin)
    if commit:
        db.commit()
        db.refresh(super_admin)
    else:
        db.flush()
    return super_admin


def create_team_admin_registration(
    db: Session,
    *,
    full_name: str,
    team_name: str | None,
    email: str,
    password: str,
    national_id: str,
    phone: str,
    photo_path: str | None,
    team_id: int | None = None,
    team_code: str | None = None,
    commit: bool = True,
) -> TeamAdmin:
    full_name = _validate_person_name(full_name, "Full name")
    normalized_email = _validate_text(email, field_name="Email address").lower()
    phone = _validate_phone(phone)
    normalized_team_code = _normalize_team_code(team_code)
    normalized_national_id = _normalize_text(national_id)
    normalized_team_name = _normalize_text(team_name)
    existing_user = db.scalar(select(User).where(User.email == normalized_email))
    existing_id = db.scalar(
        select(TeamAdmin).where(TeamAdmin.national_id == normalized_national_id)
    )
    if existing_user or existing_id:
        raise RegistrationError("This email or national ID is already registered.")
    if len(password) < 8:
        raise RegistrationError("Password must be at least 8 characters long.")
    if not photo_path:
        raise RegistrationError("Profile photo is required.")
    team = _resolve_team_reference(db, team_id=team_id, team_code=normalized_team_code)
    if normalized_team_code and not team:
        raise RegistrationError("Specified team code does not exist.")
    if team_id is not None and not team:
        raise RegistrationError("Specified team does not exist.")
    if team:
        if team.status != ApprovalStatus.APPROVED.value:
            raise RegistrationError("This team must be approved before additional admins can join.")
        if normalized_team_name:
            normalized_team_name = _validate_team_name(normalized_team_name)
            if team.team_name.strip().casefold() != normalized_team_name.casefold():
                raise RegistrationError("Team code does not match the team name you entered.")
        else:
            normalized_team_name = team.team_name.strip()
        admin_count = len(db.scalars(select(TeamAdmin).where(TeamAdmin.team_id == team.team_id)).all())
        if admin_count >= 7:
            raise RegistrationError("Maximum number of team admins reached for this team.")
    else:
        if not normalized_team_name:
            raise RegistrationError("Team name is required for the first team admin registration.")
        normalized_team_name = _validate_team_name(normalized_team_name)

    user = User(
        full_name=full_name,
        email=normalized_email,
        password_hash=hash_password(password),
        role=UserRole.TEAM_ADMIN.value,
        photo_path=photo_path,
        email_verified=False,
    )
    db.add(user)
    db.flush()

    team_admin = TeamAdmin(
        user_id=user.user_id,
        national_id=normalized_national_id,
        phone=phone,
        requested_team_name=normalized_team_name,
        team_id=team.team_id if team else team_id,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(team_admin)
    if commit:
        db.commit()
        db.refresh(team_admin)
    else:
        db.flush()
    return team_admin


def get_team_admins_count(db: Session, team_id: int) -> int:
    """Get count of team admins (pending and approved) for a team."""
    return len(
        db.scalars(
            select(TeamAdmin).where(TeamAdmin.team_id == team_id)
        ).all()
    )


def is_first_team_admin_for_team(db: Session, team_id: int | None) -> bool:
    """Check if this is the first team admin registration for a team."""
    if not team_id:
        return True  # No team specified yet, so it's the first
    return get_team_admins_count(db, team_id) == 0


def approve_team_admin(
    db: Session,
    team_admin_id: int,
    approved_by_super_admin_id: int | None = None,
) -> TeamAdmin:
    team_admin = db.get(TeamAdmin, team_admin_id)
    if not team_admin:
        raise RegistrationError("Team Admin registration was not found.")
    
    # Immutability check
    if team_admin.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if team_admin.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This registration has already been approved.")

    if not team_admin.admin_code:
        sequence = _next_code_number(db, TeamAdmin.admin_code)
        team_admin.admin_code = (
            f"{ID_PREFIX}{sequence}{_team_initials(team_admin.requested_team_name)}"
        )
    team_admin.status = ApprovalStatus.APPROVED.value
    team_admin.rejection_reason = None
    team_admin.approved_by_super_admin_id = approved_by_super_admin_id
    db.commit()
    db.refresh(team_admin)
    try:
        from app.services.league import create_notification

        assigned_team = load_team_admin_primary_team(db, team_admin.team_admin_id)
        if assigned_team and assigned_team.team_code:
            approval_message = (
                "Your Team Admin registration has been approved. "
                f"Your team code is {assigned_team.team_code}. You can view it in My Account."
            )
        else:
            approval_message = (
                "Your Team Admin registration has been approved. "
                "If your team has already been registered by another admin, ask a colleague for the team code before registering your team."
            )
        create_notification(
            db,
            user_id=team_admin.user_id,
            title="Team Admin approved",
            message=approval_message,
            link="/team-admin/welcome",
        )
    except Exception:
        pass
    return team_admin


def reject_team_admin(db: Session, team_admin_id: int, rejection_reason: str) -> TeamAdmin:
    team_admin = db.get(TeamAdmin, team_admin_id)
    if not team_admin:
        raise RegistrationError("Team Admin registration was not found.")
    
    # Immutability check
    if team_admin.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved registration. Approved registrations cannot be changed.")
    if team_admin.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This registration has already been rejected.")
    
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")

    team_admin.status = ApprovalStatus.REJECTED.value
    team_admin.rejection_reason = rejection_reason.strip()
    db.commit()
    db.refresh(team_admin)
    try:
        from app.services.league import create_notification

        create_notification(
            db,
            user_id=team_admin.user_id,
            title="Team Admin rejected",
            message=f"Your Team Admin registration was rejected: {team_admin.rejection_reason}",
            link="/login",
        )
    except Exception:
        pass
    return team_admin


def register_team(
    db: Session,
    *,
    team_admin_id: int,
    team_name: str | None,
    category_id: int,
    contact_information: str,
    team_address: str,
    training_ground: str,
    home_ground: str,
    logo: str | None,
    team_code: str | None = None,
) -> Team:
    category = db.get(Category, category_id)
    if not category:
        raise RegistrationError("Selected category does not exist.")
    normalized_team_name = _normalize_text(team_name)
    normalized_team_code = _normalize_team_code(team_code)
    contact_information = _validate_phone(contact_information, "Contact information")
    team_address = _validate_text(team_address, field_name="Team address")
    training_ground = _validate_text(training_ground, field_name="Training ground")
    home_ground = _validate_text(home_ground, field_name="Home ground")
    if not logo:
        raise RegistrationError("Team logo is required.")

    team = _resolve_team_reference(db, team_code=normalized_team_code)
    if normalized_team_code and not team:
        raise RegistrationError("Specified team code does not exist.")
    if team:
        if team.status != ApprovalStatus.APPROVED.value:
            raise RegistrationError("This team must be approved before another registration can use its code.")
        if team.category_id != category_id:
            raise RegistrationError("Selected category must match the team code you entered.")
        if normalized_team_name:
            normalized_team_name = _validate_team_name(normalized_team_name)
            if team.team_name.strip().casefold() != normalized_team_name.casefold():
                raise RegistrationError("Team name must match the team code you entered.")
        else:
            normalized_team_name = team.team_name.strip()
    else:
        if not normalized_team_name:
            raise RegistrationError("Team name is required for the first team registration.")
        normalized_team_name = _validate_team_name(normalized_team_name)

    if not team:
        duplicate_team = db.scalar(
            select(Team).where(
                Team.category_id == category_id,
                func.lower(Team.team_name) == normalized_team_name.casefold(),
            )
        )
        if duplicate_team:
            raise RegistrationError("This team name is already registered for the selected category.")

    team = Team(
        team_admin_id=team_admin_id,
        category_id=category_id,
        team_name=normalized_team_name,
        contact_information=contact_information,
        team_address=team_address,
        training_ground=training_ground,
        home_ground=home_ground,
        logo=logo,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def approve_team(
    db: Session,
    team_id: int,
    approved_by_super_admin_id: int | None = None,
) -> Team:
    team = db.get(Team, team_id)
    if not team:
        raise RegistrationError("Team was not found.")
    
    # Immutability check
    if team.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if team.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This registration has already been approved.")

    # Generate team code
    if not team.team_code:
        team.team_code = generate_team_code(db, team)
    if team.team_admin and team.team_admin.team_id is None:
        team.team_admin.team_id = team.team_id
    
    team.status = ApprovalStatus.APPROVED.value
    team.rejection_reason = None
    team.approved_by_super_admin_id = approved_by_super_admin_id
    
    season = db.scalar(select(Season).order_by(Season.start_date.desc()))
    if season:
        exists = db.scalar(
            select(TeamSeason).where(
                TeamSeason.team_id == team.team_id,
                TeamSeason.season_id == season.season_id,
            )
        )
        if not exists:
            db.add(TeamSeason(team_id=team.team_id, season_id=season.season_id))

    db.commit()
    db.refresh(team)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            team.team_id,
            "Team approved",
            f"Your team {team.team_name} has been approved and can now participate in league activities.",
            "/team-admin/dashboard",
        )
    except Exception:
        pass
    return team


def reject_team(db: Session, team_id: int, rejection_reason: str) -> Team:
    team = db.get(Team, team_id)
    if not team:
        raise RegistrationError("Team was not found.")
    
    # Immutability check
    if team.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved registration. Approved registrations cannot be changed.")
    if team.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This registration has already been rejected.")
    
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")

    team.status = ApprovalStatus.REJECTED.value
    team.rejection_reason = rejection_reason.strip()
    db.commit()
    db.refresh(team)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            team.team_id,
            "Team rejected",
            f"Your team {team.team_name} was rejected: {team.rejection_reason}",
            "/team-admin/dashboard",
        )
    except Exception:
        pass
    return team


def register_player(
    db: Session,
    *,
    team_id: int,
    full_name: str,
    gender: str,
    dob: date,
    nationality: str,
    email: str | None,
    residential_address: str | None,
    parent_name: str,
    parent_contact: str,
    school_name: str | None,
    position: str | None,
    agreement_form_path: str | None,
    photo_path: str | None,
    documents: list[tuple[str, str]],
    registration_period: int = 1,
) -> Player:
    team = db.get(Team, team_id)
    if not team:
        raise RegistrationError("Selected team does not exist.")
    if team.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Players can only be submitted for approved teams.")

    full_name = _validate_person_name(full_name, "Player full name")
    gender = _validate_text(gender, field_name="Gender")
    nationality = _validate_person_name(nationality, "Nationality")
    parent_name = _validate_person_name(parent_name, "Parent/Guardian name")
    parent_contact = _validate_phone(parent_contact, "Parent contact")
    if agreement_form_path is not None:
        agreement_form_path = _normalize_text(agreement_form_path) or None
    if agreement_form_path is None:
        raise RegistrationError("Parent/Guardian Consent Form is required.")

    age_group = determine_age_group(dob)
    eligible_category = determine_player_club_category(gender, dob)
    max_registration_period = suggested_registration_period(dob)
    if not age_group:
        raise RegistrationError("Player is not eligible for any youth age category.")
    if not eligible_category:
        raise RegistrationError("Player gender must be Male or Female.")
    if not max_registration_period:
        raise RegistrationError("Player is not eligible for any youth age category.")

    team_category_name = team.category.category_name if team.category else None
    if not team_category_name:
        raise RegistrationError("Selected team category is not configured.")
    if team_category_name.casefold() != eligible_category.casefold():
        raise RegistrationError(
            f"This player qualifies for {eligible_category}, but the selected team is registered as {team_category_name}. Registration cannot continue."
        )

    if registration_period not in (1, 2, 3):
        raise RegistrationError("Registration period must be 1, 2, or 3 years.")
    if registration_period > max_registration_period:
        raise RegistrationError(
            f"This player is eligible for {age_group} and can only be registered for up to {max_registration_period} year(s)."
        )

    parent = None
    if parent_name.strip() and parent_contact.strip():
        parent = Parent(name=parent_name.strip(), contact=parent_contact.strip())
        db.add(parent)
        db.flush()

    player = Player(
        team_id=team_id,
        parent_id=parent.parent_id if parent else None,
        full_name=full_name,
        gender=gender,
        dob=dob,
        nationality=nationality,
        email=email.strip().lower() if email else None,
        residential_address=residential_address.strip() if residential_address else None,
        school_name=school_name.strip() if school_name else None,
        position=position.strip() if position else None,
        registration_type="new",
        registration_period=registration_period,
        agreement_form_path=agreement_form_path,
        photo_path=photo_path,
        age_group=age_group,
        rejection_reason=(
            None
            if age_group
            else "Player is not eligible for any youth age category."
        ),
        registration_reminder_sent_at=None,
        status=(
            ApprovalStatus.PENDING.value
            if age_group
            else ApprovalStatus.REJECTED.value
        ),
        approved_at=None,
    )
    db.add(player)
    db.flush()

    for document_type, file_path in documents:
        db.add(
            PlayerDocument(
                player_id=player.player_id,
                document_type=document_type,
                file_path=file_path,
            )
        )

    if agreement_form_path:
        db.add(
            PlayerRegistrationRequest(
                player_id=player.player_id,
                team_id=team.team_id,
                requested_by_team_admin_id=team.team_admin_id,
                registration_type="new",
                agreement_form_path=agreement_form_path,
                registration_period=registration_period,
                status=player.status,
                rejection_reason=player.rejection_reason,
            )
        )

    db.commit()
    db.refresh(player)
    return player


def renew_player_registration(
    db: Session,
    *,
    team_admin_id: int,
    player_id: int,
    agreement_form_path: str,
    registration_period: int = 1,
) -> PlayerRegistrationRequest:
    player = db.get(Player, player_id)
    accessible_team_ids = set(load_team_admin_approved_team_ids(db, team_admin_id))
    if not player or not player.team or player.team_id not in accessible_team_ids:
        raise RegistrationError("You can only renew players from your own teams.")
    if player.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Only approved players can be renewed.")
    if player.is_on_loan:
        raise RegistrationError("This player is currently on loan and cannot be renewed.")
    agreement_form_path = _normalize_text(agreement_form_path) or None
    if not agreement_form_path:
        raise RegistrationError("Player-club agreement form is required.")
    if registration_period not in (1, 2, 3):
        raise RegistrationError("Registration period must be 1, 2, or 3 years.")
    expiry_date = _player_registration_expiry_date(db, player)
    if expiry_date is None:
        raise RegistrationError("The current registration period for this player could not be determined.")
    if date.today() < expiry_date:
        raise RegistrationError(
            f"This player's current registration is valid until {expiry_date.isoformat()}. Renewal can only be made after that date."
        )

    existing_pending_request = db.scalar(
        select(PlayerRegistrationRequest).where(
            PlayerRegistrationRequest.player_id == player.player_id,
            PlayerRegistrationRequest.registration_type == "renewal",
            PlayerRegistrationRequest.status == ApprovalStatus.PENDING.value,
        )
    )
    if existing_pending_request:
        raise RegistrationError("A pending renewal request already exists for this player.")

    player.agreement_form_path = agreement_form_path
    request = PlayerRegistrationRequest(
        player_id=player.player_id,
        team_id=player.team_id,
        requested_by_team_admin_id=team_admin_id,
        registration_type="renewal",
        agreement_form_path=agreement_form_path,
        registration_period=registration_period,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def request_player_transfer(
    db: Session,
    *,
    team_admin_id: int,
    player_id: int,
    to_team_id: int,
    transfer_type: str,
    player_details: str,
    transfer_conditions: str,
    registration_period: int = 1,
    loan_period: str | None = None,
) -> PlayerTransferRequest:
    player = db.get(Player, player_id)
    to_team = db.get(Team, to_team_id)
    accessible_team_ids = set(load_team_admin_approved_team_ids(db, team_admin_id))
    if not player or not player.team or player.team_id not in accessible_team_ids:
        raise RegistrationError("You can only transfer players from your own teams.")
    if not to_team:
        raise RegistrationError("Selected destination team was not found.")
    if to_team.team_id in accessible_team_ids:
        raise RegistrationError("Destination team must belong to another Team Admin.")
    transfer_type = _validate_text(transfer_type, field_name="Transfer type")
    player_details = _validate_text(player_details, field_name="Player details")
    transfer_conditions = _validate_text(transfer_conditions, field_name="Transfer conditions")
    if not transfer_conditions:
        raise RegistrationError("Transfer conditions are required.")
    if registration_period not in (1, 2, 3):
        raise RegistrationError("Registration period must be 1, 2, or 3 years.")

    request = PlayerTransferRequest(
        player_id=player.player_id,
        from_team_id=player.team_id,
        to_team_id=to_team.team_id,
        requested_by_team_admin_id=team_admin_id,
        transfer_type=transfer_type,
        player_details=player_details or player.full_name,
        transfer_conditions=transfer_conditions,
        registration_period=registration_period,
        loan_period=loan_period if _is_loan_transfer(transfer_type) else None,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def request_player_from_team(
    db: Session,
    *,
    team_admin_id: int,
    player_id: int,
    from_team_id: int,
    to_team_id: int,
    request_type: str,
    request_details: str,
    registration_period: int = 1,
    request_loan_period: str | None = None,
) -> PlayerTransferRequest:
    player = db.get(Player, player_id)
    from_team = db.get(Team, from_team_id)
    to_team = db.get(Team, to_team_id)
    accessible_team_ids = set(load_team_admin_approved_team_ids(db, team_admin_id))
    if not player or not player.team or not from_team or player.team_id != from_team_id:
        raise RegistrationError("Invalid player or player not found in the selected team.")
    if player.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Only approved players can be requested for transfer.")
    if player.is_on_loan:
        raise RegistrationError("This player is currently on loan and cannot be requested.")
    if not to_team or to_team.team_id not in accessible_team_ids:
        raise RegistrationError("You can only request players for your own approved team.")
    if from_team.team_id in accessible_team_ids:
        raise RegistrationError("You cannot request a player from your own team.")
    if from_team.status != ApprovalStatus.APPROVED.value or to_team.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Both teams must be approved before a transfer request can be sent.")
    if registration_period not in (1, 2, 3):
        raise RegistrationError("Registration period must be 1, 2, or 3 years.")
    request_type = _validate_text(request_type, field_name="Request type")
    request_details = _validate_text(request_details, field_name="Transfer request details")
    if not request_details:
        raise RegistrationError("Transfer request details are required.")

    transfer_type = "Loan Transfer" if request_type == "Loan Request" else "Permanent Transfer"
    if _is_loan_transfer(transfer_type) and not (request_loan_period or "").strip():
        raise RegistrationError("Loan period is required for loan transfers.")

    existing_request = db.scalar(
        select(PlayerTransferRequest).where(
            PlayerTransferRequest.player_id == player.player_id,
            PlayerTransferRequest.to_team_id == to_team.team_id,
            PlayerTransferRequest.status == ApprovalStatus.PENDING.value,
        )
    )
    if existing_request:
        raise RegistrationError("A pending transfer request already exists for this player and team.")

    request = PlayerTransferRequest(
        player_id=player.player_id,
        from_team_id=from_team.team_id,
        to_team_id=to_team.team_id,
        requested_by_team_admin_id=team_admin_id,
        transfer_type=transfer_type,
        player_details=f"{player.full_name} requested by {to_team.team_name}",
        transfer_conditions=request_details.strip(),
        registration_period=registration_period,
        loan_period=request_loan_period.strip() if _is_loan_transfer(transfer_type) else None,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def respond_to_transfer(
    db: Session,
    *,
    team_admin_id: int,
    transfer_id: int,
    decision: str,
    rejection_reason: str | None = None,
    explanation: str | None = None,
) -> PlayerTransferRequest:
    request = db.get(PlayerTransferRequest, transfer_id)
    if not request:
        raise RegistrationError("Transfer request was not found for your team.")
    accessible_team_ids = set(load_team_admin_approved_team_ids(db, team_admin_id))
    if request.to_team_id not in accessible_team_ids and request.from_team_id not in accessible_team_ids:
        raise RegistrationError("Transfer request was not found for your team.")
    if request.status != ApprovalStatus.PENDING.value:
        raise RegistrationError("This transfer request has already been answered.")

    normalized_decision = decision.strip().lower()
    if normalized_decision in {"approved", "approve", "agree", "agreed"}:
        request.status = ApprovalStatus.APPROVED.value
        _release_player_for_transfer(request)
    elif normalized_decision in {"rejected", "reject", "decline", "declined"}:
        if not rejection_reason or not rejection_reason.strip():
            raise RegistrationError("A rejection reason is required.")
        request.status = ApprovalStatus.REJECTED.value
        request.rejection_reason = rejection_reason.strip()
    else:
        raise RegistrationError("Invalid transfer decision.")
    request.response_explanation = explanation.strip() if explanation else None
    request.responded_at = datetime.utcnow()
    db.commit()
    db.refresh(request)
    return request


def complete_transfer_registration(
    db: Session,
    *,
    team_admin_id: int,
    transfer_id: int,
    agreement_form_path: str | None,
) -> PlayerTransferRequest:
    request = db.get(PlayerTransferRequest, transfer_id)
    if not request or request.to_team_id not in set(load_team_admin_approved_team_ids(db, team_admin_id)):
        raise RegistrationError("Transfer request was not found for your team.")
    if request.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Transfer must be approved before registration.")
    agreement_form_path = _normalize_text(agreement_form_path) or None
    if not agreement_form_path:
        raise RegistrationError("Parent/Guardian Consent Form is required for transfer registration.")

    register_transferred_player(
        db,
        transfer_id=transfer_id,
        team_admin_id=team_admin_id,
        consent_form_path=agreement_form_path,
    )
    db.refresh(request)
    return request


def approve_player(
    db: Session,
    player_id: int,
    approved_by_super_admin_id: int | None = None,
) -> Player:
    player = db.get(Player, player_id)
    if not player:
        raise RegistrationError("Player was not found.")
    if not player.age_group:
        raise RegistrationError("Player is not eligible for any youth age category.")
    
    # Immutability check
    if player.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if player.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This registration has already been approved.")

    player.status = ApprovalStatus.APPROVED.value
    player.approved_by_super_admin_id = approved_by_super_admin_id
    player.approved_at = datetime.utcnow()
    player.registration_reminder_sent_at = None
    if not player.player_code:
        sequence = _next_code_number(db, Player.player_code)
        player.player_code = (
            f"{ID_PREFIX}{sequence}"
            f"{_first_two_team_initials(player.team.team_name)}"
            f"{_player_category_code(player)}"
        )
    player.rejection_reason = None
    if not player.qr_player_card:
        db.add(
            QRPlayerCard(
                player_id=player.player_id,
                qr_code=player.player_code,
                issue_date=date.today(),
            )
        )

    db.commit()
    db.refresh(player)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            player.team_id,
            "Player approved",
            f"{player.full_name} has been approved and is now an official league player.",
            "/team-admin/dashboard#my-players",
        )
    except Exception:
        pass
    return player


def reject_player(db: Session, player_id: int, rejection_reason: str) -> Player:
    player = db.get(Player, player_id)
    if not player:
        raise RegistrationError("Player was not found.")
    
    # Immutability check
    if player.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved registration. Approved registrations cannot be changed.")
    if player.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This registration has already been rejected.")
    
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")

    player.status = ApprovalStatus.REJECTED.value
    player.rejection_reason = rejection_reason.strip()
    db.commit()
    db.refresh(player)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            player.team_id,
            "Player rejected",
            f"{player.full_name} was rejected: {player.rejection_reason}",
            "/team-admin/dashboard#my-players",
        )
    except Exception:
        pass
    return player


def approve_renewal(db: Session, registration_id: int, approved_by_super_admin_id: int) -> PlayerRegistrationRequest:
    """Approve a player renewal registration."""
    request = db.get(PlayerRegistrationRequest, registration_id)
    if not request:
        raise RegistrationError("Registration request was not found.")
    if request.registration_type != "renewal":
        raise RegistrationError("This is not a renewal registration.")
    
    # Immutability check
    if request.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if request.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This registration has already been approved.")
    
    request.status = ApprovalStatus.APPROVED.value
    request.approved_by_super_admin_id = approved_by_super_admin_id
    request.rejection_reason = None
    request.player.status = ApprovalStatus.APPROVED.value
    request.player.registration_period = request.registration_period
    request.player.approved_by_super_admin_id = approved_by_super_admin_id
    request.player.approved_at = datetime.utcnow()
    request.player.registration_reminder_sent_at = None
    request.player.rejection_reason = None
    db.commit()
    db.refresh(request)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            request.team_id,
            "Renewal approved",
            f"The renewal for {request.player.full_name} has been approved.",
            "/team-admin/dashboard#my-players",
        )
    except Exception:
        pass
    return request


def reject_renewal(db: Session, registration_id: int, rejection_reason: str) -> PlayerRegistrationRequest:
    """Reject a player renewal registration."""
    request = db.get(PlayerRegistrationRequest, registration_id)
    if not request:
        raise RegistrationError("Registration request was not found.")
    if request.registration_type != "renewal":
        raise RegistrationError("This is not a renewal registration.")
    
    # Immutability check
    if request.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved registration. Approved registrations cannot be changed.")
    if request.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This registration has already been rejected.")
    
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")
    
    request.status = ApprovalStatus.REJECTED.value
    request.rejection_reason = rejection_reason.strip()
    db.commit()
    db.refresh(request)
    try:
        from app.services.league import notify_team_admin

        notify_team_admin(
            db,
            request.team_id,
            "Renewal rejected",
            f"The renewal for {request.player.full_name} was rejected: {request.rejection_reason}",
            "/team-admin/dashboard#my-players",
        )
    except Exception:
        pass
    return request


def _transfer_for_registration(
    db: Session,
    request: PlayerRegistrationRequest,
) -> PlayerTransferRequest | None:
    if not request.agreement_form_path:
        return None
    return db.scalar(
        select(PlayerTransferRequest)
        .where(PlayerTransferRequest.agreement_form_path == request.agreement_form_path)
        .order_by(PlayerTransferRequest.transfer_id.desc())
    )


def approve_transfer_registration(
    db: Session,
    registration_id: int,
    approved_by_super_admin_id: int,
) -> PlayerRegistrationRequest:
    """Approve a completed transfer registration and activate the new-team player."""
    request = db.get(PlayerRegistrationRequest, registration_id)
    if not request:
        raise RegistrationError("Transfer registration request was not found.")
    if request.registration_type != "transfer":
        raise RegistrationError("This is not a transfer registration.")
    if request.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if request.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This registration has already been approved.")

    player = request.player
    if player.status != ApprovalStatus.APPROVED.value:
        approve_player(db, player.player_id, approved_by_super_admin_id)
        request = db.get(PlayerRegistrationRequest, registration_id)

    request.status = ApprovalStatus.APPROVED.value
    request.approved_by_super_admin_id = approved_by_super_admin_id
    request.rejection_reason = None

    transfer = _transfer_for_registration(db, request)
    if transfer:
        transfer.approved_by_super_admin_id = approved_by_super_admin_id
        transfer.approved_by_super_admin_at = datetime.utcnow()

    db.commit()
    db.refresh(request)
    try:
        from app.services.league import notify_team_admins_for_teams

        team_ids = [request.team_id]
        if request.player and request.player.team_id:
            team_ids.append(request.player.team_id)
        notify_team_admins_for_teams(
            db,
            team_ids,
            "Transfer approved",
            f"The transfer registration for {request.player.full_name} has been approved.",
            "/team-admin/dashboard#transfers",
        )
    except Exception:
        pass
    return request


def reject_transfer_registration(
    db: Session,
    registration_id: int,
    rejection_reason: str,
) -> PlayerRegistrationRequest:
    """Reject a completed transfer registration and keep it visible to the new team."""
    request = db.get(PlayerRegistrationRequest, registration_id)
    if not request:
        raise RegistrationError("Transfer registration request was not found.")
    if request.registration_type != "transfer":
        raise RegistrationError("This is not a transfer registration.")
    if request.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved registration. Approved registrations cannot be changed.")
    if request.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This registration has already been rejected.")
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")

    request.status = ApprovalStatus.REJECTED.value
    request.rejection_reason = rejection_reason.strip()
    request.player.status = ApprovalStatus.REJECTED.value
    request.player.rejection_reason = rejection_reason.strip()

    db.commit()
    db.refresh(request)
    try:
        from app.services.league import notify_team_admins_for_teams

        team_ids = [request.team_id]
        if request.player and request.player.team_id:
            team_ids.append(request.player.team_id)
        notify_team_admins_for_teams(
            db,
            team_ids,
            "Transfer rejected",
            f"The transfer registration for {request.player.full_name} was rejected: {request.rejection_reason}",
            "/team-admin/dashboard#transfers",
        )
    except Exception:
        pass
    return request


def approve_transfer(db: Session, transfer_id: int, approved_by_super_admin_id: int) -> PlayerTransferRequest:
    """Approve a player transfer request."""
    transfer = db.get(PlayerTransferRequest, transfer_id)
    if not transfer:
        raise RegistrationError("Transfer request was not found.")
    
    # Immutability check
    if transfer.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("Cannot approve a rejected registration. Rejected registrations are permanent.")
    if transfer.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("This transfer has already been approved.")
    
    transfer.status = ApprovalStatus.APPROVED.value
    transfer.approved_by_super_admin_id = approved_by_super_admin_id
    transfer.approved_by_super_admin_at = datetime.utcnow()
    transfer.rejection_reason = None
    db.commit()
    db.refresh(transfer)
    try:
        from app.services.league import notify_team_admins_for_teams

        notify_team_admins_for_teams(
            db,
            [transfer.from_team_id, transfer.to_team_id],
            "Transfer request approved",
            f"The transfer request for {transfer.player.full_name} has been approved.",
            "/team-admin/dashboard#transfers",
        )
    except Exception:
        pass
    return transfer


def reject_transfer(db: Session, transfer_id: int, rejection_reason: str) -> PlayerTransferRequest:
    """Reject a player transfer request."""
    transfer = db.get(PlayerTransferRequest, transfer_id)
    if not transfer:
        raise RegistrationError("Transfer request was not found.")
    
    # Immutability check
    if transfer.status == ApprovalStatus.APPROVED.value:
        raise RegistrationError("Cannot reject an approved transfer. Approved transfers cannot be changed.")
    if transfer.status == ApprovalStatus.REJECTED.value:
        raise RegistrationError("This transfer has already been rejected.")
    
    if not rejection_reason.strip():
        raise RegistrationError("A rejection reason is required.")
    
    transfer.status = ApprovalStatus.REJECTED.value
    transfer.rejection_reason = rejection_reason.strip()
    db.commit()
    db.refresh(transfer)
    try:
        from app.services.league import notify_team_admins_for_teams

        notify_team_admins_for_teams(
            db,
            [transfer.from_team_id, transfer.to_team_id],
            "Transfer request rejected",
            f"The transfer request for {transfer.player.full_name} was rejected: {transfer.rejection_reason}",
            "/team-admin/dashboard#transfers",
        )
    except Exception:
        pass
    return transfer


def register_transferred_player(
    db: Session,
    *,
    transfer_id: int,
    team_admin_id: int,
    consent_form_path: str,
) -> Player:
    """Register a player after transfer is approved by both teams. Creates new player record for receiving team."""
    transfer = db.get(PlayerTransferRequest, transfer_id)
    if not transfer:
        raise RegistrationError("Transfer request was not found.")
    
    if transfer.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Transfer must be approved before registration.")
    
    # Verify requesting team admin is from the receiving team
    to_team = db.get(Team, transfer.to_team_id)
    if not to_team or to_team.team_id not in set(load_team_admin_approved_team_ids(db, team_admin_id)):
        raise RegistrationError("You can only register transfers for your own team.")
    consent_form_path = _normalize_text(consent_form_path) or None
    if not consent_form_path:
        raise RegistrationError("Parent/Guardian Consent Form is required for transfer registration.")
    if transfer.completed_at:
        raise RegistrationError("This transfer registration has already been submitted.")
    
    # Get the original player
    player = transfer.player
    
    # Create documents list for new player
    documents: list[tuple[str, str]] = []
    
    # Add existing identity/medical documents from original player.
    if player.documents:
        for doc in player.documents:
            if "consent" not in doc.document_type.lower():
                documents.append((doc.document_type, doc.file_path))
    
    documents.append(("Parent/Guardian Consent Form", consent_form_path))
    
    # Create new player record in receiving team
    new_player = Player(
        team_id=transfer.to_team_id,
        parent_id=player.parent_id,
        full_name=player.full_name,
        gender=player.gender,
        dob=player.dob,
        nationality=player.nationality,
        email=player.email,
        residential_address=player.residential_address,
        school_name=player.school_name,
        position=player.position,
        registration_type="transfer",
        registration_period=transfer.registration_period,
        agreement_form_path=consent_form_path,
        photo_path=player.photo_path,
        age_group=player.age_group,
        registration_reminder_sent_at=None,
        status=ApprovalStatus.PENDING.value,
        approved_at=None,
    )
    
    db.add(new_player)
    db.flush()
    
    # Add documents for new player
    for document_type, file_path in documents:
        db.add(
            PlayerDocument(
                player_id=new_player.player_id,
                document_type=document_type,
                file_path=file_path,
            )
        )
    
    # Create registration request for new player
    db.add(
        PlayerRegistrationRequest(
            player_id=new_player.player_id,
            team_id=transfer.to_team_id,
            requested_by_team_admin_id=team_admin_id,
            registration_type="transfer",
            agreement_form_path=consent_form_path,
            status=ApprovalStatus.PENDING.value,
        )
    )
    
    # Mark transfer as completed
    transfer.agreement_form_path = consent_form_path
    transfer.consent_form_uploaded = True
    transfer.completed_at = datetime.utcnow()
    
    db.commit()
    db.refresh(new_player)
    return new_player


def unregister_transferred_player(
    db: Session,
    *,
    transfer_id: int,
    team_admin_id: int,
) -> Player:
    """Unregister/hide player from original team after approval. For loans, blur player. For permanent, remove from roster."""
    transfer = db.get(PlayerTransferRequest, transfer_id)
    if not transfer:
        raise RegistrationError("Transfer request was not found.")
    
    if transfer.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Transfer must be approved before unregistering.")
    
    # Verify requesting team admin is from the original team
    from_team = db.get(Team, transfer.from_team_id)
    if not from_team or from_team.team_id not in set(load_team_admin_approved_team_ids(db, team_admin_id)):
        raise RegistrationError("You can only unregister players from your own team.")
    
    player = transfer.player
    _release_player_for_transfer(transfer)
    
    db.commit()
    db.refresh(player)
    return player
