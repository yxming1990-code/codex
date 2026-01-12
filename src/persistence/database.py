"""
SQLite persistence module for market maker state.

Handles:
- Market selection and state persistence
- Strategy parameter overrides
- New shares tracking baselines
- Alert and operation logs
"""

import json
import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Any
from pathlib import Path

import aiosqlite

from ..config import get_settings, MarketState, PricingMode
from ..utils import get_logger

logger = get_logger(__name__)


class Database:
    """
    Async SQLite database for market maker persistence.

    Tables:
    - markets: Selected markets and their states
    - strategy_overrides: Per-market strategy parameters
    - alerts: Risk alert history
    - operations: Operation log (orders, merges, etc.)
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize database.

        Args:
            db_path: Path to SQLite database file
        """
        settings = get_settings()
        self._db_path = db_path or settings.database_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Connect to database and create tables."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"Connected to database: {self._db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                condition_id TEXT,
                title TEXT,
                external_source TEXT,
                external_id TEXT,
                state TEXT NOT NULL DEFAULT 'watch',
                baseline_yes_amount TEXT DEFAULT '0',
                baseline_no_amount TEXT DEFAULT '0',
                baseline_set_at TEXT,
                new_shares_count TEXT DEFAULT '0',
                watch_reason TEXT,
                watch_entered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_overrides (
                market_id TEXT PRIMARY KEY,
                pricing_mode TEXT,
                offset TEXT,
                order_size TEXT,
                max_new_shares INTEGER,
                auto_pricing_enabled INTEGER,
                merge_enabled INTEGER,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (market_id) REFERENCES markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT,
                data TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (market_id) REFERENCES markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                market_id TEXT,
                details TEXT,
                success INTEGER,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_market ON alerts(market_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
            CREATE INDEX IF NOT EXISTS idx_operations_created ON operations(created_at);
        """)
        await self._conn.commit()

    # ========== Market Operations ==========

    async def save_market(
        self,
        market_id: str,
        condition_id: str,
        title: str,
        external_source: Optional[str] = None,
        external_id: Optional[str] = None,
        state: MarketState = MarketState.WATCH
    ) -> None:
        """
        Save or update a market.

        Args:
            market_id: Market ID
            condition_id: Condition ID
            title: Market title
            external_source: External source (polymarket/kalshi)
            external_id: External market ID
            state: Market state
        """
        now = datetime.utcnow().isoformat()

        await self._conn.execute("""
            INSERT INTO markets (
                market_id, condition_id, title, external_source, external_id,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                condition_id = excluded.condition_id,
                title = excluded.title,
                external_source = excluded.external_source,
                external_id = excluded.external_id,
                state = excluded.state,
                updated_at = excluded.updated_at
        """, (market_id, condition_id, title, external_source, external_id,
              state.value, now, now))
        await self._conn.commit()

    async def update_market_state(
        self,
        market_id: str,
        state: MarketState,
        watch_reason: Optional[str] = None
    ) -> None:
        """Update market state."""
        now = datetime.utcnow().isoformat()

        if state == MarketState.WATCH and watch_reason:
            await self._conn.execute("""
                UPDATE markets
                SET state = ?, watch_reason = ?, watch_entered_at = ?, updated_at = ?
                WHERE market_id = ?
            """, (state.value, watch_reason, now, now, market_id))
        else:
            await self._conn.execute("""
                UPDATE markets
                SET state = ?, updated_at = ?
                WHERE market_id = ?
            """, (state.value, now, market_id))

        await self._conn.commit()

    async def update_market_baseline(
        self,
        market_id: str,
        yes_amount: Decimal,
        no_amount: Decimal
    ) -> None:
        """Update market baseline position."""
        now = datetime.utcnow().isoformat()

        await self._conn.execute("""
            UPDATE markets
            SET baseline_yes_amount = ?, baseline_no_amount = ?,
                baseline_set_at = ?, new_shares_count = '0', updated_at = ?
            WHERE market_id = ?
        """, (str(yes_amount), str(no_amount), now, now, market_id))
        await self._conn.commit()

    async def update_new_shares(self, market_id: str, count: Decimal) -> None:
        """Update new shares count."""
        now = datetime.utcnow().isoformat()

        await self._conn.execute("""
            UPDATE markets
            SET new_shares_count = ?, updated_at = ?
            WHERE market_id = ?
        """, (str(count), now, market_id))
        await self._conn.commit()

    async def get_market(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get market by ID."""
        async with self._conn.execute(
            "SELECT * FROM markets WHERE market_id = ?",
            (market_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None

    async def get_all_markets(self) -> List[Dict[str, Any]]:
        """Get all markets."""
        async with self._conn.execute("SELECT * FROM markets") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_markets_by_state(self, state: MarketState) -> List[Dict[str, Any]]:
        """Get markets by state."""
        async with self._conn.execute(
            "SELECT * FROM markets WHERE state = ?",
            (state.value,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_market(self, market_id: str) -> None:
        """Delete a market."""
        await self._conn.execute(
            "DELETE FROM markets WHERE market_id = ?",
            (market_id,)
        )
        await self._conn.execute(
            "DELETE FROM strategy_overrides WHERE market_id = ?",
            (market_id,)
        )
        await self._conn.commit()

    # ========== Strategy Override Operations ==========

    async def save_strategy_override(
        self,
        market_id: str,
        **kwargs
    ) -> None:
        """
        Save strategy overrides for a market.

        Args:
            market_id: Market ID
            **kwargs: Override parameters
        """
        now = datetime.utcnow().isoformat()

        # Build update fields
        fields = ["market_id", "updated_at"]
        values = [market_id, now]
        update_parts = ["updated_at = excluded.updated_at"]

        for key in ["pricing_mode", "offset", "order_size", "max_new_shares",
                    "auto_pricing_enabled", "merge_enabled"]:
            if key in kwargs:
                fields.append(key)
                value = kwargs[key]
                if isinstance(value, (Decimal, float)):
                    value = str(value)
                elif isinstance(value, bool):
                    value = 1 if value else 0
                elif hasattr(value, 'value'):
                    value = value.value
                values.append(value)
                update_parts.append(f"{key} = excluded.{key}")

        placeholders = ", ".join(["?"] * len(values))
        field_names = ", ".join(fields)
        update_clause = ", ".join(update_parts)

        await self._conn.execute(f"""
            INSERT INTO strategy_overrides ({field_names})
            VALUES ({placeholders})
            ON CONFLICT(market_id) DO UPDATE SET {update_clause}
        """, values)
        await self._conn.commit()

    async def get_strategy_override(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get strategy override for a market."""
        async with self._conn.execute(
            "SELECT * FROM strategy_overrides WHERE market_id = ?",
            (market_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None

    async def delete_strategy_override(self, market_id: str) -> None:
        """Delete strategy override for a market."""
        await self._conn.execute(
            "DELETE FROM strategy_overrides WHERE market_id = ?",
            (market_id,)
        )
        await self._conn.commit()

    # ========== Alert Operations ==========

    async def save_alert(
        self,
        market_id: str,
        event_type: str,
        reason: str,
        data: Optional[Dict] = None
    ) -> int:
        """
        Save an alert.

        Returns:
            Alert ID
        """
        now = datetime.utcnow().isoformat()
        data_json = json.dumps(data) if data else None

        cursor = await self._conn.execute("""
            INSERT INTO alerts (market_id, event_type, reason, data, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (market_id, event_type, reason, data_json, now))
        await self._conn.commit()

        return cursor.lastrowid

    async def get_recent_alerts(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent alerts."""
        async with self._conn.execute("""
            SELECT * FROM alerts
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            alerts = []
            for row in rows:
                alert = dict(row)
                if alert.get("data"):
                    alert["data"] = json.loads(alert["data"])
                alerts.append(alert)
            return alerts

    async def get_market_alerts(
        self,
        market_id: str,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get alerts for a specific market."""
        async with self._conn.execute("""
            SELECT * FROM alerts
            WHERE market_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (market_id, limit)) as cursor:
            rows = await cursor.fetchall()
            alerts = []
            for row in rows:
                alert = dict(row)
                if alert.get("data"):
                    alert["data"] = json.loads(alert["data"])
                alerts.append(alert)
            return alerts

    # ========== Operation Log ==========

    async def log_operation(
        self,
        operation_type: str,
        market_id: Optional[str] = None,
        details: Optional[Dict] = None,
        success: bool = True,
        error: Optional[str] = None
    ) -> int:
        """
        Log an operation.

        Returns:
            Operation ID
        """
        now = datetime.utcnow().isoformat()
        details_json = json.dumps(details) if details else None

        cursor = await self._conn.execute("""
            INSERT INTO operations (operation_type, market_id, details, success, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (operation_type, market_id, details_json, 1 if success else 0, error, now))
        await self._conn.commit()

        return cursor.lastrowid

    async def get_recent_operations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent operations."""
        async with self._conn.execute("""
            SELECT * FROM operations
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            operations = []
            for row in rows:
                op = dict(row)
                if op.get("details"):
                    op["details"] = json.loads(op["details"])
                operations.append(op)
            return operations

    # ========== Cleanup ==========

    async def cleanup_old_data(self, days: int = 30) -> None:
        """Clean up old alerts and operations."""
        cutoff = datetime.utcnow().isoformat()[:10]  # Use date part

        await self._conn.execute("""
            DELETE FROM alerts
            WHERE date(created_at) < date(?, '-' || ? || ' days')
        """, (cutoff, days))

        await self._conn.execute("""
            DELETE FROM operations
            WHERE date(created_at) < date(?, '-' || ? || ' days')
        """, (cutoff, days))

        await self._conn.commit()
        logger.info(f"Cleaned up data older than {days} days")
