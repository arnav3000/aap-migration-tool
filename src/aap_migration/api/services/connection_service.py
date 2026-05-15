"""Connection CRUD and AAP client factory."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aap_migration.api.crypto import decrypt_token, encrypt_token
from aap_migration.api.models import Connection
from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig


class ConnectionService:
    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        url: str,
        token: str | None = None,
        type: str = "awx",
        role: str = "source",
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> Connection:
        conn = Connection(
            name=name,
            url=url,
            token=encrypt_token(token or ""),
            type=type,
            role=role,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        db.add(conn)
        db.flush()
        return conn

    @staticmethod
    def list_all(db: Session) -> list[Connection]:
        result: list[Connection] = db.query(Connection).order_by(Connection.created_at).all()
        return result

    @staticmethod
    def get(db: Session, conn_id: str) -> Connection | None:
        result: Connection | None = db.query(Connection).filter(Connection.id == conn_id).first()
        return result

    @staticmethod
    def update(db: Session, conn_id: str, **kwargs: object) -> Connection | None:
        conn: Connection | None = db.query(Connection).filter(Connection.id == conn_id).first()
        if conn is None:
            return None
        for k, v in kwargs.items():
            if v is not None and hasattr(conn, k):
                if k == "token" and isinstance(v, str):
                    v = encrypt_token(v)
                setattr(conn, k, v)
        db.flush()
        return conn

    @staticmethod
    def delete(db: Session, conn_id: str) -> bool:
        conn: Connection | None = db.query(Connection).filter(Connection.id == conn_id).first()
        if conn is None:
            return False
        db.delete(conn)
        db.flush()
        return True

    @staticmethod
    def build_instance_config(conn: Connection) -> AAPInstanceConfig:
        return AAPInstanceConfig(
            url=conn.url,
            token=decrypt_token(conn.token),
            verify_ssl=conn.verify_ssl,
            timeout=conn.timeout,
        )

    @staticmethod
    def build_source_client(conn: Connection) -> AAPSourceClient:
        config = ConnectionService.build_instance_config(conn)
        return AAPSourceClient(config)

    @staticmethod
    def build_target_client(conn: Connection) -> AAPTargetClient:
        config = ConnectionService.build_instance_config(conn)
        return AAPTargetClient(config)

    @staticmethod
    async def test_connection(conn: Connection) -> tuple[bool, str | None]:
        """Test connectivity to an AAP instance. Returns (ok, error_message)."""
        try:
            config = ConnectionService.build_instance_config(conn)
            if conn.role in ("target", "destination"):
                target = AAPTargetClient(config)
                async with target:
                    await target.get_version()
            else:
                source = AAPSourceClient(config)
                async with source:
                    await source.get_version()
            return True, None
        except Exception as exc:
            return False, str(exc)
