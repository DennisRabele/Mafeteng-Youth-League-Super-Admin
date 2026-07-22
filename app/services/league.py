from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import re
from typing import Iterable

from sqlalchemy import delete, func, inspect, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ApprovalStatus,
    Category,
    Fixture,
    FixtureStatus,
    Match,
    MatchEvent,
    MatchResultSubmission,
    Notification,
    Season,
    Player,
    PlayerTransferRequest,
    ResultVerification,
    Team,
    TeamAdmin,
    User,
    UserRole,
)
from app.services.email import send_notification_email
from app.services.registration import RegistrationError
from app.services.storage import delete_upload


GOAL_TYPE_ALIASES = {
    "freekick": "Free Kick",
    "free kick": "Free Kick",
    "penalty": "Penalty",
    "header from open play": "Header From Open Play",
    "header from corner kick": "Header From Corner Kick",
    "open play": "Open Play",
}


def _split_items(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [item.strip() for item in re.split(r"[\n,;]+", value) if item.strip()]
    return parts


def _split_result_lines(value: str | None) -> list[str]:
    return _split_items(value)


def _normalize_goal_type(value: str | None) -> str:
    raw = " ".join((value or "").split()).lower()
    return GOAL_TYPE_ALIASES.get(raw, raw.title() if raw else "Open Play")


def _player_identity_key(player: Player) -> str:
    parent_name = player.parent.name.strip().casefold() if player.parent and player.parent.name else ""
    parent_contact = player.parent.contact.strip().casefold() if player.parent and player.parent.contact else ""
    components = [
        player.full_name.strip().casefold(),
        player.dob.isoformat() if player.dob else "",
        (player.gender or "").strip().casefold(),
        (player.nationality or "").strip().casefold(),
        parent_name,
        parent_contact,
    ]
    return "|".join(components)


def _has_table(db: Session, table_name: str) -> bool:
    try:
        return inspect(db.get_bind()).has_table(table_name)
    except Exception:
        return False


def _team_admin_user_ids(db: Session, team_ids: Iterable[int] | None = None) -> list[int]:
    query = select(TeamAdmin.user_id).join(Team, Team.team_admin_id == TeamAdmin.team_admin_id)
    if team_ids is not None:
        query = query.where(Team.team_id.in_(list(team_ids)))
    return list(db.scalars(query).all())


def create_notification(
    db: Session,
    *,
    user_id: int,
    title: str,
    message: str,
    link: str | None = None,
    commit: bool = True,
) -> Notification:
    if not _has_table(db, "notifications"):
        return Notification(
            user_id=user_id,
            title=title.strip(),
            message=message.strip(),
            link=link.strip() if link else None,
        )
    notification = Notification(
        user_id=user_id,
        title=title.strip(),
        message=message.strip(),
        link=link.strip() if link else None,
    )
    db.add(notification)
    if commit:
        db.commit()
        db.refresh(notification)
        user = db.get(User, user_id)
        if user and user.email:
            try:
                send_notification_email(
                    to_email=user.email,
                    title=notification.title,
                    message=notification.message,
                    link=notification.link,
                )
            except Exception:
                pass
    else:
        db.flush()
    return notification


def broadcast_notifications(
    db: Session,
    *,
    user_ids: Iterable[int],
    title: str,
    message: str,
    link: str | None = None,
) -> None:
    if not _has_table(db, "notifications"):
        return
    notifications: list[Notification] = []
    for user_id in user_ids:
        notifications.append(
            create_notification(db, user_id=user_id, title=title, message=message, link=link, commit=False)
        )
    db.commit()
    for notification in notifications:
        user = db.get(User, notification.user_id)
        if user and user.email:
            try:
                send_notification_email(
                    to_email=user.email,
                    title=notification.title,
                    message=notification.message,
                    link=notification.link,
                )
            except Exception:
                pass


def notify_super_admins(db: Session, title: str, message: str, link: str | None = None) -> None:
    user_ids = db.scalars(select(User.user_id).where(User.role == UserRole.SUPER_ADMIN.value)).all()
    broadcast_notifications(db, user_ids=user_ids, title=title, message=message, link=link)


def notify_team_admins_for_teams(
    db: Session,
    team_ids: Iterable[int],
    title: str,
    message: str,
    link: str | None = None,
) -> None:
    user_ids = _team_admin_user_ids(db, team_ids)
    if user_ids:
        broadcast_notifications(db, user_ids=user_ids, title=title, message=message, link=link)


def notify_team_admin(db: Session, team_id: int, title: str, message: str, link: str | None = None) -> None:
    user_ids = _team_admin_user_ids(db, [team_id])
    if user_ids:
        broadcast_notifications(db, user_ids=user_ids, title=title, message=message, link=link)


def purge_expired_result_files(db: Session) -> int:
    if not (_has_table(db, "match_result_submissions") and _has_table(db, "result_verifications")):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=2)
    submissions = db.scalars(
        select(MatchResultSubmission)
        .join(ResultVerification, ResultVerification.submission_id == MatchResultSubmission.submission_id)
        .options(selectinload(MatchResultSubmission.verification))
        .where(
            MatchResultSubmission.result_file_path.is_not(None),
            ResultVerification.decision == ApprovalStatus.APPROVED.value,
            ResultVerification.verification_date <= cutoff,
        )
    ).all()
    deleted = 0
    for submission in submissions:
        if submission.result_file_path:
            delete_upload(submission.result_file_path, "match-results")
            submission.result_file_path = None
            deleted += 1
    if deleted:
        db.commit()
    return deleted


