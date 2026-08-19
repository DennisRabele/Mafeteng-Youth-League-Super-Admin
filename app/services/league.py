from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging
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
    MatchDaySquad,
    MatchDaySquadMember,
    Notification,
    Season,
    Player,
    PlayerStatistic,
    PlayerTransferRequest,
    ResultVerification,
    Team,
    TeamAdmin,
    User,
    UserRole,
)
from app.services.email import send_notification_email
from app.services.registration import RegistrationError, player_can_play_for_category
from app.services.storage import delete_upload


logger = logging.getLogger(__name__)

GOAL_TYPE_ALIASES = {
    "freekick": "Freekick",
    "free kick": "Freekick",
    "penalty": "Penalty",
    "cornerkick": "Cornerkick",
    "corner kick": "Cornerkick",
    "from kickoff": "from KickOFF",
    "from kick off": "from KickOFF",
    "header": "Header",
    "tap in": "Tap In",
    "own goal": "Own Goal",
}

RESULT_TYPE_ALIASES = {
    "standard": "standard",
    "normal": "standard",
    "opponent_did_not_honour": "opponent_did_not_honour",
    "opponent did not honour the match": "opponent_did_not_honour",
    "did not honour": "opponent_did_not_honour",
    "did not honor": "opponent_did_not_honour",
    "opponent_forfeited": "opponent_forfeited",
    "opponent forfeited the match": "opponent_forfeited",
    "forfeited": "opponent_forfeited",
    "forfeit": "opponent_forfeited",
}

RESULT_TYPE_LABELS = {
    "standard": "Standard",
    "opponent_did_not_honour": "Opponent Did Not Honour The Match",
    "opponent_forfeited": "Opponent Forfeited The Match",
}

SPECIAL_RESULT_TYPES = {"opponent_did_not_honour", "opponent_forfeited"}


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


def _normalize_result_type(value: str | None) -> str:
    raw = " ".join((value or "").split()).lower()
    return RESULT_TYPE_ALIASES.get(raw, "standard")


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


def purge_expired_match_day_squads(db: Session) -> int:
    if not _has_table(db, "match_day_squads"):
        return 0
    cutoff = datetime.utcnow()
    squads = db.scalars(
        select(MatchDaySquad).where(
            MatchDaySquad.expires_at.is_not(None),
            MatchDaySquad.expires_at <= cutoff,
        )
    ).all()
    deleted = 0
    for squad in squads:
        db.delete(squad)
        deleted += 1
    if deleted:
        db.commit()
    return deleted


def delete_match_day_squad(
    db: Session,
    squad_id: int,
    *,
    team_ids: Iterable[int] | None = None,
) -> None:
    if not _has_table(db, "match_day_squads"):
        raise RegistrationError("Match day squads are not available yet.")
    query = select(MatchDaySquad).where(MatchDaySquad.squad_id == squad_id)
    if team_ids is not None:
        query = query.where(MatchDaySquad.team_id.in_(list(team_ids)))
    squad = db.scalar(query)
    if not squad:
        raise RegistrationError("Match day squad was not found.")
    db.delete(squad)
    db.commit()


def delete_match_day_squads(
    db: Session,
    *,
    team_ids: Iterable[int] | None = None,
) -> int:
    if not _has_table(db, "match_day_squads"):
        raise RegistrationError("Match day squads are not available yet.")
    query = select(MatchDaySquad)
    if team_ids is not None:
        query = query.where(MatchDaySquad.team_id.in_(list(team_ids)))
    squads = db.scalars(query).all()
    deleted = 0
    for squad in squads:
        db.delete(squad)
        deleted += 1
    if deleted:
        db.commit()
    return deleted


def _category_age_group(category_name: str | None) -> str | None:
    if not category_name:
        return None
    match = re.search(r"\bU\d{2}\b", category_name, re.IGNORECASE)
    return match.group(0).upper() if match else None


