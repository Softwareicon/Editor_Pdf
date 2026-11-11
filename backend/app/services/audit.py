from sqlalchemy.orm import Session

from ..models import AuditLog


def record(db: Session, action: str, user_id: str | None = None, document_id: str | None = None,
           ip: str | None = None) -> None:
    """Queue an audit entry; it is committed with the caller's transaction."""
    db.add(AuditLog(action=action, user_id=user_id, document_id=document_id, ip_address=ip))
