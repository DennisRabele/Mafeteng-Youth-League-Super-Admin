from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import BASE_DIR, settings
from app.core.security import sign_session, unsign_session, verify_password
from app.db.session import SessionLocal, get_db
from app.models import (
    ApprovalStatus,
    Category,
    Fixture,
    FixtureStatus,
    Match,
    MatchResultSubmission,
    Notification,
    Player,
    PlayerRegistrationRequest,
    PlayerRequest,
    PlayerTransferRequest,
    SuperAdmin,
    Team,
    TeamAdmin,
    TransferStatus,
    ResultVerification,
    User,
    UserRole,
)
from app.services.league import (
    create_fixture,
    get_league_tables,
    get_notifications_for_user,
    get_player_performances,
    mark_notification_read,
    postpone_fixture,
    submit_match_result,
    update_fixture,
    verify_match_result,
)
from app.services.registration import (
    RegistrationError,
    approve_player,
    approve_renewal,
    approve_team,
    approve_team_admin,
    approve_transfer_registration,
    create_super_admin_registration,
    create_team_admin_registration,
    complete_transfer_registration,
    get_team_admins_count,
    is_first_team_admin_for_team,
    issue_email_verification_code,
    issue_login_code,
    issue_password_recovery_code,
    register_player,
    register_team,
    register_transferred_player,
    reject_player,
    reject_renewal,
    reject_team,
    reject_team_admin,
    reject_transfer_registration,
    renew_player_registration,
    request_player_from_team,
    request_player_transfer,
    respond_to_transfer,
    restore_expired_loans,
    reset_password,
    unregister_transferred_player,
    age_on,
    verify_email_code,
    verify_login_code,
    verify_password_recovery_code,
)
from app.services.email import EmailDeliveryError, send_login_code, send_verification_code
from app.services.storage import save_upload


router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
LOGIN_CHALLENGE_COOKIE = "ydl_login_challenge"
VERIFY_CHALLENGE_COOKIE = "ydl_verify_challenge"
PASSWORD_RECOVERY_COOKIE = "ydl_password_recovery"


def _render(request: Request, template: str, context: dict):
    app_mode = getattr(request.app.state, "app_mode", "combined")
    assets = context.pop("assets", None) or _load_assets()
    context.setdefault("current_user", None)
    context.setdefault("message", None)
    context.setdefault("error", None)
    context.setdefault("app_name", settings.app_name)
    context.setdefault("app_mode", app_mode)
    context.setdefault("assets", assets)
    return templates.TemplateResponse(request, template, context)


def _load_assets() -> dict[str, str]:
    return {
        "league_logo": "/static/images/logo.jpg",
    }


def _current_user(request: Request, db: Session | None = None) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    payload = unsign_session(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not isinstance(user_id, int):
        return None
    try:
        if db is not None:
            return db.get(User, user_id)
        with SessionLocal() as session:
            return session.get(User, user_id)
    except Exception:
        return None


def _safe_upload(upload: UploadFile | None, folder: str) -> str | None:
    try:
        return save_upload(upload, folder)
    except Exception as exc:
        raise RegistrationError(
            "A file upload could not be completed right now. Please try again."
        ) from exc


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


def _destination_for_user(user: User) -> str:
    if user.role == UserRole.SUPER_ADMIN.value:
        return "/super-admin"
    if user.role == UserRole.TEAM_ADMIN.value:
        return "/team-admin/welcome"
    return "/"


def _render_code_screen(
    request: Request,
    *,
    purpose: str,
    user: User,
    message: str,
    error: str | None = None,
):
    if purpose == "login":
        response = _render(
            request,
            "code_verification.html",
            {
                "title": "Login Code Verification",
                "action": "/login/code",
                "code_field": "one_time_code",
                "submit_label": "Continue",
                "message": message,
                "error": error,
            },
        )
        response.set_cookie(
            LOGIN_CHALLENGE_COOKIE,
            sign_session({"sub": user.user_id, "purpose": "login"}, settings.login_code_minutes * 60),
            httponly=True,
            samesite="lax",
        )
        return response
    
    if purpose == "password_recovery":
        response = _render(
            request,
            "code_verification.html",
            {
                "title": "Password Recovery Code Verification",
                "action": "/password-reset/code",
                "code_field": "one_time_code",
                "submit_label": "Verify Code",
                "message": message,
                "error": error,
            },
        )
        response.set_cookie(
            PASSWORD_RECOVERY_COOKIE,
            sign_session({"sub": user.user_id, "purpose": "password_recovery"}, settings.email_code_minutes * 60),
            httponly=True,
            samesite="lax",
        )
        return response

    response = _render(
        request,
        "code_verification.html",
        {
            "title": "Email Code Verification",
            "action": "/verify-email",
            "code_field": "verification_code",
            "submit_label": "Continue",
            "message": message,
            "error": error,
        },
    )
    response.set_cookie(
        VERIFY_CHALLENGE_COOKIE,
        sign_session({"sub": user.user_id, "purpose": "email_verification"}, settings.email_code_minutes * 60),
        httponly=True,
        samesite="lax",
    )
    return response


def _challenge_user(request: Request, db: Session, cookie_name: str, purpose: str) -> User | None:
    token = request.cookies.get(cookie_name)
    if not token:
        return None
    payload = unsign_session(token)
    if not payload or payload.get("purpose") != purpose:
        return None
    user_id = payload.get("sub")
    if not isinstance(user_id, int):
        return None
    try:
        return db.get(User, user_id)
    except Exception:
        return None


def _require_user(request: Request, db: Session) -> User:
    user = _current_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def _require_super_admin(request: Request, db: Session) -> User:
    user = _require_user(request, db)
    if user.role != UserRole.SUPER_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/team-admin/dashboard"},
        )
    return user


def _get_super_admin_id(user: User) -> int:
    """Extract super admin ID from user's super_admin_profile"""
    if not user.super_admin_profile:
        raise RegistrationError("Super admin profile not found.")
    return user.super_admin_profile.admin_id


def _get_super_admin_user(db: Session, super_admin_id: int | None) -> User | None:
    if not super_admin_id:
        return None
    super_admin = db.get(SuperAdmin, super_admin_id)
    return super_admin.user if super_admin else None


def _require_team_admin(request: Request, db: Session) -> TeamAdmin:
    user = _require_user(request, db)
    if user.role != UserRole.TEAM_ADMIN.value or not user.team_admin_profile:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/super-admin"},
        )
    if user.team_admin_profile.status != ApprovalStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/team-admin/account"},
        )
    return user.team_admin_profile


def _get_team_admin_profile(request: Request, db: Session) -> TeamAdmin:
    """Get team admin profile without requiring approval status. Allows dashboard access for pending admins."""
    user = _require_user(request, db)
    if user.role != UserRole.TEAM_ADMIN.value or not user.team_admin_profile:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/super-admin"},
        )
    return user.team_admin_profile


def _require_team_admin_account(request: Request, db: Session) -> TeamAdmin:
    user = _require_user(request, db)
    if user.role != UserRole.TEAM_ADMIN.value or not user.team_admin_profile:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/super-admin"},
        )
    return user.team_admin_profile