def create_match_day_squad(
    db: Session,
    *,
    fixture_id: int,
    team_id: int,
    generated_by_team_admin_id: int,
    player_ids: list[int],
    jersey_numbers: list[int],
) -> MatchDaySquad:
    if not _has_table(db, "match_day_squads"):
        raise RegistrationError("Match day squads are not available yet.")
    fixture = db.scalar(
        select(Fixture)
        .options(
            selectinload(Fixture.category),
            selectinload(Fixture.home_team).selectinload(Team.category),
            selectinload(Fixture.away_team).selectinload(Team.category),
        )
        .where(Fixture.fixture_id == fixture_id)
    )
    if not fixture or not fixture.category or not fixture.home_team or not fixture.away_team:
        raise RegistrationError("Selected fixture could not be found.")
    team = db.scalar(
        select(Team)
        .options(selectinload(Team.category), selectinload(Team.players))
        .where(
            Team.team_id == team_id,
            Team.status == ApprovalStatus.APPROVED.value,
        )
    )
    if not team or not team.category:
        raise RegistrationError("Selected team does not exist or is not approved.")
    if team.team_id not in {fixture.home_team_id, fixture.away_team_id}:
        raise RegistrationError("Selected team is not part of the chosen fixture.")
    if team.category_id != fixture.category_id:
        raise RegistrationError("Selected team does not match the fixture category.")

    if not _category_age_group(team.category.category_name):
        raise RegistrationError("Selected team category is not eligible for match day squads.")

    if not player_ids:
        raise RegistrationError("Select at least one player for the squad.")
    if len(player_ids) != len(jersey_numbers):
        raise RegistrationError("Each selected player must have a jersey number.")
    if len(set(player_ids)) != len(player_ids):
        raise RegistrationError("Each player can only appear once in the squad.")
    if len(set(jersey_numbers)) != len(jersey_numbers):
        raise RegistrationError("Each jersey number must be unique.")

    players = db.scalars(
        select(Player)
        .options(selectinload(Player.team).selectinload(Team.category))
        .where(Player.player_id.in_(player_ids))
    ).all()
    player_map = {player.player_id: player for player in players}
    if len(player_map) != len(player_ids):
        raise RegistrationError("One or more selected players could not be found.")

    eligible_players: list[Player] = []
    for player_id in player_ids:
        player = player_map[player_id]
        if player.status != ApprovalStatus.APPROVED.value:
            raise RegistrationError(f"{player.full_name} is not approved for selection.")
        if not player_can_play_for_category(player, team.category.category_name):
            raise RegistrationError(
                f"{player.full_name} is registered in {player.age_group or 'another category'} and cannot be selected for {team.category.category_name}."
            )
        eligible_players.append(player)

    now = datetime.utcnow()
    squad = MatchDaySquad(
        fixture_id=fixture.fixture_id,
        team_id=team.team_id,
        category_id=team.category_id,
        generated_by_team_admin_id=generated_by_team_admin_id,
        generated_at=now,
        verified_at=datetime.utcnow(),
        downloaded_at=None,
        expires_at=now + timedelta(hours=24),
        fixture_date_snapshot=fixture.fixture_date,
        venue_snapshot=fixture.venue,
        home_team_name_snapshot=fixture.home_team.team_name,
        home_team_logo_snapshot=fixture.home_team.logo,
        away_team_name_snapshot=fixture.away_team.team_name,
        away_team_logo_snapshot=fixture.away_team.logo,
        team_name_snapshot=team.team_name,
        team_logo_snapshot=team.logo,
        category_name_snapshot=team.category.category_name,
    )
    db.add(squad)
    db.flush()

    for player, jersey_number in zip(eligible_players, jersey_numbers, strict=True):
        db.add(
            MatchDaySquadMember(
                squad_id=squad.squad_id,
                player_id=player.player_id,
                jersey_number=jersey_number,
                player_name_snapshot=player.full_name,
                player_code_snapshot=player.player_code,
                age_group_snapshot=player.age_group,
            )
        )

    db.commit()
    db.refresh(squad)
    return squad


def get_match_day_squad(db: Session, squad_id: int) -> MatchDaySquad | None:
    if not _has_table(db, "match_day_squads"):
        return None
    return db.scalar(
        select(MatchDaySquad)
        .options(
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.category),
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.home_team),
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.away_team),
            selectinload(MatchDaySquad.team).selectinload(Team.category),
            selectinload(MatchDaySquad.generated_by).selectinload(TeamAdmin.user),
            selectinload(MatchDaySquad.members).selectinload(MatchDaySquadMember.player).selectinload(Player.team),
        )
        .where(MatchDaySquad.squad_id == squad_id)
    )