def get_notifications_for_user(db: Session, user_id: int, *, limit: int = 20) -> list[Notification]:
    if not _has_table(db, "notifications"):
        return []
    purge_expired_notifications(db)
    return db.scalars(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.notification_id.desc())
        .limit(limit)
    ).all()


def mark_notification_read(db: Session, notification_id: int, user_id: int) -> Notification:
    if not _has_table(db, "notifications"):
        raise RegistrationError("Notifications are not available yet.")
    purge_expired_notifications(db)
    notification = db.scalar(
        select(Notification).where(
            Notification.notification_id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if not notification:
        raise RegistrationError("Notification was not found.")
    notification.is_read = True
    db.commit()
    db.refresh(notification)
    return notification


def purge_expired_notifications(db: Session) -> int:
    if not _has_table(db, "notifications"):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=14)
    deleted = db.execute(
        delete(Notification).where(Notification.created_at < cutoff)
    )
    db.commit()
    return int(getattr(deleted, "rowcount", 0) or 0)


def delete_notification(db: Session, notification_id: int, user_id: int) -> None:
    if not _has_table(db, "notifications"):
        raise RegistrationError("Notifications are not available yet.")
    purge_expired_notifications(db)
    deleted = db.execute(
        delete(Notification).where(
            Notification.notification_id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if not getattr(deleted, "rowcount", 0):
        raise RegistrationError("Notification was not found.")
    db.commit()


def delete_all_notifications(db: Session, user_id: int) -> int:
    if not _has_table(db, "notifications"):
        raise RegistrationError("Notifications are not available yet.")
    purge_expired_notifications(db)
    deleted = db.execute(delete(Notification).where(Notification.user_id == user_id))
    db.commit()
    return int(getattr(deleted, "rowcount", 0) or 0)


def create_fixture(
    db: Session,
    *,
    category_id: int,
    home_team_id: int,
    away_team_id: int,
    fixture_date: datetime,
    venue: str,
    status: str = FixtureStatus.PUBLISHED.value,
    created_by_super_admin_id: int | None = None,
) -> Fixture:
    category = db.get(Category, category_id)
    home_team = db.get(Team, home_team_id)
    away_team = db.get(Team, away_team_id)
    if not category:
        raise RegistrationError("Selected category does not exist.")
    if not home_team or not away_team:
        raise RegistrationError("One or both selected teams do not exist.")
    if home_team.team_id == away_team.team_id:
        raise RegistrationError("A fixture must involve two different teams.")
    if home_team.status != ApprovalStatus.APPROVED.value or away_team.status != ApprovalStatus.APPROVED.value:
        raise RegistrationError("Both teams must be approved before a fixture can be created.")
    if home_team.category_id != category_id or away_team.category_id != category_id:
        raise RegistrationError("Selected teams must belong to the chosen category.")
    if fixture_date < datetime.utcnow() + timedelta(days=2):
        raise RegistrationError("Fixtures must be scheduled at least 2 days before match day.")
    season = db.scalar(select(Season).order_by(Season.start_date.desc()))
    if not season:
        raise RegistrationError("No active season is available for fixture creation.")

    fixture = Fixture(
        season_id=season.season_id,
        category_id=category_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        fixture_date=fixture_date,
        venue=venue.strip(),
        status=status,
        created_by_super_admin_id=created_by_super_admin_id,
    )
    db.add(fixture)
    db.flush()
    db.add(Match(fixture_id=fixture.fixture_id, match_date=fixture_date, status="scheduled"))
    db.commit()
    db.refresh(fixture)
    notify_super_admins(
        db,
        "New fixture created",
        f"{home_team.team_name} vs {away_team.team_name} has been scheduled for {fixture_date:%Y-%m-%d %H:%M} at {venue}.",
        "/super-admin#fixtures",
    )
    notify_team_admins_for_teams(
        db,
        [home_team_id, away_team_id],
        "Fixture update",
        f"{home_team.team_name} vs {away_team.team_name} has been scheduled for {fixture_date:%Y-%m-%d %H:%M} at {venue}.",
        "/team-admin/dashboard#fixtures",
    )
    return fixture


def update_fixture(
    db: Session,
    *,
    fixture_id: int,
    fixture_date: datetime,
    venue: str,
    status: str | None = None,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
    category_id: int | None = None,
) -> Fixture:
    fixture = db.get(Fixture, fixture_id)
    if not fixture:
        raise RegistrationError("Fixture was not found.")

    fixture_category_id = category_id or fixture.category_id
    home_team_id = home_team_id or fixture.home_team_id
    away_team_id = away_team_id or fixture.away_team_id
    if fixture_date < datetime.utcnow() + timedelta(days=2):
        raise RegistrationError("Fixtures must be scheduled at least 2 days before match day.")

    category = db.get(Category, fixture_category_id)
    home_team = db.get(Team, home_team_id)
    away_team = db.get(Team, away_team_id)
    if not category or not home_team or not away_team:
        raise RegistrationError("Selected fixture data is invalid.")
    if home_team.category_id != fixture_category_id or away_team.category_id != fixture_category_id:
        raise RegistrationError("Selected teams must belong to the chosen category.")
    season = db.scalar(select(Season).order_by(Season.start_date.desc()))
    if season and not fixture.season_id:
        fixture.season_id = season.season_id

    fixture.category_id = fixture_category_id
    fixture.home_team_id = home_team_id
    fixture.away_team_id = away_team_id
    fixture.fixture_date = fixture_date
    fixture.venue = venue.strip()
    if status:
        fixture.status = status
    if fixture.match:
        fixture.match.match_date = fixture_date
    else:
        db.add(Match(fixture_id=fixture.fixture_id, match_date=fixture_date, status="scheduled"))
    db.commit()
    db.refresh(fixture)
    notify_super_admins(
        db,
        "Fixture updated",
        f"{home_team.team_name} vs {away_team.team_name} has been updated for {fixture_date:%Y-%m-%d %H:%M} at {venue}.",
        "/super-admin#fixtures",
    )
    notify_team_admins_for_teams(
        db,
        [home_team_id, away_team_id],
        "Fixture updated",
        f"{home_team.team_name} vs {away_team.team_name} has been updated for {fixture_date:%Y-%m-%d %H:%M} at {venue}.",
        "/team-admin/dashboard#fixtures",
    )
    return fixture


def postpone_fixture(db: Session, fixture_id: int, new_date: datetime) -> Fixture:
    fixture = db.get(Fixture, fixture_id)
    if not fixture:
        raise RegistrationError("Fixture was not found.")
    if new_date < datetime.utcnow() + timedelta(days=2):
        raise RegistrationError("Fixtures must be scheduled at least 2 days before match day.")
    fixture.fixture_date = new_date
    fixture.status = FixtureStatus.POSTPONED.value
    if fixture.match:
        fixture.match.match_date = new_date
    db.commit()
    db.refresh(fixture)
    notify_super_admins(
        db,
        "Fixture postponed",
        f"{fixture.home_team.team_name} vs {fixture.away_team.team_name} has been postponed to {new_date:%Y-%m-%d %H:%M}.",
        "/super-admin#fixtures",
    )
    notify_team_admins_for_teams(
        db,
        [fixture.home_team_id, fixture.away_team_id],
        "Fixture postponed",
        f"{fixture.home_team.team_name} vs {fixture.away_team.team_name} has been postponed to {new_date:%Y-%m-%d %H:%M}.",
        "/team-admin/dashboard#fixtures",
    )
    return fixture


def _fixture_allows_result_submission(fixture: Fixture) -> bool:
    return fixture.fixture_date <= datetime.utcnow()


def _clear_match_result_state(db: Session, match: Match) -> None:
    match.home_score = None
    match.away_score = None
    match.status = "reviewed"
    db.execute(delete(MatchEvent).where(MatchEvent.match_id == match.match_id))


def _validate_match_result_payload(
    *,
    home_score: int,
    away_score: int,
    scorer_names_text: str | None,
    goal_types_text: str | None,
    assist_names_text: str | None,
    require_result_details: bool = False,
) -> None:
    total_goals = max(0, home_score + away_score)
    scorers = _split_result_lines(scorer_names_text)
    goal_types = _split_result_lines(goal_types_text)
    assists = _split_result_lines(assist_names_text)

    if not require_result_details and not any((scorers, goal_types, assists)):
        return

    if total_goals == 0:
        if any(item for item in scorers + goal_types + assists):
            raise RegistrationError("A 0-0 result must not include scorer details.")
        return

    if len(scorers) != total_goals:
        raise RegistrationError(f"Expected {total_goals} scorer entries for this result.")
    if len(goal_types) != total_goals:
        raise RegistrationError(f"Expected {total_goals} goal type entries for this result.")
    if len(assists) > total_goals:
        raise RegistrationError(f"Expected at most {total_goals} assist entries for this result.")
    if any(not scorer for scorer in scorers):
        raise RegistrationError("Each goal row must include a scorer name.")


def submit_match_result(
    db: Session,
    *,
    team_admin_id: int,
    fixture_id: int,
    home_score: int,
    away_score: int,
    result_file_path: str | None = None,
    scorer_names_text: str | None,
    goal_types_text: str | None,
    assist_names_text: str | None,
) -> MatchResultSubmission:
    fixture = db.get(Fixture, fixture_id)
    if not fixture or fixture.home_team is None or fixture.away_team is None:
        raise RegistrationError("Fixture was not found.")
    if not _fixture_allows_result_submission(fixture):
        raise RegistrationError("Results can only be entered after the fixture has been played.")
    if team_admin_id not in {fixture.home_team.team_admin_id, fixture.away_team.team_admin_id}:
        raise RegistrationError("You can only submit results for fixtures involving your teams.")
    _validate_match_result_payload(
        home_score=home_score,
        away_score=away_score,
        scorer_names_text=scorer_names_text,
        goal_types_text=goal_types_text,
        assist_names_text=assist_names_text,
        require_result_details=False,
    )

    match = fixture.match or Match(fixture_id=fixture.fixture_id, match_date=fixture.fixture_date, status="scheduled")
    if not fixture.match:
        db.add(match)
        db.flush()

    existing_submission = db.scalar(
        select(MatchResultSubmission)
        .where(MatchResultSubmission.match_id == match.match_id)
        .order_by(MatchResultSubmission.submission_id.asc())
    )
    if existing_submission and existing_submission.submitted_by_team_admin_id != team_admin_id:
        team_ids = list(
            db.scalars(
                select(Team.team_id).where(
                    Team.team_admin_id == team_admin_id,
                    Team.team_id.in_([fixture.home_team_id, fixture.away_team_id]),
                )
            ).all()
        )
        if team_ids:
            notify_team_admins_for_teams(
                db,
                team_ids,
                "Result already submitted",
                f"Result for {fixture.home_team.team_name} vs {fixture.away_team.team_name} was already set by the other team admin.",
                "/team-admin/dashboard#results",
            )
        raise RegistrationError("This fixture already has a result submission from the other team admin.")

    submission = existing_submission
    if not submission:
        submission = MatchResultSubmission(
            match_id=match.match_id,
            submitted_by_team_admin_id=team_admin_id,
            home_score=home_score,
            away_score=away_score,
            result_file_path=result_file_path,
            scorer_names_text=scorer_names_text,
            goal_types_text=goal_types_text,
            assist_names_text=assist_names_text,
            status=ApprovalStatus.PENDING.value,
        )
        db.add(submission)
    else:
        old_file_path = submission.result_file_path
        if submission.verification:
            db.delete(submission.verification)
        if submission.status != ApprovalStatus.PENDING.value:
            _clear_match_result_state(db, match)
        submission.home_score = home_score
        submission.away_score = away_score
        submission.result_file_path = result_file_path or submission.result_file_path
        submission.scorer_names_text = scorer_names_text
        submission.goal_types_text = goal_types_text
        submission.assist_names_text = assist_names_text
        submission.status = ApprovalStatus.PENDING.value
        if result_file_path and old_file_path and old_file_path != result_file_path:
            delete_upload(old_file_path, "match-results")
    db.commit()
    db.refresh(submission)
    notify_super_admins(
        db,
        "New result submission",
        f"Result submitted for {fixture.home_team.team_name} vs {fixture.away_team.team_name}.",
        "/super-admin#results",
    )
    return submission


def _find_player_for_fixture(db: Session, fixture: Fixture, player_name: str) -> Player | None:
    normalized = " ".join(player_name.split()).casefold()
    if not normalized:
        return None
    return db.scalar(
        select(Player)
        .options(selectinload(Player.team))
        .where(
            func.lower(Player.full_name) == normalized,
            Player.team_id.in_([fixture.home_team_id, fixture.away_team_id]),
        )
    )


def _rebuild_match_events(db: Session, match: Match, submission: MatchResultSubmission) -> None:
    db.execute(delete(MatchEvent).where(MatchEvent.match_id == match.match_id))
    scorers = _split_result_lines(submission.scorer_names_text)
    goal_types = _split_result_lines(submission.goal_types_text)
    assists = _split_result_lines(submission.assist_names_text)
    if len(assists) < len(scorers):
        assists.extend([""] * (len(scorers) - len(assists)))
    for index, scorer_name in enumerate(scorers):
        player = _find_player_for_fixture(db, match.fixture, scorer_name)
        goal_type = _normalize_goal_type(goal_types[index] if index < len(goal_types) else None)
        db.add(
            MatchEvent(
                match_id=match.match_id,
                player_id=player.player_id if player else None,
                event_type=f"goal:{goal_type}",
                minute=index + 1,
            )
        )
        if index < len(assists):
            assister = _find_player_for_fixture(db, match.fixture, assists[index])
            if assister:
                db.add(
                    MatchEvent(
                        match_id=match.match_id,
                        player_id=assister.player_id,
                        event_type="assist",
                        minute=index + 1,
                    )
                )


def verify_match_result(
    db: Session,
    *,
    submission_id: int,
    super_admin_id: int,
    home_score: int,
    away_score: int,
    scorer_names_text: str | None,
    goal_types_text: str | None,
    assist_names_text: str | None,
    rejection_reason: str | None = None,
    decision: str = ApprovalStatus.APPROVED.value,
) -> MatchResultSubmission:
    submission = db.get(MatchResultSubmission, submission_id)
    if not submission:
        raise RegistrationError("Result submission was not found.")

    match = submission.match
    if not match or not match.fixture:
        raise RegistrationError("Linked match was not found.")
    normalized_rejection_reason = (rejection_reason or "").strip()
    if decision == ApprovalStatus.REJECTED.value and not normalized_rejection_reason:
        raise RegistrationError("A rejection reason is required when rejecting a result.")
    _validate_match_result_payload(
        home_score=home_score,
        away_score=away_score,
        scorer_names_text=scorer_names_text,
        goal_types_text=goal_types_text,
        assist_names_text=assist_names_text,
        require_result_details=decision == ApprovalStatus.APPROVED.value,
    )

    submission.home_score = home_score
    submission.away_score = away_score
    submission.scorer_names_text = scorer_names_text
    submission.goal_types_text = goal_types_text
    submission.assist_names_text = assist_names_text
    submission.status = decision

    verification = submission.verification
    if not verification:
        verification = ResultVerification(
            submission_id=submission.submission_id,
            verified_by_admin_id=super_admin_id,
            decision=decision,
            rejection_reason=normalized_rejection_reason or None,
        )
        db.add(verification)
    else:
        verification.verified_by_admin_id = super_admin_id
        verification.decision = decision
        verification.verification_date = datetime.utcnow()
        verification.rejection_reason = normalized_rejection_reason or None

    if decision == ApprovalStatus.APPROVED.value:
        match.home_score = home_score
        match.away_score = away_score
        match.status = "completed"
        _rebuild_match_events(db, match, submission)
    else:
        _clear_match_result_state(db, match)

    if decision == ApprovalStatus.APPROVED.value:
        notify_team_admins_for_teams(
            db,
            [match.fixture.home_team_id, match.fixture.away_team_id],
            "Result updated",
            f"Result for {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name} is now {home_score}-{away_score}. Open Results to review the verified score, League tables to see updated standings, and Performances to view player stats.",
            "/team-admin/dashboard#results",
        )
        notify_super_admins(
            db,
            "Results updated",
            f"Result for {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name} has been approved.",
            "/super-admin#results",
        )
        notify_super_admins(
            db,
            "League tables updated",
            f"League tables were recalculated after {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name}.",
            "/super-admin#league-tables",
        )
        notify_super_admins(
            db,
            "Performances updated",
            f"Player performances were updated after {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name}.",
            "/super-admin#performances",
        )
    else:
        rejection_note = normalized_rejection_reason or "No reason was provided."
        notify_team_admins_for_teams(
            db,
            [match.fixture.home_team_id, match.fixture.away_team_id],
            "Result rejected",
            f"Result for {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name} was rejected. Reason: {rejection_note} You can edit and resubmit the result.",
            "/team-admin/dashboard#results",
        )
        notify_super_admins(
            db,
            "Result rejected",
            f"Result for {match.fixture.home_team.team_name} vs {match.fixture.away_team.team_name} was rejected by Super Admin ID {super_admin_id}.",
            "/super-admin#results",
        )

    db.commit()
    db.refresh(submission)
    return submission


def get_league_tables(db: Session, *, team_ids: Iterable[int] | None = None) -> dict[str, list[dict[str, object]]]:
    team_query = select(Team).options(selectinload(Team.category)).where(Team.status == ApprovalStatus.APPROVED.value)
    if team_ids is not None:
        team_query = team_query.where(Team.team_id.in_(list(team_ids)))
    teams = db.scalars(team_query).all()
    standings: dict[str, dict[int, dict[str, object]]] = defaultdict(dict)
    for team in teams:
        standings[team.category.category_name][team.team_id] = {
            "team": team,
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "goals_for": 0,
            "goals_against": 0,
            "goal_difference": 0,
            "points": 0,
        }

    matches = db.scalars(
        select(Match)
        .join(Fixture, Fixture.fixture_id == Match.fixture_id)
        .options(
            selectinload(Match.fixture).selectinload(Fixture.category),
            selectinload(Match.fixture).selectinload(Fixture.home_team).selectinload(Team.category),
            selectinload(Match.fixture).selectinload(Fixture.away_team).selectinload(Team.category),
        )
        .where(Match.home_score.is_not(None), Match.away_score.is_not(None))
    ).all()
    for match in matches:
        fixture = match.fixture
        if not fixture or not fixture.home_team or not fixture.away_team:
            continue
        category_name = fixture.category.category_name
        if fixture.home_team.team_id not in standings[category_name]:
            continue
        if fixture.away_team.team_id not in standings[category_name]:
            continue
        home = standings[category_name][fixture.home_team.team_id]
        away = standings[category_name][fixture.away_team.team_id]
        home_score = match.home_score or 0
        away_score = match.away_score or 0

        home["played"] += 1
        away["played"] += 1
        home["goals_for"] += home_score
        home["goals_against"] += away_score
        away["goals_for"] += away_score
        away["goals_against"] += home_score

        if home_score > away_score:
            home["wins"] += 1
            away["losses"] += 1
            home["points"] += 3
        elif home_score < away_score:
            away["wins"] += 1
            home["losses"] += 1
            away["points"] += 3
        else:
            home["draws"] += 1
            away["draws"] += 1
            home["points"] += 1
            away["points"] += 1

    for category_rows in standings.values():
        for row in category_rows.values():
            row["goal_difference"] = row["goals_for"] - row["goals_against"]

    ordered: dict[str, list[dict[str, object]]] = {}
    for category_name, rows in standings.items():
        category_rows = sorted(
            rows.values(),
            key=lambda row: (
                -int(row["points"]),
                -int(row["goal_difference"]),
                -int(row["goals_for"]),
                str(row["team"].team_name).lower(),
            ),
        )
        for index, row in enumerate(category_rows, start=1):
            row["position"] = index
            row["team_logo"] = row["team"].logo
        ordered[category_name] = category_rows
    return ordered


def get_player_performances(
    db: Session,
    *,
    team_ids: Iterable[int] | None = None,
) -> dict[str, list[dict[str, object]]]:
    team_id_set = set(team_ids) if team_ids is not None else None
    query = (
        select(MatchEvent)
        .join(Match, Match.match_id == MatchEvent.match_id)
        .join(Fixture, Fixture.fixture_id == Match.fixture_id)
        .options(
            selectinload(MatchEvent.player).selectinload(Player.team).selectinload(Team.category),
            selectinload(MatchEvent.player).selectinload(Player.original_team).selectinload(Team.category),
            selectinload(MatchEvent.player).selectinload(Player.parent),
            selectinload(MatchEvent.match).selectinload(Match.fixture).selectinload(Fixture.home_team),
            selectinload(MatchEvent.match).selectinload(Match.fixture).selectinload(Fixture.away_team),
        )
    )
    if team_id_set is not None:
        query = query.where(
            or_(
                Fixture.home_team_id.in_(list(team_id_set)),
                Fixture.away_team_id.in_(list(team_id_set)),
            )
        )
    events = db.scalars(query).all()

    player_groups: dict[str, dict[str, object]] = {}
    for event in events:
        if not event.player or not event.player.team:
            continue
        identity_key = _player_identity_key(event.player)
        group = player_groups.setdefault(
            identity_key,
            {
                "players": {},
                "primary_player": event.player,
                "goals": 0,
                "assists": 0,
                "goal_types": defaultdict(int),
                "category_totals": defaultdict(lambda: {"goals": 0, "assists": 0, "goal_types": defaultdict(int)}),
            },
        )
        group["players"][event.player.player_id] = event.player
        if event.player.player_id > group["primary_player"].player_id:
            group["primary_player"] = event.player

        if not event.event_type.startswith("goal:") and event.event_type != "assist":
            continue
        category_name = event.player.team.category.category_name if event.player.team.category else ""
        category_entry = group["category_totals"][category_name]
        if event.event_type.startswith("goal:"):
            group["goals"] += 1
            goal_type = event.event_type.split(":", 1)[1]
            group["goal_types"][goal_type] += 1
            category_entry["goals"] += 1
            category_entry["goal_types"][goal_type] += 1
        elif event.event_type == "assist":
            group["assists"] += 1
            category_entry["assists"] += 1

    player_ids = [player.player_id for group in player_groups.values() for player in group["players"].values()]
    transfer_history: dict[int, list[PlayerTransferRequest]] = defaultdict(list)
    if player_ids:
        transfer_rows = db.scalars(
            select(PlayerTransferRequest)
            .options(
                selectinload(PlayerTransferRequest.from_team),
                selectinload(PlayerTransferRequest.to_team),
            )
            .where(PlayerTransferRequest.player_id.in_(player_ids))
        ).all()
        for transfer in transfer_rows:
            transfer_history[transfer.player_id].append(transfer)

    def _format_goal_types(row: dict[str, object]) -> dict[str, int]:
        return dict(sorted(row["goal_types"].items(), key=lambda item: (-item[1], item[0])))

    def _format_category_totals(row: dict[str, object]) -> dict[str, dict[str, object]]:
        formatted: dict[str, dict[str, object]] = {}
        for category_name, totals in sorted(row["category_totals"].items(), key=lambda item: item[0].lower()):
            formatted[category_name] = {
                "goals": totals["goals"],
                "assists": totals["assists"],
                "goal_types": dict(sorted(totals["goal_types"].items(), key=lambda item: (-item[1], item[0]))),
            }
        return formatted

    def _collect_clubs(row: dict[str, object]) -> list[str]:
        clubs: list[str] = []
        seen: set[str] = set()

        for player in sorted(row["players"].values(), key=lambda item: item.player_id):
            sources = [
                player.team.team_name if player.team else None,
                player.original_team.team_name if player.original_team else None,
            ]
            for transfer in transfer_history.get(player.player_id, []):
                sources.extend([
                    transfer.from_team.team_name if transfer.from_team else None,
                    transfer.to_team.team_name if transfer.to_team else None,
                ])
            for club_name in sources:
                normalized = (club_name or "").strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    clubs.append(normalized)
        return clubs

    def _system_ids(row: dict[str, object]) -> list[str]:
        identifiers: list[str] = []
        seen: set[str] = set()
        for player in sorted(row["players"].values(), key=lambda item: item.player_id):
            identifier = player.player_code or f"PLAYER-{player.player_id}"
            if identifier not in seen:
                seen.add(identifier)
                identifiers.append(identifier)
        return identifiers

    performance_rows = []
    for group in player_groups.values():
        primary_player = group["primary_player"]
        team = primary_player.team
        category_name = team.category.category_name if team and team.category else ""
        performance_rows.append(
            {
                "player": primary_player,
                "team": team,
                "category_name": category_name,
                "system_ids": _system_ids(group),
                "clubs_played_for": _collect_clubs(group),
                "goals": group["goals"],
                "assists": group["assists"],
                "goal_types": _format_goal_types(group),
                "category_totals": _format_category_totals(group),
                "primary_system_id": primary_player.player_code or f"PLAYER-{primary_player.player_id}",
                "photo_path": primary_player.photo_path,
            }
        )

    scorer_rows = sorted(
        (
            {
                "player": row["player"],
                "team": row["team"],
                "category_name": row["category_name"],
                "system_id": row["primary_system_id"],
                "system_ids": row["system_ids"],
                "photo_path": row["photo_path"],
                "clubs_played_for": row["clubs_played_for"],
                "goals": row["goals"],
                "assists": row["assists"],
                "goal_types": row["goal_types"],
                "category_totals": row["category_totals"],
            }
            for row in performance_rows
            if row["goals"] > 0
        ),
        key=lambda row: (-row["goals"], -row["assists"], row["player"].full_name.lower()),
    )
    assister_rows = sorted(
        (
            {
                "player": row["player"],
                "team": row["team"],
                "category_name": row["category_name"],
                "system_id": row["primary_system_id"],
                "system_ids": row["system_ids"],
                "photo_path": row["photo_path"],
                "clubs_played_for": row["clubs_played_for"],
                "goals": row["goals"],
                "assists": row["assists"],
                "goal_types": row["goal_types"],
                "category_totals": row["category_totals"],
            }
            for row in performance_rows
            if row["assists"] > 0
        ),
        key=lambda row: (-row["assists"], -row["goals"], row["player"].full_name.lower()),
    )
    detailed_rows = sorted(
        performance_rows,
        key=lambda row: (-row["goals"], -row["assists"], row["player"].full_name.lower()),
    )
    return {"players": detailed_rows, "scorers": scorer_rows, "assisters": assister_rows}
