from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import re
from typing import Iterable

from sqlalchemy import delete, func, inspect, select
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
    ResultVerification,
    Team,
    TeamAdmin,
    User,
    UserRole,
)
from app.services.email import send_notification_email
from app.services.registration import RegistrationError


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


def _normalize_goal_type(value: str | None) -> str:
    raw = " ".join((value or "").split()).lower()
    return GOAL_TYPE_ALIASES.get(raw, raw.title() if raw else "Open Play")


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


def get_notifications_for_user(db: Session, user_id: int, *, limit: int = 20) -> list[Notification]:
    if not _has_table(db, "notifications"):
        return []
    return db.scalars(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.notification_id.desc())
        .limit(limit)
    ).all()


def mark_notification_read(db: Session, notification_id: int, user_id: int) -> Notification:
    if not _has_table(db, "notifications"):
        raise RegistrationError("Notifications are not available yet.")
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
    if fixture_date < datetime.utcnow() + timedelta(days=7):
        raise RegistrationError("Fixtures must be scheduled at least 7 days before match day.")
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
    if fixture_date < datetime.utcnow() + timedelta(days=7):
        raise RegistrationError("Fixtures must be scheduled at least 7 days before match day.")

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
    if new_date < datetime.utcnow() + timedelta(days=7):
        raise RegistrationError("Fixtures must be scheduled at least 7 days before match day.")
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


def submit_match_result(
    db: Session,
    *,
    team_admin_id: int,
    fixture_id: int,
    home_score: int,
    away_score: int,
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

    match = fixture.match or Match(fixture_id=fixture.fixture_id, match_date=fixture.fixture_date, status="scheduled")
    if not fixture.match:
        db.add(match)
        db.flush()

    submission = db.scalar(
        select(MatchResultSubmission).where(
            MatchResultSubmission.match_id == match.match_id,
            MatchResultSubmission.submitted_by_team_admin_id == team_admin_id,
        )
    )
    if not submission:
        submission = MatchResultSubmission(
            match_id=match.match_id,
            submitted_by_team_admin_id=team_admin_id,
            home_score=home_score,
            away_score=away_score,
            scorer_names_text=scorer_names_text,
            goal_types_text=goal_types_text,
            assist_names_text=assist_names_text,
            status=ApprovalStatus.PENDING.value,
        )
        db.add(submission)
    else:
        submission.home_score = home_score
        submission.away_score = away_score
        submission.scorer_names_text = scorer_names_text
        submission.goal_types_text = goal_types_text
        submission.assist_names_text = assist_names_text
        submission.status = ApprovalStatus.PENDING.value
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
    scorers = _split_items(submission.scorer_names_text)
    goal_types = _split_items(submission.goal_types_text)
    assists = _split_items(submission.assist_names_text)
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
    decision: str = ApprovalStatus.APPROVED.value,
) -> MatchResultSubmission:
    submission = db.get(MatchResultSubmission, submission_id)
    if not submission:
        raise RegistrationError("Result submission was not found.")

    match = submission.match
    if not match or not match.fixture:
        raise RegistrationError("Linked match was not found.")

    submission.home_score = home_score
    submission.away_score = away_score
    submission.scorer_names_text = scorer_names_text
    submission.goal_types_text = goal_types_text
    submission.assist_names_text = assist_names_text
    submission.status = decision
    match.home_score = home_score
    match.away_score = away_score
    match.status = "completed" if decision == ApprovalStatus.APPROVED.value else "reviewed"

    verification = submission.verification
    if not verification:
        verification = ResultVerification(
            submission_id=submission.submission_id,
            verified_by_admin_id=super_admin_id,
            decision=decision,
        )
        db.add(verification)
    else:
        verification.verified_by_admin_id = super_admin_id
        verification.decision = decision
        verification.verification_date = datetime.utcnow()

    if decision == ApprovalStatus.APPROVED.value:
        _rebuild_match_events(db, match, submission)
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
        ordered[category_name] = sorted(
            rows.values(),
            key=lambda row: (
                -int(row["points"]),
                -int(row["goal_difference"]),
                -int(row["goals_for"]),
                str(row["team"].team_name).lower(),
            ),
        )
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
            selectinload(MatchEvent.match).selectinload(Match.fixture).selectinload(Fixture.home_team),
            selectinload(MatchEvent.match).selectinload(Match.fixture).selectinload(Fixture.away_team),
        )
    )
    if team_ids is not None:
        query = query.join(Player, Player.player_id == MatchEvent.player_id).where(Player.team_id.in_(list(team_id_set or [])))
    events = db.scalars(query).all()

    scorers: dict[int, dict[str, object]] = {}
    assisters: dict[int, dict[str, object]] = {}
    for event in events:
        if not event.player or not event.player.team:
            continue
        if team_id_set is not None and event.player.team_id not in team_id_set:
            continue
        target = None
        if event.event_type.startswith("goal:"):
            target = scorers
        elif event.event_type == "assist":
            target = assisters
        if target is None:
            continue
        entry = target.setdefault(
            event.player_id,
            {
                "player": event.player,
                "team": event.player.team,
                "category_name": event.player.team.category.category_name if event.player.team.category else "",
                "goals": 0,
                "assists": 0,
                "goal_types": defaultdict(int),
            },
        )
        if event.event_type.startswith("goal:"):
            entry["goals"] += 1
            goal_type = event.event_type.split(":", 1)[1]
            entry["goal_types"][goal_type] += 1
        elif event.event_type == "assist":
            entry["assists"] += 1

    def _format_goal_types(row: dict[str, object]) -> dict[str, int]:
        return dict(sorted(row["goal_types"].items(), key=lambda item: (-item[1], item[0])))

    scorer_rows = sorted(
        (
            {
                "player": row["player"],
                "team": row["team"],
                "category_name": row["category_name"],
                "goals": row["goals"],
                "goal_types": _format_goal_types(row),
            }
            for row in scorers.values()
        ),
        key=lambda row: (-row["goals"], row["player"].full_name.lower()),
    )
    assister_rows = sorted(
        (
            {
                "player": row["player"],
                "team": row["team"],
                "category_name": row["category_name"],
                "assists": row["assists"],
            }
            for row in assisters.values()
        ),
        key=lambda row: (-row["assists"], row["player"].full_name.lower()),
    )
    return {"scorers": scorer_rows, "assisters": assister_rows}