def get_team_admin_match_day_squads(db: Session, team_ids: Iterable[int]) -> list[MatchDaySquad]:
    if not _has_table(db, "match_day_squads"):
        return []
    return db.scalars(
        select(MatchDaySquad)
        .options(
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.category),
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.home_team),
            selectinload(MatchDaySquad.fixture).selectinload(Fixture.away_team),
            selectinload(MatchDaySquad.team).selectinload(Team.category),
            selectinload(MatchDaySquad.generated_by).selectinload(TeamAdmin.user),
            selectinload(MatchDaySquad.members),
        )
        .where(MatchDaySquad.team_id.in_(list(team_ids)))
        .order_by(MatchDaySquad.generated_at.desc(), MatchDaySquad.squad_id.desc())
    ).all()


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


def delete_notification(db: Session, notification_id: int, user_id: int) -> None:
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
    db.delete(notification)
    db.commit()


def delete_all_notifications_for_user(db: Session, user_id: int) -> int:
    if not _has_table(db, "notifications"):
        raise RegistrationError("Notifications are not available yet.")
    notifications = db.scalars(select(Notification).where(Notification.user_id == user_id)).all()
    count = len(notifications)
    for notification in notifications:
        db.delete(notification)
    db.commit()
    return count


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


def _clean_goal_type(value: str | None) -> str:
    normalized = _normalize_goal_type(value)
    allowed_goal_types = {"Penalty", "Freekick", "Cornerkick", "from KickOFF", "Header", "Tap In", "Own Goal"}
    if normalized not in allowed_goal_types:
        raise RegistrationError("Select a valid goal type.")
    return normalized


def _team_admin_fixture_context(
    fixture: Fixture,
    *,
    team_admin_id: int,
    approved_team_ids: Iterable[int] | None = None,
) -> tuple[Team, Team, str]:
    approved_team_id_set = {int(team_id) for team_id in approved_team_ids or []}
    if fixture.home_team and fixture.home_team.team_id in approved_team_id_set:
        return fixture.home_team, fixture.away_team, "home"
    if fixture.away_team and fixture.away_team.team_id in approved_team_id_set:
        return fixture.away_team, fixture.home_team, "away"
    if fixture.home_team and fixture.home_team.team_admin_id == team_admin_id:
        return fixture.home_team, fixture.away_team, "home"
    if fixture.away_team and fixture.away_team.team_admin_id == team_admin_id:
        return fixture.away_team, fixture.home_team, "away"
    raise RegistrationError("You can only submit results for fixtures involving your teams.")


def _coerce_selected_player_ids(values: Iterable[str | int | None]) -> list[int | str | None]:
    coerced: list[int | str | None] = []
    for value in values:
        if value is None:
            coerced.append(None)
            continue
        if isinstance(value, int):
            coerced.append(value)
            continue
        text_value = str(value).strip()
        if not text_value:
            coerced.append(None)
            continue
        try:
            coerced.append(int(text_value))
        except ValueError:
            coerced.append(text_value)
    return coerced


def _validate_match_result_payload(
    *,
    expected_goal_count: int,
    scorer_player_ids: list[int | None],
    goal_types: list[str | None],
    assist_player_ids: list[int | None],
    result_type: str = "standard",
) -> None:
    normalized_result_type = _normalize_result_type(result_type)
    if normalized_result_type in SPECIAL_RESULT_TYPES:
        if any(
            any(str(value or "").strip() for value in group)
            for group in (scorer_player_ids, goal_types, assist_player_ids)
        ):
            raise RegistrationError("Special results do not include scorer or assist details.")
        return

    if expected_goal_count == 0:
        if any(any(str(value or "").strip() for value in group) for group in (scorer_player_ids, goal_types, assist_player_ids)):
            raise RegistrationError("A 0-0 result must not include scorer details.")
        return

    if len(scorer_player_ids) != expected_goal_count:
        raise RegistrationError(f"Expected {expected_goal_count} scorer entries for this result.")
    if len(goal_types) != expected_goal_count:
        raise RegistrationError(f"Expected {expected_goal_count} goal type entries for this result.")
    if len(assist_player_ids) != expected_goal_count:
        raise RegistrationError(f"Expected {expected_goal_count} assister entries for this result.")
    for index, player_id in enumerate(scorer_player_ids):
        goal_type = _clean_goal_type(goal_types[index] if index < len(goal_types) else None)
        assist_player_id = assist_player_ids[index] if index < len(assist_player_ids) else None
        if goal_type == "Own Goal":
            if player_id is not None or assist_player_id is not None:
                raise RegistrationError("Own goals must not include a scorer or assister.")
            continue
        if player_id is None:
            raise RegistrationError("Each goal row must include a scorer name.")


