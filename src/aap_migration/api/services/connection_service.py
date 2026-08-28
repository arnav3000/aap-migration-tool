"""Connection CRUD and AAP client factory."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aap_migration.api.crypto import decrypt_token, encrypt_token
from aap_migration.api.models import Connection
from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig


class ConnectionService:
    """CRUD for Connection plus helpers to build AAP clients and test connectivity."""

    @staticmethod
    def _auth_scheme(token: str) -> str:
        # AWX uses Token, gateway uses Bearer — we try both via Bearer primarily
        # Client will use Bearer; string here for logging only
        return "Bearer" if token else "Token"

    @staticmethod
    def create(
        session: Session,
        *,
        name: str,
        url: str,
        token: str,
        role: str,
        verify_ssl: bool,
        timeout: int,
    ) -> Connection:
        try:
            encrypted = encrypt_token(token)
        except RuntimeError:
            # No encryption key configured — store plaintext (dev simple mode)
            encrypted = token
        conn = Connection(
            name=name,
            url=url.rstrip("/"),
            token=encrypted,
            role=role,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        session.add(conn)
        session.flush()
        return conn

    @staticmethod
    def list_all(session: Session) -> list[Connection]:
        return session.query(Connection).order_by(Connection.created_at).all()

    @staticmethod
    def get(session: Session, conn_id: str) -> Connection | None:
        return session.query(Connection).filter(Connection.id == conn_id).first()

    @staticmethod
    def update(session: Session, conn: Connection, **kwargs) -> Connection:
        for k, v in kwargs.items():
            if v is None:
                continue
            if k == "token":
                try:
                    v = encrypt_token(v)  # type: ignore[assignment]
                except RuntimeError:
                    v = v  # plaintext fallback
            if k == "url" and isinstance(v, str):
                v = v.rstrip("/")
            if hasattr(conn, k):
                setattr(conn, k, v)
        session.flush()
        return conn

    @staticmethod
    def delete(session: Session, conn: Connection) -> None:
        session.delete(conn)
        session.flush()

    @staticmethod
    def build_instance_config(
        conn: Connection, token_override: str | None = None
    ) -> AAPInstanceConfig:
        token = token_override if token_override is not None else decrypt_token(conn.token)
        return AAPInstanceConfig(
            url=conn.url, token=token, verify_ssl=bool(conn.verify_ssl), timeout=int(conn.timeout)
        )

    @staticmethod
    def build_source_client(conn: Connection) -> AAPSourceClient:
        cfg = ConnectionService.build_instance_config(conn)
        return AAPSourceClient(config=cfg)

    @staticmethod
    def build_target_client(conn: Connection) -> AAPTargetClient:
        cfg = ConnectionService.build_instance_config(conn)
        return AAPTargetClient(config=cfg)

    @staticmethod
    async def test_connection(conn: Connection) -> tuple[str, str, str | None]:
        """Test connectivity AND authentication to an AAP instance.

        Uses /me/ which requires valid auth, unlike /ping/ which is public.
        Returns (ping_status, auth_status, error).
        """
        # We need to hit /api/v2/me or /api/controller/v2/me depending on URL.
        # Easiest: use configured client and call get("me/") + get("ping/").
        cfg = ConnectionService.build_instance_config(conn)
        # Detect role to choose client
        client = (
            AAPSourceClient(config=cfg) if conn.role == "source" else AAPTargetClient(config=cfg)
        )
        ping_status = "unknown"
        auth_status = "unknown"
        error: str | None = None
        try:
            # ping first
            try:
                await client.get("ping/")
                ping_status = "ok"
            except Exception as e:
                ping_status = "failed"
                error = str(e)[:500]

            # auth check via me/
            try:
                await client.get("me/")
                auth_status = "ok"
            except Exception as e:
                auth_status = "failed"
                msg = str(e)[:500]
                error = f"{error}; auth: {msg}" if error else msg
        finally:
            try:
                await client.close()
            except Exception:
                pass

        # Update stored statuses best-effort (caller will commit)
        conn.ping_status = ping_status
        conn.auth_status = auth_status
        return ping_status, auth_status, error
