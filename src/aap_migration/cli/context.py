"""
CLI context manager for AAP Bridge.

This module provides the context object that is passed to all CLI commands,
containing configuration, clients, and state management.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import MigrationConfig, load_config_from_yaml
from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MigrationContext:
    """
    Context object for CLI commands.

    This object holds configuration, clients, and state that is shared
    across CLI commands. It is passed via Click's context mechanism.

    Attributes:
        config_path: Path to configuration file
        log_level: Logging level
        log_file: Optional log file path
        config: Loaded migration configuration
        source_client: Client for source AAP instance
        target_client: Client for target AAP instance
        migration_state: State tracker for migration
    """

    config_path: Path | None = None
    log_level: str = "INFO"
    log_file: Path | None = None

    # Lazy-loaded attributes
    _config: MigrationConfig | None = field(default=None, init=False, repr=False)
    _source_client: AAPSourceClient | None = field(default=None, init=False, repr=False)
    _target_client: AAPTargetClient | None = field(default=None, init=False, repr=False)
    _migration_state: MigrationState | None = field(default=None, init=False, repr=False)

    @property
    def config(self) -> MigrationConfig:
        """Get or load migration configuration."""
        if self._config is None:
            if self.config_path is None:
                raise ValueError(
                    "Configuration file path not provided. "
                    "Use --config option or set AAP_MIGRATE_CONFIG environment variable."
                )

            logger.debug("Loading configuration", config_path=str(self.config_path))
            self._config = load_config_from_yaml(self.config_path)
            logger.debug("Configuration loaded successfully")

        return self._config

    @property
    def source_client(self) -> AAPSourceClient:
        """Get or create source AAP client."""
        if self._source_client is None:
            logger.debug("Creating source client", url=self.config.source.url)
            self._source_client = AAPSourceClient(
                config=self.config.source,
                rate_limit=self.config.performance.rate_limit,
                log_payloads=self.config.logging.log_payloads,
                max_payload_size=self.config.logging.max_payload_size,
                max_connections=self.config.performance.http_max_connections,
                max_keepalive_connections=self.config.performance.http_max_keepalive_connections,
            )
            logger.debug("Source client created")

        return self._source_client

    @property
    def target_client(self) -> AAPTargetClient:
        """Get or create target AAP client."""
        if self._target_client is None:
            logger.debug("Creating target client", url=self.config.target.url)
            self._target_client = AAPTargetClient(
                config=self.config.target,
                rate_limit=self.config.performance.rate_limit,
                log_payloads=self.config.logging.log_payloads,
                max_payload_size=self.config.logging.max_payload_size,
                max_connections=self.config.performance.http_max_connections,
                max_keepalive_connections=self.config.performance.http_max_keepalive_connections,
            )
            logger.debug("Target client created")

        return self._target_client

    @property
    def migration_state(self) -> MigrationState:
        """Get or create migration state tracker."""
        if self._migration_state is None:
            logger.debug(
                "Initializing migration state",
                db_path=str(self.config.state.db_path),
            )
            self._migration_state = MigrationState(
                config=self.config.state,
            )
            logger.debug("Migration state initialized")

        return self._migration_state

    def replace_config(self, config: MigrationConfig) -> None:
        """Explicit injection seam for pre-built configs (P2 #10).

        The API layer builds an isolated job config per background job and
        installs it here instead of assigning the private ``_config``
        attribute, so a CLI-side rename of privates cannot silently break
        every job. Invalidates lazily-created clients and state, which
        were derived from the previous config.
        """
        self._config = config
        self._source_client = None
        self._target_client = None
        self._migration_state = None

    def close_clients(self) -> None:
        """Best-effort close of created HTTP clients (never raises; P2 #10).

        Closes the source/target clients whether their close method is
        sync or async. Async closes run to completion on the owning loop
        when possible, otherwise on a helper thread with a 10s bound --
        never fire-and-forget leaked.
        """
        import asyncio
        import threading

        for attr in ("_source_client", "_target_client"):
            client = getattr(self, attr, None)
            if client is None:
                continue
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.run(result)
                    else:
                        done = threading.Event()
                        errors: list[BaseException] = []

                        def _runner(
                            _result: Any = result,
                            _done: threading.Event = done,
                            _errors: list[BaseException] = errors,
                        ) -> None:
                            try:
                                asyncio.run(_result)
                            except BaseException as exc:  # noqa: BLE001 - teardown
                                _errors.append(exc)
                            finally:
                                _done.set()

                        thread = threading.Thread(target=_runner, daemon=True)
                        thread.start()
                        closed = done.wait(timeout=10)
                        if not closed:
                            logger.debug("async client close timed out for %s", attr)
                        thread.join(timeout=5)
                setattr(self, attr, None)
            except Exception:
                pass

    def cleanup(self) -> None:
        """Clean up resources."""
        logger.debug("Cleaning up context resources")

        # Close clients if created
        if self._source_client is not None:
            logger.debug("Closing source client")
            # Add cleanup if needed

        if self._target_client is not None:
            logger.debug("Closing target client")
            # Add cleanup if needed

        # Migration state cleanup handled by context manager
        logger.debug("Context cleanup complete")

    def __enter__(self) -> "MigrationContext":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.cleanup()