def _selected_player_lookup_key(value: int | str | None) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return " ".join(str(value).split()).casefold()


def _player_lookup_map(
    db: Session,
    player_ids: list[int | str],
    *,
    team_admin_id: int | None = None,
) -> dict[int | str, Player]:
    if not player_ids:
        return {}
    numeric_ids = [player_id for player_id in player_ids if isinstance(player_id, int)]
    text_values = [str(player_id).strip() for player_id in player_ids if isinstance(player_id, str) and str(player_id).strip()]
    players_by_key: dict[int | str, Player] = {}

    if numeric_ids:
        query = select(Player).options(selectinload(Player.team).selectinload(Team.category)).where(Player.player_id.in_(numeric_ids))
        if team_admin_id is not None:
            query = query.join(Team, Player.team_id == Team.team_id).where(Team.team_admin_id == team_admin_id)
        players = db.scalars(query).all()
        for player in players:
            players_by_key[player.player_id] = player
            players_by_key[player.full_name.casefold()] = player
            if player.player_code:
                players_by_key[player.player_code.casefold()] = player
                players_by_key[f"{player.full_name} ({player.player_code})".casefold()] = player

    for text_value in text_values:
        normalized_text = " ".join(text_value.split()).casefold()
        display_match = re.match(r"^(?P<name>.+?)\s*\((?P<code>[^()]+)\)$", text_value)
        if display_match:
            name_value = display_match.group("name").strip().casefold()
            code_value = display_match.group("code").strip().casefold()
        else:
            name_value = normalized_text
            code_value = normalized_text
        query = select(Player).options(selectinload(Player.team).selectinload(Team.category)).where(
            or_(
                func.lower(func.trim(Player.full_name)) == name_value,
                func.lower(func.trim(Player.player_code)) == code_value,
                func.lower(func.trim(Player.full_name)) == normalized_text,
                func.lower(func.trim(Player.player_code)) == normalized_text,
            )
        )
        if team_admin_id is not None:
            query = query.join(Team, Player.team_id == Team.team_id).where(Team.team_admin_id == team_admin_id)
        player = db.scalar(query)
        if player:
            players_by_key[normalized_text] = player
            players_by_key[name_value] = player
            players_by_key[code_value] = player
            players_by_key[player.player_id] = player
            players_by_key[player.full_name.casefold()] = player
            if player.player_code:
                players_by_key[player.player_code.casefold()] = player
                players_by_key[f"{player.full_name} ({player.player_code})".casefold()] = player

    return players_by_key


def _assert_player_belongs_to_team(
    player: Player | None,
    submitting_team: Team,
    label: str,
    *,
    fixture_category_name: str | None = None,
) -> None:
    if not player or player.status != ApprovalStatus.APPROVED.value or not player.team:
        raise RegistrationError(f"Select only approved players from your own club for {label}.")
    if player.team.team_admin_id != submitting_team.team_admin_id:
        raise RegistrationError(f"Select only approved players from your own club for {label}.")
    if not player_can_play_for_category(player, fixture_category_name or submitting_team.category.category_name if submitting_team.category else None):
        raise RegistrationError(
            f"{player.full_name} is not eligible for {fixture_category_name or submitting_team.category.category_name or 'this fixture'}."
        )


def _serialize_submission_players(
    players: list[Player | None],
    assists: list[Player | None],
    goal_types: list[str],
) -> tuple[str, str, str]:
    scorer_names_text = "\n".join(player.full_name if player else "-" for player in players)
    goal_types_text = "\n".join(goal_types)
    assist_names_text = "\n".join(assister.full_name if assister else "-" for assister in assists)
    return scorer_names_text, goal_types_text, assist_names_text


def _remove_submission_statistics(db: Session, submission: MatchResultSubmission) -> None:
    db.execute(delete(PlayerStatistic).where(PlayerStatistic.submission_id == submission.submission_id))


