from sqlalchemy.orm import Session

from warehouse_portal.services import OrchestratorAgent


def seed_demo(db: Session) -> list[str]:
    manager = OrchestratorAgent(db)
    accounts = manager.discover("mock")
    for account in accounts:
        if account.research_status != "completed":
            manager.research(account.id, "mock")
        if account.pipeline_stage not in {"needs_review", "qualified", "disqualified"}:
            manager.score(account.id)
    return [account.id for account in accounts]