def _parse_dashboard_datetime(value: str | None) -> datetime:
    if not value:
        raise RegistrationError("A valid date and time is required.")
    cleaned = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.replace(hour=15, minute=0)
            return parsed
        except ValueError:
            continue
    raise RegistrationError("Date and time must be entered in YYYY-MM-DD HH:MM format.")


def _safe_dashboard_value(factory, default):
    try:
        return factory()
    except Exception:
        return default


def _load_fixtures(db: Session, *, team_ids: list[int] | None = None) -> list[Fixture]:
    query = (
        select(Fixture)
        .options(
            selectinload(Fixture.category),
            selectinload(Fixture.home_team),
            selectinload(Fixture.away_team),
            selectinload(Fixture.match).selectinload(Match.result_submissions),
        )
        .order_by(Fixture.fixture_date.desc(), Fixture.fixture_id.desc())
    )
    if team_ids is not None:
        query = query.where(
            or_(
                Fixture.home_team_id.in_(team_ids),
                Fixture.away_team_id.in_(team_ids),
            )
        )
    return db.scalars(query).all()


def _load_result_submissions(
    db: Session,
    *,
    team_ids: list[int] | None = None,
) -> list[MatchResultSubmission]:
    query = (
        select(MatchResultSubmission)
        .options(
            selectinload(MatchResultSubmission.match).selectinload(Match.fixture).selectinload(Fixture.home_team),
            selectinload(MatchResultSubmission.match).selectinload(Match.fixture).selectinload(Fixture.away_team),
            selectinload(MatchResultSubmission.submitted_by).selectinload(TeamAdmin.user),
            selectinload(MatchResultSubmission.verification).selectinload(ResultVerification.verified_by).selectinload(SuperAdmin.user),
        )
        .order_by(MatchResultSubmission.submitted_date.desc(), MatchResultSubmission.submission_id.desc())
    )
    if team_ids is not None:
        query = (
            query.join(Match, Match.match_id == MatchResultSubmission.match_id)
            .join(Fixture, Fixture.fixture_id == Match.fixture_id)
            .where(
                or_(
                    Fixture.home_team_id.in_(team_ids),
                    Fixture.away_team_id.in_(team_ids),
                )
            )
        )
    return db.scalars(query).all()


@router.get("/")
def home(request: Request):
    user = _current_user(request)
    app_mode = getattr(request.app.state, "app_mode", "combined")
    if user and user.role == UserRole.SUPER_ADMIN.value:
        return _redirect("/super-admin")
    if app_mode != "super_admin" and user and user.role == UserRole.TEAM_ADMIN.value:
        return _redirect("/team-admin/welcome")
    if app_mode == "super_admin":
        return _render(request, "super_admin_home.html", {"current_user": None})
    return _render(request, "home.html", {"current_user": user})