def _write_submission_statistics(
    db: Session,
    *,
    submission: MatchResultSubmission,
    fixture: Fixture,
    team: Team,
    scorers: list[Player | None],
    assists: list[Player | None],
    goal_types: list[str],
) -> None:
    category = fixture.category or team.category
    category_id = category.category_id if category else None
    category_name = category.category_name if category else None
    for index, scorer in enumerate(scorers):
        goal_type = _clean_goal_type(goal_types[index] if index < len(goal_types) else None)
        if goal_type == "Own Goal":
            continue
        if scorer is None:
            continue
        db.add(
            PlayerStatistic(
                fixture_id=fixture.fixture_id,
                match_id=submission.match_id,
                submission_id=submission.submission_id,
                player_id=scorer.player_id,
                team_id=team.team_id,
                category_id=category_id,
                team_code=team.team_code,
                club_name=team.team_name,
                category_name=category_name,
                stat_type="goal",
                goal_type=goal_type,
            )
        )
        assister = assists[index] if index < len(assists) else None
        if assister:
            db.add(
                PlayerStatistic(
                    fixture_id=fixture.fixture_id,
                    match_id=submission.match_id,
                    submission_id=submission.submission_id,
                    player_id=assister.player_id,
                    team_id=team.team_id,
                    category_id=category_id,
                    team_code=team.team_code,
                    club_name=team.team_name,
                    category_name=category_name,
                    stat_type="assist",
                    goal_type=None,
                )
            )


def _create_admin_verification(db: Session, submission: MatchResultSubmission, verified_by_admin_id: int) -> None:
    verification = submission.verification
    if not verification:
        verification = ResultVerification(
            submission_id=submission.submission_id,
            verified_by_admin_id=verified_by_admin_id,
            verified_by_system=False,
            decision=ApprovalStatus.APPROVED.value,
        )
        db.add(verification)
    else:
        verification.verified_by_admin_id = verified_by_admin_id
        verification.verified_by_system = False
        verification.decision = ApprovalStatus.APPROVED.value
        verification.rejection_reason = None
        verification.verification_date = datetime.utcnow()


def _finalize_single_result_submission(
    db: Session,
    *,
    fixture: Fixture,
    submission: MatchResultSubmission,
    verified_by_admin_id: int,
) -> None:
    fixture_match = fixture.match
    if not fixture_match:
        fixture_match = Match(fixture_id=fixture.fixture_id, match_date=fixture.fixture_date, status="scheduled")
        db.add(fixture_match)
        db.flush()
        fixture.match = fixture_match
    fixture_match.home_score = submission.home_score
    fixture_match.away_score = submission.away_score
    fixture_match.status = "completed"
    submission.status = ApprovalStatus.APPROVED.value
    _create_admin_verification(db, submission, verified_by_admin_id)
    db.commit()
    try:
        notify_team_admins_for_teams(
            db,
            [fixture.home_team_id, fixture.away_team_id],
            "Result verified",
            f"Result for {fixture.home_team.team_name} vs {fixture.away_team.team_name} is now verified at {fixture_match.home_score}-{fixture_match.away_score}. League tables and player statistics have been updated automatically.",
            "/team-admin/dashboard#results",
        )
        notify_super_admins(
            db,
            "Result verified",
            f"Super admin approved {fixture.home_team.team_name} vs {fixture.away_team.team_name} at {fixture_match.home_score}-{fixture_match.away_score}.",
            "/super-admin#results",
        )
    except Exception:
        logger.exception("Match-result notification dispatch failed")


def _create_system_verification(db: Session, submission: MatchResultSubmission) -> None:
    verification = submission.verification
    if not verification:
        verification = ResultVerification(
            submission_id=submission.submission_id,
            verified_by_admin_id=None,
            verified_by_system=True,
            decision=ApprovalStatus.APPROVED.value,
        )
        db.add(verification)
    else:
        verification.verified_by_admin_id = None
        verification.verified_by_system = True
        verification.decision = ApprovalStatus.APPROVED.value
        verification.rejection_reason = None
        verification.verification_date = datetime.utcnow()


