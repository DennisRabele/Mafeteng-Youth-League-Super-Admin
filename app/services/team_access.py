from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import ApprovalStatus, Team, TeamAdmin


def _backfill_missing_team_codes(db: Session, teams: list[Team]) -> None:
    missing_codes = [
        team
        for team in teams
        if team.status == ApprovalStatus.APPROVED.value and not team.team_code
    ]
    if not missing_codes:
        return

    from app.services.registration import generate_team_code

    for team in sorted(missing_codes, key=lambda item: item.team_id):
        team.team_code = generate_team_code(db, team)
        db.flush()

    db.commit()


def load_team_admin_teams(db: Session, team_admin_id: int) -> list[Team]:
    teams_by_id: dict[int, Team] = {}

    owned_teams = db.scalars(
        select(Team)
        .options(selectinload(Team.category))
        .where(Team.team_admin_id == team_admin_id)
        .order_by(Team.team_id.desc())
    ).all()
    for team in owned_teams:
        teams_by_id[team.team_id] = team

    team_admin = db.get(TeamAdmin, team_admin_id)
    if team_admin and team_admin.team_id:
        assigned_team = db.scalar(
            select(Team)
            .options(selectinload(Team.category))
            .where(Team.team_id == team_admin.team_id)
        )
        if assigned_team:
            teams_by_id[assigned_team.team_id] = assigned_team

    teams = list(teams_by_id.values())
    _backfill_missing_team_codes(db, teams)
    return teams


def load_team_admin_approved_teams(db: Session, team_admin_id: int) -> list[Team]:
    return [
        team
        for team in load_team_admin_teams(db, team_admin_id)
        if team.status == ApprovalStatus.APPROVED.value
    ]


def load_team_admin_approved_team_ids(db: Session, team_admin_id: int) -> list[int]:
    return [team.team_id for team in load_team_admin_approved_teams(db, team_admin_id)]


def load_team_admin_owned_approved_teams(db: Session, team_admin_id: int) -> list[Team]:
    return [
        team
        for team in load_team_admin_teams(db, team_admin_id)
        if team.status == ApprovalStatus.APPROVED.value and team.team_admin_id == team_admin_id
    ]


def load_team_admin_owned_approved_team_ids(db: Session, team_admin_id: int) -> list[int]:
    return [team.team_id for team in load_team_admin_owned_approved_teams(db, team_admin_id)]


def team_admin_has_access_to_team(db: Session, team_admin_id: int, team_id: int | None) -> bool:
    if not team_id:
        return False
    return team_id in set(load_team_admin_approved_team_ids(db, team_admin_id))


def load_team_admin_primary_team(db: Session, team_admin_id: int) -> Team | None:
    approved_teams = load_team_admin_approved_teams(db, team_admin_id)
    return approved_teams[0] if approved_teams else None