@router.get("/login")
def login_form(
    request: Request,
    error: str | None = None,
):
    return _render(
        request,
        "login.html",
        {"current_user": _current_user(request), "error": error},
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = db.scalar(
            select(User)
            .options(selectinload(User.team_admin_profile), selectinload(User.super_admin_profile))
            .where(User.email == email.strip().lower())
        )
    except Exception:
        db.rollback()
        return _render(
            request,
            "login.html",
            {"error": "Login could not be completed right now. Please try again."},
        )
    if not user or not verify_password(password, user.password_hash):
        return _render(request, "login.html", {"error": "Invalid email or password."})

    app_mode = getattr(request.app.state, "app_mode", "combined")
    if app_mode == "super_admin" and user.role != UserRole.SUPER_ADMIN.value:
        return _render(
            request,
            "login.html",
            {"error": "Invalid email or password."},
        )
    if app_mode == "team_admin" and user.role != UserRole.TEAM_ADMIN.value:
        return _render(
            request,
            "login.html",
            {"error": "Invalid email or password."},
        )

    if not user.email_verified:
        try:
            verification_code = issue_email_verification_code(db, user)
            send_verification_code(to_email=user.email, code=verification_code)
        except EmailDeliveryError as exc:
            return _render(request, "login.html", {"error": "Verification code was not sent. Please try again."})
        except Exception:
            db.rollback()
            return _render(
                request,
                "login.html",
                {"error": "Verification could not be completed right now. Please try again."},
            )
        return _render_code_screen(
            request,
            purpose="email_verification",
            user=user,
            message="A verification code was sent to your email address.",
        )

    if user.role == UserRole.TEAM_ADMIN.value:
        team_admin = user.team_admin_profile
        if not team_admin:
            return _render(
                request,
                "login.html",
                {"error": "Your Team Admin registration is still pending approval."},
            )

    try:
        login_code = issue_login_code(db, user)
        send_login_code(to_email=user.email, code=login_code)
    except EmailDeliveryError:
        return _render(request, "login.html", {"error": "Login code was not sent. Please try again."})
    except Exception:
        db.rollback()
        return _render(
            request,
            "login.html",
            {"error": "Login could not be completed right now. Please try again."},
        )
    return _render_code_screen(
        request,
        purpose="login",
        user=user,
        message="A one-time login code was sent to your email address.",
    )

@router.post("/login/code")
def verify_login_code_route(
    request: Request,
    one_time_code: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _challenge_user(request, db, LOGIN_CHALLENGE_COOKIE, "login")
    if not user:
        return _render(request, "login.html", {"error": "Login code expired. Please log in again."})

    try:
        verify_login_code(db, user, one_time_code)
    except RegistrationError as exc:
        return _render_code_screen(
            request,
            purpose="login",
            user=user,
            message="Enter the one-time code sent to your email address.",
            error=str(exc),
        )

    destination = _destination_for_user(user)
    response = _redirect(destination)
    response.set_cookie(
        settings.session_cookie_name,
        sign_session({"sub": user.user_id, "role": user.role}),
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(LOGIN_CHALLENGE_COOKIE)
    return response


@router.post("/logout")
def logout():
    response = _redirect("/")
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.get("/forgot-password")
def forgot_password_form(request: Request):
    return _render(
        request,
        "forgot_password.html",
        {"current_user": _current_user(request)},
    )


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
    except Exception:
        db.rollback()
        return _render(
            request,
            "forgot_password.html",
            {"error": "Password recovery could not be completed right now. Please try again."},
        )
    if not user:
        # Don't reveal if email exists
        return _render(
            request,
            "forgot_password.html",
            {"message": "If an account exists with this email, a recovery code will be sent."},
        )

    try:
        recovery_code = issue_password_recovery_code(db, user)
        send_verification_code(to_email=user.email, code=recovery_code)
    except RegistrationError as exc:
        return _render(
            request,
            "forgot_password.html",
            {"error": str(exc)},
        )
    except EmailDeliveryError:
        return _render(
            request,
            "forgot_password.html",
            {"error": "Recovery code was not sent. Please try again."},
        )
    except Exception:
        db.rollback()
        return _render(
            request,
            "forgot_password.html",
            {"error": "Password recovery could not be completed right now. Please try again."},
        )

    return _render_code_screen(
        request,
        purpose="password_recovery",
        user=user,
        message="A password recovery code was sent to your email address.",
    )


@router.get("/password-reset")
def password_reset_form(
    request: Request,
    db: Session = Depends(get_db),
):
    user = _challenge_user(request, db, PASSWORD_RECOVERY_COOKIE, "password_recovery")
    if not user:
        return _redirect("/forgot-password")
    return _render(
        request,
        "password_reset.html",
        {},
    )


@router.post("/password-reset/code")
def verify_recovery_code_route(
    request: Request,
    one_time_code: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _challenge_user(request, db, PASSWORD_RECOVERY_COOKIE, "password_recovery")
    if not user:
        return _render(request, "forgot_password.html", {"error": "Recovery code expired. Please try again."})

    try:
        verify_password_recovery_code(db, user, one_time_code)
    except RegistrationError as exc:
        return _render_code_screen(
            request,
            purpose="password_recovery",
            user=user,
            message="Enter the password recovery code sent to your email address.",
            error=str(exc),
        )

    response = _render(request, "password_reset.html", {})
    response.set_cookie(
        PASSWORD_RECOVERY_COOKIE,
        sign_session({"sub": user.user_id, "purpose": "password_reset_confirmed"}, 60 * 30),
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/password-reset")
def reset_password_route(
    request: Request,
    one_time_code: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    token = request.cookies.get(PASSWORD_RECOVERY_COOKIE)
    payload = unsign_session(token) if token else None
    if not payload or payload.get("purpose") != "password_reset_confirmed":
        return _redirect("/forgot-password")

    user_id = payload.get("sub")
    if not isinstance(user_id, int):
        return _redirect("/forgot-password")

    user = db.get(User, user_id)
    if not user:
        return _redirect("/forgot-password")

    if new_password != confirm_password:
        return _render(
            request,
            "password_reset.html",
            {"error": "Passwords do not match."},
        )

    try:
        reset_password(db, user, new_password, one_time_code)
    except RegistrationError as exc:
        return _render(
            request,
            "password_reset.html",
            {"error": str(exc)},
        )

    response = _redirect("/login")
    response.delete_cookie(PASSWORD_RECOVERY_COOKIE)
    return response


@router.get("/register/team-admin")
def team_admin_registration_form(
    request: Request,
    is_first: str | None = None,
):
    return _render(
        request,
        "team_admin_register.html",
        {
            "current_user": _current_user(request),
            "is_first": is_first == "true" if is_first else None,
            "form_data": {},
        },
    )


@router.post("/register/team-admin")
def team_admin_registration(
    request: Request,
    full_name: str = Form(...),
    team_name: str = Form(...),
    national_id: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    photo: UploadFile | None = File(None),
    is_first_admin: str = Form(...),
    team_code: str | None = Form(None),
    team_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    import re
    
    # Validate full_name - only letters and spaces
    if not re.match(r"^[A-Za-z\s'\-]+$", full_name.strip()):
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "Full name can only contain letters and spaces.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )
    
    # Validate national_id - only numbers
    if not re.match(r"^[A-Za-z0-9+\-\/\s]+$", national_id.strip()):
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "National ID can only contain numbers.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )
    
    # Validate phone - only numbers and symbols (+, -, space)
    if not re.match(r"^[0-9+\-\s]+$", phone.strip()):
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "Phone number can only contain numbers, +, -, or spaces.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )
    
    if password != confirm_password:
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "Passwords do not match.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )

    # If not first admin, a team code or team ID is required
    parsed_team_id = None
    if is_first_admin == "false":
        if not team_code and (not team_id or not team_id.strip()):
            return _render(
                request,
                "team_admin_register.html",
                {
                    "error": "Team code is required for additional team admin registrations.",
                    "is_first": False,
                    "form_data": {
                        "full_name": full_name,
                        "team_name": team_name,
                        "national_id": national_id,
                        "phone": phone,
                        "email": email,
                    },
                },
            )
        if team_id and team_id.strip():
            try:
                parsed_team_id = int(team_id.strip())
            except (ValueError, TypeError):
                return _render(
                    request,
                    "team_admin_register.html",
                    {
                        "error": "Invalid Team ID format.",
                        "is_first": False,
                        "form_data": {
                            "full_name": full_name,
                            "team_name": team_name,
                            "national_id": national_id,
                            "phone": phone,
                            "email": email,
                            "team_code": team_code,
                        },
                    },
                )

    try:
        photo_path = _safe_upload(photo, "admin-photos")
        team_admin = create_team_admin_registration(
            db,
            full_name=full_name,
            team_name=team_name,
            national_id=national_id,
            phone=phone,
            email=email,
            password=password,
            photo_path=photo_path,
            team_id=parsed_team_id,
            team_code=team_code,
            commit=False,
        )
        verification_code = issue_email_verification_code(db, team_admin.user, commit=False)
        send_verification_code(to_email=team_admin.user.email, code=verification_code)
        db.commit()
        db.refresh(team_admin)
    except RegistrationError as exc:
        db.rollback()
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": str(exc),
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )
    except EmailDeliveryError:
        db.rollback()
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "Verification code was not sent. Please try again.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )
    except Exception:
        db.rollback()
        return _render(
            request,
            "team_admin_register.html",
            {
                "error": "Registration could not be completed right now. Please try again.",
                "is_first": is_first_admin == "true",
                "form_data": {
                    "full_name": full_name,
                    "team_name": team_name,
                    "national_id": national_id,
                    "phone": phone,
                    "email": email,
                    "team_code": team_code,
                },
            },
        )

    return _render_code_screen(
        request,
        purpose="email_verification",
        user=team_admin.user,
        message="Registration submitted. A verification code was sent to your email address.",
    )


@router.get("/register/super-admin")
def super_admin_registration_form(request: Request, db: Session = Depends(get_db)):
    try:
        super_admin_count = db.scalar(select(func.count()).select_from(SuperAdmin)) or 0
    except Exception:
        super_admin_count = 0
    return _render(
        request,
        "super_admin_register.html",
        {
            "current_user": _current_user(request, db),
            "super_admin_count": super_admin_count,
            "super_admin_limit": 5,
            "form_data": {},
        },
    )


@router.post("/register/super-admin")
def super_admin_registration(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    photo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    try:
        super_admin_count = db.scalar(select(func.count()).select_from(SuperAdmin)) or 0
    except Exception:
        db.rollback()
        return _render(
            request,
            "super_admin_register.html",
            {
                "error": "Registration could not be completed right now. Please try again.",
                "super_admin_count": 0,
                "super_admin_limit": 5,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                },
            },
        )
    if password != confirm_password:
        return _render(
            request,
            "super_admin_register.html",
            {
                "error": "Passwords do not match.",
                "super_admin_count": super_admin_count,
                "super_admin_limit": 5,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                },
            },
        )

    try:
        photo_path = _safe_upload(photo, "admin-photos")
        super_admin = create_super_admin_registration(
            db,
            full_name=full_name,
            email=email,
            password=password,
            photo_path=photo_path,
            commit=False,
        )
        verification_code = issue_email_verification_code(db, super_admin.user, commit=False)
        send_verification_code(to_email=super_admin.user.email, code=verification_code)
        db.commit()
        db.refresh(super_admin)
    except RegistrationError as exc:
        db.rollback()
        return _render(
            request,
            "super_admin_register.html",
            {
                "error": str(exc),
                "super_admin_count": super_admin_count,
                "super_admin_limit": 5,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                },
            },
        )
    except EmailDeliveryError:
        db.rollback()
        return _render(
            request,
            "super_admin_register.html",
            {
                "error": "Verification code was not sent. Please try again.",
                "super_admin_count": super_admin_count,
                "super_admin_limit": 5,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                },
            },
        )
    except Exception:
        db.rollback()
        return _render(
            request,
            "super_admin_register.html",
            {
                "error": "Registration could not be completed right now. Please try again.",
                "super_admin_count": super_admin_count,
                "super_admin_limit": 5,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                },
            },
        )

    return _render_code_screen(
        request,
        purpose="email_verification",
        user=super_admin.user,
        message="Super Admin registered. A verification code was sent to your email address.",
    )


@router.get("/verify-email")
def verify_email_form(request: Request):
    return _render(
        request,
        "login.html",
        {"message": "Start by logging in or registering. A code will be sent automatically."},
    )


@router.post("/verify-email")
def verify_email_route(
    request: Request,
    verification_code: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _challenge_user(request, db, VERIFY_CHALLENGE_COOKIE, "email_verification")
    if not user:
        return _render(
            request,
            "login.html",
            {"error": "Verification code expired. Please log in or register again."},
        )
    try:
        verify_email_code(db, user, verification_code)
    except RegistrationError as exc:
        return _render_code_screen(
            request,
            purpose="email_verification",
            user=user,
            message="Enter the verification code sent to your email address.",
            error=str(exc),
        )
    response = _redirect(_destination_for_user(user))
    response.set_cookie(
        settings.session_cookie_name,
        sign_session({"sub": user.user_id, "role": user.role}),
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(VERIFY_CHALLENGE_COOKIE)
    return response


@router.post("/verify-email/resend")
def resend_verification_email_route(
    request: Request,
    db: Session = Depends(get_db),
):
    user = _challenge_user(request, db, VERIFY_CHALLENGE_COOKIE, "email_verification")
    if not user:
        return _render(request, "login.html", {"error": "Verification code expired. Please log in or register again."})
    if user.email_verified:
        response = _redirect(_destination_for_user(user))
        response.delete_cookie(VERIFY_CHALLENGE_COOKIE)
        return response
    try:
        verification_code = issue_email_verification_code(db, user)
        send_verification_code(to_email=user.email, code=verification_code)
    except EmailDeliveryError:
        return _render_code_screen(
            request,
            purpose="email_verification",
            user=user,
            message="Enter the verification code sent to your email address.",
            error="Verification code was not sent. Please try again.",
        )
    except Exception:
        db.rollback()
        return _render_code_screen(
            request,
            purpose="email_verification",
            user=user,
            message="Enter the verification code sent to your email address.",
            error="Verification code could not be sent right now. Please try again.",
        )
    return _render_code_screen(
        request,
        purpose="email_verification",
        user=user,
        message="A new verification code was sent to your email address.",
    )


@router.get("/super-admin")
def super_admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = _require_super_admin(request, db)

    all_team_admins = db.scalars(
        select(TeamAdmin)
        .options(selectinload(TeamAdmin.user))
        .order_by(TeamAdmin.team_admin_id.desc())
    ).all()
    
    # Load approver user info for team admins
    for ta in all_team_admins:
        if ta.approved_by_super_admin_id:
            approver = _get_super_admin_user(db, ta.approved_by_super_admin_id)
            ta.approver_user = approver
    
    all_teams = db.scalars(
        select(Team)
        .options(
            selectinload(Team.team_admin).selectinload(TeamAdmin.user),
            selectinload(Team.category),
        )
        .order_by(Team.team_id.desc())
    ).all()
    
    # Load approver user info for teams
    for t in all_teams:
        if t.approved_by_super_admin_id:
            approver = _get_super_admin_user(db, t.approved_by_super_admin_id)
            t.approver_user = approver
    
    all_players = db.scalars(
        select(Player)
        .options(
            selectinload(Player.team),
            selectinload(Player.parent),
            selectinload(Player.documents),
        )
        .where(Player.status != "transferred")
        .order_by(Player.player_id.desc())
    ).all()
    
    # Load approver user info for players
    for p in all_players:
        p.calculated_age = age_on(p.dob)
        if p.approved_by_super_admin_id:
            approver = _get_super_admin_user(db, p.approved_by_super_admin_id)
            p.approver_user = approver
    
    # Fetch renewal registrations
    all_renewals = db.scalars(
        select(PlayerRegistrationRequest)
        .where(PlayerRegistrationRequest.registration_type == "renewal")
        .options(
            selectinload(PlayerRegistrationRequest.player).selectinload(Player.team),
            selectinload(PlayerRegistrationRequest.player).selectinload(Player.documents),
            selectinload(PlayerRegistrationRequest.requested_by).selectinload(TeamAdmin.user),
        )
        .order_by(PlayerRegistrationRequest.registration_id.desc())
    ).all()
    
    # Load approver user info for renewals
    for r in all_renewals:
        if r.player:
            r.player.calculated_age = age_on(r.player.dob)
        if r.approved_by_super_admin_id:
            approver = _get_super_admin_user(db, r.approved_by_super_admin_id)
            r.approver_user = approver
    
    # Fetch completed transfer registrations for Super Admin review.
    all_transfers = db.scalars(
        select(PlayerRegistrationRequest)
        .where(PlayerRegistrationRequest.registration_type == "transfer")
        .options(
            selectinload(PlayerRegistrationRequest.player).selectinload(Player.documents),
            selectinload(PlayerRegistrationRequest.player).selectinload(Player.parent),
            selectinload(PlayerRegistrationRequest.team),
            selectinload(PlayerRegistrationRequest.requested_by).selectinload(TeamAdmin.user),
        )
        .order_by(PlayerRegistrationRequest.registration_id.desc())
    ).all()
    
    # Load approver user info for transfers
    for t in all_transfers:
        if t.player:
            t.player.calculated_age = age_on(t.player.dob)
        if t.approved_by_super_admin_id:
            approver = _get_super_admin_user(db, t.approved_by_super_admin_id)
            t.approver_user = approver
        t.transfer_request = db.scalar(
            select(PlayerTransferRequest)
            .where(PlayerTransferRequest.agreement_form_path == t.agreement_form_path)
            .options(
                selectinload(PlayerTransferRequest.player),
                selectinload(PlayerTransferRequest.from_team),
                selectinload(PlayerTransferRequest.to_team),
            )
            .order_by(PlayerTransferRequest.transfer_id.desc())
        )
    
    teams_list = db.scalars(
        select(Team).options(selectinload(Team.category)).order_by(Team.team_name)
    ).all()
    categories = db.scalars(select(Category).order_by(Category.category_name)).all()
    fixtures = _safe_dashboard_value(lambda: _load_fixtures(db), [])
    result_submissions = _safe_dashboard_value(lambda: _load_result_submissions(db), [])
    league_tables = _safe_dashboard_value(lambda: get_league_tables(db), {})
    player_performances = _safe_dashboard_value(
        lambda: get_player_performances(db),
        {"scorers": [], "assisters": []},
    )
    notifications = _safe_dashboard_value(
        lambda: get_notifications_for_user(db, user.user_id, limit=12),
        [],
    )
    unread_notifications = sum(1 for notification in notifications if not notification.is_read)

    pending_count = (
        sum(1 for ta in all_team_admins if ta.status == ApprovalStatus.PENDING.value)
        + sum(1 for t in all_teams if t.status == ApprovalStatus.PENDING.value)
        + sum(1 for p in all_players if p.status == ApprovalStatus.PENDING.value)
        + sum(1 for r in all_renewals if r.status == ApprovalStatus.PENDING.value)
        + sum(1 for t in all_transfers if t.status == ApprovalStatus.PENDING.value)
    )

    counts = {
        "super_admins": db.scalar(select(func.count()).select_from(SuperAdmin)) or 0,
        "team_admins": len(all_team_admins),
        "teams": len(all_teams),
        "players": len(all_players),
        "renewals": len(all_renewals),
        "transfers": len(all_transfers),
        "fixtures": len(fixtures),
        "results": len(result_submissions),
        "notifications": unread_notifications,
        "pending": pending_count,
    }

    return _render(
        request,
        "super_admin/dashboard.html",
        {
            "current_user": user,
            "counts": counts,
            "all_team_admins": all_team_admins,
            "all_teams": all_teams,
            "all_players": all_players,
            "all_renewals": all_renewals,
            "all_transfers": all_transfers,
            "teams_list": teams_list,
            "categories": categories,
            "fixtures": fixtures,
            "result_submissions": result_submissions,
            "league_tables": league_tables,
            "player_performances": player_performances,
            "notifications": notifications,
            "unread_notifications": unread_notifications,
        },
    )


@router.post("/super-admin/team-admins/{team_admin_id}/approve")
def approve_team_admin_route(
    team_admin_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        team_admin = approve_team_admin(db, team_admin_id, super_admin_id)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})

    return _render(
        request,
        "super_admin/action_result.html",
        {
            "message": f"Team Admin approved. {team_admin.user.full_name} can now log into the Team Admin app with the password they created.",
            "generated_code": team_admin.admin_code,
            "credential_email": team_admin.user.email,
        },
    )


@router.post("/super-admin/team-admins/{team_admin_id}/reject")
def reject_team_admin_route(
    team_admin_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        reject_team_admin(db, team_admin_id, rejection_reason)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/teams/{team_id}/approve")
def approve_team_route(team_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        approve_team(db, team_id, super_admin_id)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/teams/{team_id}/reject")
def reject_team_route(
    team_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        reject_team(db, team_id, rejection_reason)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/players/{player_id}/approve")
def approve_player_route(player_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        approve_player(db, player_id, super_admin_id)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/players/{player_id}/reject")
def reject_player_route(
    player_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        reject_player(db, player_id, rejection_reason)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/renewals/{registration_id}/approve")
def approve_renewal_route(registration_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        approve_renewal(db, registration_id, super_admin_id)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/renewals/{registration_id}/reject")
def reject_renewal_route(
    registration_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        reject_renewal(db, registration_id, rejection_reason)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/transfers/{registration_id}/approve")
def approve_transfer_route(registration_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        approve_transfer_registration(db, registration_id, super_admin_id)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.post("/super-admin/transfers/{registration_id}/reject")
def reject_transfer_route(
    registration_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        reject_transfer_registration(db, registration_id, rejection_reason)
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin")


@router.get("/team-admin/welcome")
def team_admin_welcome(request: Request, db: Session = Depends(get_db)):
    team_admin = _require_team_admin_account(request, db)
    return _render(
        request,
        "team_admin/welcome.html",
        {
            "current_user": team_admin.user,
            "team_admin": team_admin,
        },
    )


@router.get("/team-admin/account")
def team_admin_account(request: Request, db: Session = Depends(get_db)):
    team_admin = _require_team_admin_account(request, db)
    return _render(
        request,
        "team_admin/account.html",
        {
            "current_user": team_admin.user,
            "team_admin": team_admin,
        },
    )


@router.get("/team-admin/dashboard")
def team_admin_dashboard(request: Request, db: Session = Depends(get_db)):
    team_admin = _get_team_admin_profile(request, db)
    restore_expired_loans(db)
    categories = db.scalars(select(Category).order_by(Category.category_name)).all()
    teams = db.scalars(
        select(Team)
        .options(selectinload(Team.category))
        .where(Team.team_admin_id == team_admin.team_admin_id)
        .order_by(Team.team_id.desc())
    ).all()
    approved_teams = [
        team for team in teams if team.status == ApprovalStatus.APPROVED.value
    ]
    own_team_ids = [team.team_id for team in teams]
    players = db.scalars(
        select(Player)
        .options(selectinload(Player.team))
        .join(Team, Player.team_id == Team.team_id)
        .where(Team.team_admin_id == team_admin.team_admin_id)
        .where(Player.status != "transferred")
        .order_by(Player.player_id.desc())
    ).all()
    approved_players = [
        player for player in players if player.status == ApprovalStatus.APPROVED.value
    ]
    renewal_requests = db.scalars(
        select(PlayerRegistrationRequest)
        .where(
            PlayerRegistrationRequest.registration_type == "renewal",
            PlayerRegistrationRequest.team_id.in_(own_team_ids),
        )
        .options(
            selectinload(PlayerRegistrationRequest.player).selectinload(Player.team),
            selectinload(PlayerRegistrationRequest.team),
        )
        .order_by(PlayerRegistrationRequest.registration_id.desc())
    ).all()
    transfer_target_teams = db.scalars(
        select(Team)
        .options(selectinload(Team.category))
        .where(
            Team.team_admin_id != team_admin.team_admin_id,
            Team.status == ApprovalStatus.APPROVED.value,
        )
        .order_by(Team.team_name)
    ).all()
    all_teams = db.scalars(
        select(Team)
        .options(selectinload(Team.category))
        .where(Team.status == ApprovalStatus.APPROVED.value)
        .order_by(Team.team_name)
    ).all()
    outgoing_transfers = db.scalars(
        select(PlayerTransferRequest)
        .options(
            selectinload(PlayerTransferRequest.player),
            selectinload(PlayerTransferRequest.from_team),
            selectinload(PlayerTransferRequest.to_team),
        )
        .where(PlayerTransferRequest.requested_by_team_admin_id == team_admin.team_admin_id)
        .order_by(PlayerTransferRequest.transfer_id.desc())
    ).all()
    incoming_transfers = db.scalars(
        select(PlayerTransferRequest)
        .options(
            selectinload(PlayerTransferRequest.player).selectinload(Player.documents),
            selectinload(PlayerTransferRequest.from_team),
            selectinload(PlayerTransferRequest.to_team),
            selectinload(PlayerTransferRequest.requested_by).selectinload(TeamAdmin.user),
        )
        .where(
            PlayerTransferRequest.requested_by_team_admin_id != team_admin.team_admin_id,
            or_(
                PlayerTransferRequest.from_team_id.in_(own_team_ids),
                PlayerTransferRequest.to_team_id.in_(own_team_ids),
            ),
        )
        .order_by(PlayerTransferRequest.transfer_id.desc())
    ).all()
    
    # Get approved transfers pending registration (for receiving team)
    approved_transfers_for_registration = db.scalars(
        select(PlayerTransferRequest)
        .options(
            selectinload(PlayerTransferRequest.player),
            selectinload(PlayerTransferRequest.from_team),
            selectinload(PlayerTransferRequest.to_team),
        )
        .where(
            PlayerTransferRequest.to_team_id.in_(own_team_ids),
            PlayerTransferRequest.status == ApprovalStatus.APPROVED.value,
            PlayerTransferRequest.completed_at.is_(None),
        )
        .order_by(PlayerTransferRequest.transfer_id.desc())
    ).all()
    
    # Get approved transfers pending unregistration (for original team)
    approved_transfers_for_unregistration = db.scalars(
        select(PlayerTransferRequest)
        .options(
            selectinload(PlayerTransferRequest.player),
            selectinload(PlayerTransferRequest.from_team),
            selectinload(PlayerTransferRequest.to_team),
        )
        .where(
            PlayerTransferRequest.from_team_id.in_(own_team_ids),
            PlayerTransferRequest.status == ApprovalStatus.APPROVED.value,
            PlayerTransferRequest.completed_at.is_(None),
        )
        .order_by(PlayerTransferRequest.transfer_id.desc())
    ).all()
    
    # Get all registered players from all teams for "Request Player" form
    all_registered_players = db.scalars(
        select(Player)
        .options(selectinload(Player.team))
        .where(Player.status == ApprovalStatus.APPROVED.value)
        .order_by(Player.full_name)
    ).all()
    fixtures = _safe_dashboard_value(lambda: _load_fixtures(db, team_ids=own_team_ids), [])
    result_submissions = _safe_dashboard_value(
        lambda: _load_result_submissions(db, team_ids=own_team_ids),
        [],
    )
    league_tables = _safe_dashboard_value(
        lambda: get_league_tables(db, team_ids=own_team_ids),
        {},
    )
    player_performances = _safe_dashboard_value(
        lambda: get_player_performances(db, team_ids=own_team_ids),
        {"scorers": [], "assisters": []},
    )
    notifications = _safe_dashboard_value(
        lambda: get_notifications_for_user(db, team_admin.user_id, limit=12),
        [],
    )
    unread_notifications = sum(1 for notification in notifications if not notification.is_read)

    return _render(
        request,
        "team_admin/dashboard.html",
        {
            "current_user": team_admin.user,
            "team_admin": team_admin,
            "categories": categories,
            "teams": teams,
            "approved_teams": approved_teams,
            "players": players,
            "approved_players": approved_players,
            "renewal_requests": renewal_requests,
            "transfer_target_teams": transfer_target_teams,
            "all_teams": all_teams,
            "all_registered_players": all_registered_players,
            "outgoing_transfers": outgoing_transfers,
            "incoming_transfers": incoming_transfers,
            "approved_transfers_for_registration": approved_transfers_for_registration,
            "approved_transfers_for_unregistration": approved_transfers_for_unregistration,
            "transfer_status": TransferStatus,
            "fixtures": fixtures,
            "result_submissions": result_submissions,
            "league_tables": league_tables,
            "player_performances": player_performances,
            "notifications": notifications,
            "unread_notifications": unread_notifications,
        },
    )


@router.post("/super-admin/fixtures")
def create_fixture_route(
    request: Request,
    category_id: int = Form(...),
    home_team_id: int = Form(...),
    away_team_id: int = Form(...),
    fixture_date: str = Form(...),
    venue: str = Form(...),
    status_value: str = Form(FixtureStatus.PUBLISHED.value),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        create_fixture(
            db,
            category_id=category_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            fixture_date=_parse_dashboard_datetime(fixture_date),
            venue=venue,
            status=status_value,
        )
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin#fixtures")


@router.post("/super-admin/fixtures/{fixture_id}")
def update_fixture_route(
    fixture_id: int,
    request: Request,
    fixture_date: str = Form(...),
    venue: str = Form(...),
    status_value: str | None = Form(None),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        update_fixture(
            db,
            fixture_id=fixture_id,
            fixture_date=_parse_dashboard_datetime(fixture_date),
            venue=venue,
            status=status_value,
        )
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin#fixtures")


@router.post("/super-admin/fixtures/{fixture_id}/postpone")
def postpone_fixture_route(
    fixture_id: int,
    request: Request,
    new_date: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    try:
        postpone_fixture(db, fixture_id, _parse_dashboard_datetime(new_date))
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin#fixtures")


@router.post("/team-admin/results")
def submit_result_route(
    request: Request,
    fixture_id: int = Form(...),
    home_score: int = Form(...),
    away_score: int = Form(...),
    scorer_names_text: str | None = Form(None),
    goal_types_text: str | None = Form(None),
    assist_names_text: str | None = Form(None),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    try:
        submit_match_result(
            db,
            team_admin_id=team_admin.team_admin_id,
            fixture_id=fixture_id,
            home_score=home_score,
            away_score=away_score,
            scorer_names_text=scorer_names_text,
            goal_types_text=goal_types_text,
            assist_names_text=assist_names_text,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    return _redirect("/team-admin/dashboard#results")


@router.post("/super-admin/results/{submission_id}/verify")
def verify_result_route(
    submission_id: int,
    request: Request,
    home_score: int = Form(...),
    away_score: int = Form(...),
    scorer_names_text: str | None = Form(None),
    goal_types_text: str | None = Form(None),
    assist_names_text: str | None = Form(None),
    decision: str = Form(ApprovalStatus.APPROVED.value),
    db: Session = Depends(get_db),
):
    user = _require_super_admin(request, db)
    try:
        super_admin_id = _get_super_admin_id(user)
        verify_match_result(
            db,
            submission_id=submission_id,
            super_admin_id=super_admin_id,
            home_score=home_score,
            away_score=away_score,
            scorer_names_text=scorer_names_text,
            goal_types_text=goal_types_text,
            assist_names_text=assist_names_text,
            decision=decision,
        )
    except RegistrationError as exc:
        return _render(request, "super_admin/action_result.html", {"error": str(exc)})
    return _redirect("/super-admin#results")


@router.post("/notifications/{notification_id}/read")
def mark_notification_read_route(
    notification_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    try:
        mark_notification_read(db, notification_id, user.user_id)
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    destination = _destination_for_user(user)
    return _redirect(f"{destination}#notifications")


@router.post("/team-admin/teams")
def create_team_route(
    request: Request,
    team_name: str = Form(...),
    category_id: int = Form(...),
    contact_information: str = Form(...),
    team_address: str = Form(...),
    training_ground: str = Form(...),
    home_ground: str = Form(...),
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    try:
        logo_path = _safe_upload(logo, "team-logos")
        register_team(
            db,
            team_admin_id=team_admin.team_admin_id,
            team_name=team_name,
            category_id=category_id,
            contact_information=contact_information,
            team_address=team_address,
            training_ground=training_ground,
            home_ground=home_ground,
            logo=logo_path,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    except Exception:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Team registration could not be completed right now. Please try again."},
        )
    return _redirect("/team-admin/dashboard")


@router.post("/team-admin/players")
def create_player_route(
    request: Request,
    team_id: int = Form(...),
    full_name: str = Form(...),
    gender: str = Form(...),
    dob: str = Form(...),
    nationality: str = Form(...),
    player_email: str | None = Form(None),
    player_address: str | None = Form(None),
    parent_name: str = Form(...),
    parent_contact: str = Form(...),
    school_name: str | None = Form(None),
    position: str | None = Form(None),
    registration_period: int = Form(1),
    passport_photo: UploadFile | None = File(None),
    player_agreement_form: UploadFile | None = File(None),
    identity_document_type: str = Form("Birth Certificate"),
    identity_document: UploadFile | None = File(None),
    passport_document: UploadFile | None = File(None),
    birth_certificate: UploadFile | None = File(None),
    national_id_document: UploadFile | None = File(None),
    parent_consent_picture: UploadFile | None = File(None),
    medical_certificate: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    import re
    
    team_admin = _require_team_admin(request, db)
    team = db.get(Team, team_id)
    if not team or team.team_admin_id != team_admin.team_admin_id:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "You can only register players for your own approved teams."},
        )
    
    # Validate full_name - only letters and spaces
    if not re.match(r"^[A-Za-z\s'\-]+$", full_name.strip()):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Player full name can only contain letters and spaces."},
        )
    
    # Validate parent_name - only letters and spaces
    if not re.match(r"^[A-Za-z\s'\-]+$", parent_name.strip()):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Parent/Guardian name can only contain letters and spaces."},
        )
    
    # Validate nationality - only letters and spaces
    if not re.match(r"^[A-Za-z\s'\-]+$", nationality.strip()):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Nationality can only contain letters and spaces."},
        )
    
    # Validate parent_contact - only numbers and symbols (+, -, space)
    if not re.match(r"^[0-9+\-\s]+$", parent_contact.strip()):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Parent contact can only contain numbers, +, -, or spaces."},
        )
    try:
        dob_value = datetime.strptime(dob.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Date of birth must be entered in YYYY-MM-DD format."},
        )
    try:
        photo_path = _safe_upload(passport_photo, "player-photos")
        # Parent/Guardian Consent Form is now the main agreement form
        agreement_form_path = _safe_upload(parent_consent_picture, "player-agreements")
        if not agreement_form_path:
            # Fallback to player_agreement_form if parent_consent_picture not provided
            agreement_form_path = _safe_upload(player_agreement_form, "player-agreements")
        if not agreement_form_path:
            return _render(
                request,
                "team_admin/action_result.html",
                {"error": "PARENT/GUARDIAN CONSENT FORM NOT UPLOADED."},
            )
        documents: list[tuple[str, str]] = []

        # Add Parent/Guardian Consent Form to documents list
        if agreement_form_path:
            documents.append(("Parent/Guardian Consent Form", agreement_form_path))

        identity_file_path = _safe_upload(identity_document, "player-documents")
        if identity_file_path:
            documents.append((identity_document_type.strip() or "Identity Document", identity_file_path))

        for document_type, upload in [
            ("Passport", passport_document),
            ("Birth Certificate", birth_certificate),
            ("National ID", national_id_document),
            ("Medical Certificate", medical_certificate),
        ]:
            file_path = _safe_upload(upload, "player-documents")
            if file_path:
                documents.append((document_type, file_path))

        register_player(
            db,
            team_id=team_id,
            full_name=full_name,
            gender=gender,
            dob=dob_value,
            nationality=nationality,
            email=player_email,
            residential_address=player_address,
            parent_name=parent_name,
            parent_contact=parent_contact,
            school_name=school_name,
            position=position,
            registration_period=registration_period,
            agreement_form_path=agreement_form_path,
            photo_path=photo_path,
            documents=documents,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    except Exception:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Player registration could not be completed right now. Please try again."},
        )

    return _redirect("/team-admin/dashboard")


@router.post("/team-admin/players/renewals")
def renew_player_route(
    request: Request,
    player_id: int = Form(...),
    parent_consent_picture: UploadFile | None = File(None),
    player_agreement_form: UploadFile | None = File(None),
    registration_period: str = Form("1"),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    
    # Validate registration_period
    try:
        period = int(registration_period)
        if period not in (1, 2, 3):
            return _render(
                request,
                "team_admin/action_result.html",
                {"error": "Registration period must be 1, 2, or 3 years."},
            )
    except (ValueError, TypeError):
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Invalid registration period."},
        )
    try:
        # Try parent_consent_picture first, then fallback to player_agreement_form
        agreement_form_path = _safe_upload(parent_consent_picture, "player-agreements")
        if not agreement_form_path:
            agreement_form_path = _safe_upload(player_agreement_form, "player-agreements")
        renewal_request = renew_player_registration(
            db,
            team_admin_id=team_admin.team_admin_id,
            player_id=player_id,
            agreement_form_path=agreement_form_path or "",
            registration_period=period,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    except Exception:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Renewal registration could not be completed right now. Please try again."},
        )
    player_name = renewal_request.player.full_name if renewal_request.player else f"player #{renewal_request.player_id}"
    return _render(
        request,
        "team_admin/action_result.html",
        {
            "message": (
                f"Renewal registration for {player_name} was submitted successfully and is now pending approval."
            )
        },
    )


@router.post("/team-admin/transfers")
def request_transfer_route(
    request: Request,
    player_id: int = Form(...),
    to_team_id: int = Form(...),
    transfer_type: str = Form(...),
    player_details: str = Form(...),
    transfer_conditions: str = Form(...),
    registration_period: int = Form(1),
    loan_period: str | None = Form(None),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    try:
        request_player_transfer(
            db,
            team_admin_id=team_admin.team_admin_id,
            player_id=player_id,
            to_team_id=to_team_id,
            transfer_type=transfer_type,
            player_details=player_details,
            transfer_conditions=transfer_conditions,
            registration_period=registration_period,
            loan_period=loan_period,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    return _redirect("/team-admin/dashboard")


@router.post("/team-admin/transfers/{transfer_id}/respond")
def respond_transfer_route(
    transfer_id: int,
    request: Request,
    decision: str = Form(...),
    rejection_reason: str | None = Form(None),
    explanation: str | None = Form(None),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    try:
        respond_to_transfer(
            db,
            team_admin_id=team_admin.team_admin_id,
            transfer_id=transfer_id,
            decision=decision,
            rejection_reason=rejection_reason,
            explanation=explanation,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    return _redirect("/team-admin/dashboard")


@router.post("/team-admin/transfers/{transfer_id}/register")
def complete_transfer_route(
    transfer_id: int,
    request: Request,
    player_consent_form: UploadFile | None = File(None),
    player_agreement_form: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    try:
        # Try player_consent_form first, then fallback to player_agreement_form
        agreement_form_path = _safe_upload(player_consent_form, "player-agreements")
        if not agreement_form_path:
            agreement_form_path = _safe_upload(player_agreement_form, "player-agreements")
        complete_transfer_registration(
            db,
            team_admin_id=team_admin.team_admin_id,
            transfer_id=transfer_id,
            agreement_form_path=agreement_form_path,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    except Exception:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Transfer registration could not be completed right now. Please try again."},
        )
    return _redirect("/team-admin/dashboard")


@router.post("/team-admin/player-requests")
def request_player_route(
    request: Request,
    player_id: int = Form(...),
    from_team_id: int = Form(...),
    to_team_id: int | None = Form(None),
    request_type: str = Form(...),
    request_details: str = Form(...),
    request_loan_period: str | None = Form(None),
    request_registration_period: int = Form(...),
    db: Session = Depends(get_db),
):
    team_admin = _require_team_admin(request, db)
    
    # Get the requesting team (to_team)
    if to_team_id:
        to_team = db.scalar(
            select(Team)
            .where(Team.team_id == to_team_id)
            .where(Team.team_admin_id == team_admin.team_admin_id)
            .where(Team.status == ApprovalStatus.APPROVED.value)
        )
    else:
        to_team = db.scalar(
            select(Team)
            .where(Team.team_admin_id == team_admin.team_admin_id)
            .where(Team.status == ApprovalStatus.APPROVED.value)
        )
    
    if not to_team:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Your team must be approved before making player requests."},
        )
    try:
        request_player_from_team(
            db,
            team_admin_id=team_admin.team_admin_id,
            player_id=player_id,
            from_team_id=from_team_id,
            to_team_id=to_team.team_id,
            request_type=request_type,
            request_details=request_details,
            registration_period=request_registration_period,
            request_loan_period=request_loan_period,
        )
    except RegistrationError as exc:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": str(exc)},
        )
    
    return _redirect("/team-admin/dashboard")


@router.get("/api/search-players-by-name")
def search_players_by_name(name: str, db: Session = Depends(get_db)):
    """API endpoint to search for players by name and return their teams."""
    if not name or len(name.strip()) < 2:
        return {"players": []}
    
    # Search for players with similar names
    search_term = f"%{name.strip()}%"
    players = db.scalars(
        select(Player)
        .where(Player.full_name.ilike(search_term))
        .where(Player.status == ApprovalStatus.APPROVED.value)
        .where(Player.is_on_loan.is_(False))
        .options(selectinload(Player.team))
        .limit(20)
    ).all()
    
    result = {
        "players": [
            {
                "player_id": p.player_id,
                "player_name": p.full_name,
                "team_id": p.team_id,
                "team_name": p.team.team_name,
                "age_group": p.age_group,
                "photo_path": p.photo_path,
            }
            for p in players
        ]
    }
    
    return result


@router.post("/team-admin/transfers/{transfer_id}/register-transferred-player")
def register_transferred_player_route(
    transfer_id: int,
    request: Request,
    consent_form: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Register a transferred player on the receiving team after approved transfer."""
    team_admin = _require_team_admin(request, db)
    try:
        consent_form_path = _safe_upload(consent_form, "player-agreements")
        if not consent_form_path:
            return _render(
                request,
                "team_admin/action_result.html",
                {"error": "Parent/Guardian Consent Form is required for transfer registration."},
            )
        register_transferred_player(
            db,
            transfer_id=transfer_id,
            team_admin_id=team_admin.team_admin_id,
            consent_form_path=consent_form_path,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    except Exception:
        return _render(
            request,
            "team_admin/action_result.html",
            {"error": "Transfer registration could not be completed right now. Please try again."},
        )
    
    return _render(
        request,
        "team_admin/action_result.html",
        {"message": "Player transfer registered successfully. Awaiting SuperAdmin approval."},
    )


@router.post("/team-admin/transfers/{transfer_id}/unregister-player")
def unregister_transferred_player_route(
    transfer_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Unregister/hide player from original team after transfer is approved."""
    team_admin = _require_team_admin(request, db)
    
    try:
        unregister_transferred_player(
            db,
            transfer_id=transfer_id,
            team_admin_id=team_admin.team_admin_id,
        )
    except RegistrationError as exc:
        return _render(request, "team_admin/action_result.html", {"error": str(exc)})
    
    return _redirect("/team-admin/dashboard")


@router.get("/team-admin/player-agreement-template")
def player_agreement_template(request: Request, db: Session = Depends(get_db)):
    _require_team_admin(request, db)
    body = """MAFETENG YOUTH DEVELOPMENT LEAGUE
DEVELOPMENT PLAYER-CLUB AGREEMENT

Player Full Name:
Date of Birth:
Identity Document Number:
Club / Team Name:
Parent / Guardian Name:
Parent / Guardian Contact:

Agreement:
The player, parent/guardian and club confirm that the player is registered with the club for development football participation under league rules.

Player Signature:
Parent / Guardian Signature:
Club Representative Signature:
Date:
"""
    return Response(
        body,
        media_type="text/plain",
        headers={
            "Content-Disposition": "attachment; filename=development-player-club-agreement.txt"
        },
    )


@router.get("/team-admin/parent-consent-template")
def parent_consent_template(request: Request, db: Session = Depends(get_db)):
    _require_team_admin(request, db)
    body = """MAFETENG YOUTH DEVELOPMENT LEAGUE
PARENT/GUARDIAN CONSENT FORM

PLAYER INFORMATION
Player Full Name: _________________________________
Date of Birth: _________________________________
Gender: ☐ Male  ☐ Female
Identity Document Number: _________________________________
Nationality: _________________________________

PARENT/GUARDIAN INFORMATION
Parent/Guardian Full Name: _________________________________
Contact Phone: _________________________________
Email: _________________________________
Residential Address: _________________________________

TEAM INFORMATION
Team/Club Name: _________________________________
Team Representative Name: _________________________________
Team Representative Signature: _________________________________

CONSENT DECLARATION
I, the parent/guardian of the above named player, hereby give my consent for the player to participate in football/soccer activities under the Mafeteng Youth Development League. I acknowledge that:

1. I have read and understand the league rules and regulations
2. The player is in good health and fit to participate
3. I authorize the team to represent the player in league competitions
4. I accept responsibility for the player during all league activities
5. The player has my permission to wear the team uniform and participate in all authorized activities

PLAYER ACKNOWLEDGMENT
I, the player named above, confirm that I am participating willingly and understand the code of conduct.

Parent/Guardian Signature: _________________________ Date: _____________
Player Signature: _________________________ Date: _____________

TEAM REPRESENTATIVE CONFIRMATION
I certify that I have obtained proper consent from the player's parent/guardian.

Team Representative Signature: _________________________ Date: _____________
"""
    return Response(
        body,
        media_type="text/plain",
        headers={
            "Content-Disposition": "attachment; filename=parent-guardian-consent-form.txt"
        },
    )