def _finalize_matched_result(
    db: Session,
    *,
    fixture: Fixture,
    home_submission: MatchResultSubmission,
    away_submission: MatchResultSubmission,
) -> None:
    fixture_match = fixture.match
    if not fixture_match:
        fixture_match = Match(fixture_id=fixture.fixture_id, match_date=fixture.fixture_date, status="scheduled")
        db.add(fixture_match)
        db.flush()
        fixture.match = fixture_match
    fixture_match.home_score = home_submission.home_score
    fixture_match.away_score = home_submission.away_score
    fixture_match.status = "completed"

    for submission in (home_submission, away_submission):
        submission.status = ApprovalStatus.APPROVED.value
        _create_system_verification(db, submission)
    db.commit()
    try:
        notify_team_admins_for_teams(
            db,
            [fixture.home_team_id, fixture.away_team_id],
            "Result verified",
            f"Result for {fixture.home_team.team_name} vs {fixture.away_team.team_name} is now verified at {fixture_match.home_score}-{fixture_match.away_score}. League tables and player statistics have been updated automatically.",
            "/team-admin/dashboard#results",
        )
        notify_super_admins(
            db,
            "Result verified",
            f"System verified {fixture.home_team.team_name} vs {fixture.away_team.team_name} at {fixture_match.home_score}-{fixture_match.away_score}.",
            "/super-admin#results",
        )
    except Exception:
        logger.exception("Match-result notification dispatch failed")


def submit_match_result(
    db: Session,
    *,
    team_admin_id: int,
    approved_team_ids: Iterable[int] | None = None,
    fixture_id: int,
    home_score: int,
    away_score: int,
    result_file_path: str | None = None,
    scorer_player_ids: list[str | int | None],
    goal_types: list[str | None],
    assist_player_ids: list[str | int | None],
    result_type: str = "standard",
) -> MatchResultSubmission:
    fixture = db.get(Fixture, fixture_id)
    if not fixture or fixture.home_team is None or fixture.away_team is None:
        raise RegistrationError("Fixture was not found.")
    if not _fixture_allows_result_submission(fixture):
        raise RegistrationError("Results can only be entered after the fixture has been played.")
    if home_score < 0 or away_score < 0:
        raise RegistrationError("Scores cannot be negative.")

    submitting_team, opposing_team, submitting_side = _team_admin_fixture_context(
        fixture,
        team_admin_id=team_admin_id,
        approved_team_ids=approved_team_ids,
    )
    normalized_result_type = _normalize_result_type(result_type)
    scorer_ids = _coerce_selected_player_ids(scorer_player_ids)
    assister_ids = _coerce_selected_player_ids(assist_player_ids)
    is_special_result = normalized_result_type in SPECIAL_RESULT_TYPES
    expected_goal_count = home_score if submitting_side == "home" else away_score
    if is_special_result:
        expected_goal_count = 3
    _validate_match_result_payload(
        expected_goal_count=expected_goal_count,
        scorer_player_ids=scorer_ids,
        goal_types=goal_types,
        assist_player_ids=assister_ids,
        result_type=normalized_result_type,
    )

    if is_special_result:
        expected_home_score = 3 if submitting_side == "home" else 0
        expected_away_score = 3 if submitting_side == "away" else 0
        if home_score != expected_home_score or away_score != expected_away_score:
            raise RegistrationError("Special result types must be submitted as a 3-0 scoreline for the team that showed up.")

    match = fixture.match or Match(
        fixture_id=fixture.fixture_id,
        match_date=fixture.fixture_date,
        status="scheduled",
    )
    if not fixture.match:
        db.add(match)
        db.flush()
        fixture.match = match

    submissions = db.scalars(
        select(MatchResultSubmission)
        .where(MatchResultSubmission.match_id == match.match_id)
        .order_by(MatchResultSubmission.submission_id.asc())
    ).all()
    own_submission = next((submission for submission in submissions if submission.submitted_by_team_admin_id == team_admin_id), None)
    other_submission = next((submission for submission in submissions if submission.submitted_by_team_admin_id != team_admin_id), None)

    if other_submission and (
        other_submission.home_score != home_score or other_submission.away_score != away_score
    ):
        raise RegistrationError(
            "The other team admin already submitted a different scoreline for this fixture."
        )

    existing_submission = own_submission or MatchResultSubmission(
        match_id=match.match_id,
        submitted_by_team_admin_id=team_admin_id,
        status=ApprovalStatus.PENDING.value,
    )
    if not own_submission:
        db.add(existing_submission)
        db.flush()
    else:
        _remove_submission_statistics(db, existing_submission)
        if existing_submission.verification:
            db.delete(existing_submission.verification)

    scorer_players: list[Player | None] = []
    assister_players: list[Player | None] = []
    if not is_special_result:
        selected_players = _player_lookup_map(
            db,
            [player_id for player_id in scorer_ids if player_id is not None]
            + [player_id for player_id in assister_ids if player_id is not None],
            team_admin_id=submitting_team.team_admin_id,
        )
        for index, scorer_id in enumerate(scorer_ids):
            goal_type = _clean_goal_type(goal_types[index] if index < len(goal_types) else None)
            if goal_type == "Own Goal":
                scorer_players.append(None)
                assister_players.append(None)
                continue
            scorer = selected_players.get(_selected_player_lookup_key(scorer_id))
            _assert_player_belongs_to_team(
                scorer,
                submitting_team,
                "scorers",
                fixture_category_name=fixture.category.category_name if fixture.category else None,
            )
            scorer_players.append(scorer)
            assister_id = assister_ids[index] if index < len(assister_ids) else None
            assister = selected_players.get(_selected_player_lookup_key(assister_id)) if assister_id is not None else None
            if assister is not None:
                _assert_player_belongs_to_team(
                    assister,
                    submitting_team,
                    "assisters",
                    fixture_category_name=fixture.category.category_name if fixture.category else None,
                )
            assister_players.append(assister)

        scorer_names_text, goal_types_text, assist_names_text = _serialize_submission_players(
            scorer_players,
            assister_players,
            [_clean_goal_type(goal_type) for goal_type in goal_types],
        )
        existing_submission.scorer_names_text = scorer_names_text
        existing_submission.goal_types_text = goal_types_text
        existing_submission.assist_names_text = assist_names_text
        _write_submission_statistics(
            db,
            submission=existing_submission,
            fixture=fixture,
            team=submitting_team,
            scorers=scorer_players,
            assists=assister_players,
            goal_types=[_clean_goal_type(goal_type) for goal_type in goal_types],
        )
    else:
        existing_submission.scorer_names_text = ""
        existing_submission.goal_types_text = ""
        existing_submission.assist_names_text = ""

    if not is_special_result and other_submission and other_submission.home_score == home_score and other_submission.away_score == away_score:
        _finalize_matched_result(
            db,
            fixture=fixture,
            home_submission=other_submission if submitting_side == "away" else existing_submission,
            away_submission=existing_submission if submitting_side == "away" else other_submission,
        )
        db.refresh(existing_submission)
        return existing_submission

    db.commit()
    db.refresh(existing_submission)
    try:
        if is_special_result:
            notify_team_admins_for_teams(
                db,
                [fixture.home_team_id, fixture.away_team_id],
                "Result awaiting approval",
                f"{RESULT_TYPE_LABELS.get(normalized_result_type, 'Special result')} for {fixture.home_team.team_name} vs {fixture.away_team.team_name} is waiting for super admin approval.",
                "/team-admin/dashboard#results",
            )
            notify_super_admins(
                db,
                "Result awaiting approval",
                f"{RESULT_TYPE_LABELS.get(normalized_result_type, 'Special result')} submitted for {fixture.home_team.team_name} vs {fixture.away_team.team_name}.",
                "/super-admin#results",
            )
        else:
            notify_team_admins_for_teams(
                db,
                [submitting_team.team_id],
                "Result saved",
                f"Result for {fixture.home_team.team_name} vs {fixture.away_team.team_name} was saved and is waiting for the other team admin to submit the matching scoreline.",
                "/team-admin/dashboard#results",
            )
    except Exception:
        logger.exception("Match-result notification dispatch failed")
    return existing_submission


def get_player_statistics(
    db: Session,
    *,
    team_ids: Iterable[int] | None = None,
) -> dict[str, list[dict[str, object]]]:
    query = (
        select(PlayerStatistic)
        .options(
            selectinload(PlayerStatistic.player).selectinload(Player.team).selectinload(Team.category),
            selectinload(PlayerStatistic.team).selectinload(Team.category),
            selectinload(PlayerStatistic.fixture).selectinload(Fixture.category),
        )
        .order_by(PlayerStatistic.created_at.desc(), PlayerStatistic.statistic_id.desc())
    )
    if team_ids is not None:
        query = query.where(PlayerStatistic.team_id.in_(list(team_ids)))
    statistics = db.scalars(query).all()

    player_groups: dict[tuple[str, str], dict[str, object]] = {}

    def _record_statistic(statistic: PlayerStatistic) -> None:
        player = statistic.player
        team = statistic.team
        if not player or not team:
            return
        identity_key = _player_identity_key(player)
        category_name = statistic.category_name or (team.category.category_name if team.category else "")
        group_key = (identity_key, category_name)
        group = player_groups.setdefault(
            group_key,
            {
                "players": {},
                "primary_player": player,
                "category_name": category_name,
                "goals": 0,
                "assists": 0,
                "goal_types": defaultdict(int),
                "category_totals": defaultdict(lambda: {"goals": 0, "assists": 0, "goal_types": defaultdict(int)}),
            },
        )
        group["players"][player.player_id] = player
        if player.player_id > group["primary_player"].player_id:
            group["primary_player"] = player

        category_entry = group["category_totals"][category_name]
        if statistic.stat_type == "goal":
            group["goals"] += 1
            goal_type = statistic.goal_type or "Penalty"
            group["goal_types"][goal_type] += 1
            category_entry["goals"] += 1
            category_entry["goal_types"][goal_type] += 1
        elif statistic.stat_type == "assist":
            group["assists"] += 1
            category_entry["assists"] += 1

    for statistic in statistics:
        _record_statistic(statistic)

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
            club_name = player.team.team_name if player.team else None
            normalized = (club_name or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                clubs.append(normalized)
        return clubs

    def _collect_player_names(row: dict[str, object]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for player in sorted(row["players"].values(), key=lambda item: item.player_id):
            full_name = (player.full_name or "").strip()
            if full_name and full_name not in seen:
                seen.add(full_name)
                names.append(full_name)
        return names

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
                "category_name": group["category_name"],
                "player_names": _collect_player_names(group),
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
                "player_names": row["player_names"],
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
                "player_names": row["player_names"],
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

    def _head_to_head_metrics(category_name: str, tied_team_ids: list[int]) -> dict[int, dict[str, int]]:
        stats = {
            team_id: {"points": 0, "goals_for": 0, "goals_against": 0, "goal_difference": 0}
            for team_id in tied_team_ids
        }
        tied_team_set = set(tied_team_ids)
        for match in matches:
            fixture = match.fixture
            if not fixture or not fixture.category or fixture.category.category_name != category_name:
                continue
            if not fixture.home_team or not fixture.away_team:
                continue
            home_team_id = fixture.home_team.team_id
            away_team_id = fixture.away_team.team_id
            if home_team_id not in tied_team_set or away_team_id not in tied_team_set:
                continue

            home_score = match.home_score or 0
            away_score = match.away_score or 0
            home = stats[home_team_id]
            away = stats[away_team_id]

            home["goals_for"] += home_score
            home["goals_against"] += away_score
            away["goals_for"] += away_score
            away["goals_against"] += home_score

            if home_score > away_score:
                home["points"] += 3
            elif home_score < away_score:
                away["points"] += 3
            else:
                home["points"] += 1
                away["points"] += 1

        for row in stats.values():
            row["goal_difference"] = row["goals_for"] - row["goals_against"]
        return stats
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
        ranked_rows = sorted(
            rows.values(),
            key=lambda row: (-int(row["points"]), str(row["team"].team_name).lower()),
        )
        index = 0
        while index < len(ranked_rows):
            point_total = int(ranked_rows[index]["points"])
            group_end = index + 1
            while group_end < len(ranked_rows) and int(ranked_rows[group_end]["points"]) == point_total:
                group_end += 1
            point_group = ranked_rows[index:group_end]
            if len(point_group) > 1:
                tied_team_ids = [row["team"].team_id for row in point_group]
                head_to_head = _head_to_head_metrics(category_name, tied_team_ids)
                for row in point_group:
                    metrics = head_to_head[row["team"].team_id]
                    row["head_to_head_points"] = metrics["points"]
                    row["head_to_head_goal_difference"] = metrics["goal_difference"]
                point_group.sort(
                    key=lambda row: (
                        -int(row["head_to_head_points"]),
                        -int(row["head_to_head_goal_difference"]),
                        -int(row["goal_difference"]),
                        -int(row["goals_for"]),
                        str(row["team"].team_name).lower(),
                    ),
                )
            else:
                point_group[0]["head_to_head_points"] = 0
                point_group[0]["head_to_head_goal_difference"] = 0
            ranked_rows[index:group_end] = point_group
            index = group_end
        for position, row in enumerate(ranked_rows, start=1):
            row["position"] = position
        ordered[category_name] = ranked_rows
    return ordered


get_player_performances = get_player_statistics
