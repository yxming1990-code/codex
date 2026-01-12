"""
Interactive TUI application using Textual.

Provides:
- Market list view with status
- Active markets view with prices and orders
- Watch list view
- Controls for activation, pausing, and cancellation
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Any, Callable

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, Static, Button, DataTable, Label,
    Input, Switch, TabbedContent, TabPane, ProgressBar
)
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive

from ..config import get_settings, MarketState, PricingMode
from ..utils import (
    get_logger,
    PredictMarket,
    ExternalQuote,
    PredictOrder,
    Position,
)

logger = get_logger(__name__)


class MarketRow:
    """Data for a market table row."""

    def __init__(
        self,
        market_id: str,
        title: str,
        state: MarketState,
        external_source: str = "",
        yes_bid: Optional[Decimal] = None,
        yes_ask: Optional[Decimal] = None,
        no_bid: Optional[Decimal] = None,
        no_ask: Optional[Decimal] = None,
        new_shares: Decimal = Decimal("0"),
        max_shares: int = 1000,
        open_orders: int = 0,
        yes_position: Decimal = Decimal("0"),
        no_position: Decimal = Decimal("0"),
    ):
        self.market_id = market_id
        self.title = title
        self.state = state
        self.external_source = external_source
        self.yes_bid = yes_bid
        self.yes_ask = yes_ask
        self.no_bid = no_bid
        self.no_ask = no_ask
        self.new_shares = new_shares
        self.max_shares = max_shares
        self.open_orders = open_orders
        self.yes_position = yes_position
        self.no_position = no_position

    def to_row(self) -> tuple:
        """Convert to table row tuple."""
        def fmt_price(p: Optional[Decimal]) -> str:
            return f"{p:.2f}" if p is not None else "-"

        state_emoji = {
            MarketState.ACTIVE: "[green]RUN[/]",
            MarketState.WATCH: "[yellow]WATCH[/]",
            MarketState.DISABLED: "[red]OFF[/]",
        }

        return (
            self.market_id[:8],
            self.title[:40],
            state_emoji.get(self.state, self.state.value),
            self.external_source[:4].upper(),
            fmt_price(self.yes_bid),
            fmt_price(self.yes_ask),
            fmt_price(self.no_bid),
            fmt_price(self.no_ask),
            f"{self.new_shares}/{self.max_shares}",
            str(self.open_orders),
            f"{self.yes_position:.0f}",
            f"{self.no_position:.0f}",
        )


class StatusBar(Static):
    """Status bar showing system state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active_count = 0
        self._watch_count = 0
        self._total_orders = 0
        self._last_update = None

    def update_status(
        self,
        active: int = 0,
        watch: int = 0,
        orders: int = 0,
        running: bool = False
    ) -> None:
        """Update status display."""
        self._active_count = active
        self._watch_count = watch
        self._total_orders = orders
        self._last_update = datetime.now()

        status_text = (
            f"[bold]Active:[/] {active} | "
            f"[yellow]Watch:[/] {watch} | "
            f"[blue]Orders:[/] {orders} | "
            f"[{'green' if running else 'red'}]{'RUNNING' if running else 'STOPPED'}[/]"
        )
        self.update(status_text)


class MarketTable(DataTable):
    """Market data table with selection."""

    COLUMNS = [
        ("ID", 8),
        ("Title", 40),
        ("State", 7),
        ("Src", 5),
        ("Y.Bid", 6),
        ("Y.Ask", 6),
        ("N.Bid", 6),
        ("N.Ask", 6),
        ("Shares", 12),
        ("Orders", 6),
        ("Y.Pos", 6),
        ("N.Pos", 6),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rows: Dict[str, MarketRow] = {}

    def on_mount(self) -> None:
        """Set up table columns."""
        for name, width in self.COLUMNS:
            self.add_column(name, width=width)

    def update_market(self, row: MarketRow) -> None:
        """Update or add a market row."""
        self._rows[row.market_id] = row
        self._refresh_table()

    def remove_market(self, market_id: str) -> None:
        """Remove a market row."""
        if market_id in self._rows:
            del self._rows[market_id]
            self._refresh_table()

    def _refresh_table(self) -> None:
        """Refresh table contents."""
        self.clear()
        for row in sorted(self._rows.values(), key=lambda r: r.market_id):
            self.add_row(*row.to_row(), key=row.market_id)

    def get_selected_market_id(self) -> Optional[str]:
        """Get currently selected market ID."""
        if self.cursor_row is not None and self.row_count > 0:
            row_key = self.get_row_at(self.cursor_row)
            if row_key:
                return str(row_key)
        return None


class AlertPanel(ScrollableContainer):
    """Panel showing recent alerts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alerts: List[Dict] = []

    def add_alert(self, market_id: str, event: str, reason: str) -> None:
        """Add an alert to the panel."""
        now = datetime.now().strftime("%H:%M:%S")
        alert = {
            "time": now,
            "market_id": market_id,
            "event": event,
            "reason": reason
        }
        self._alerts.insert(0, alert)
        self._alerts = self._alerts[:50]  # Keep last 50
        self._refresh()

    def _refresh(self) -> None:
        """Refresh alert display."""
        self.query("Label").remove()
        for alert in self._alerts[:20]:
            color = "red" if "jump" in alert["event"].lower() else "yellow"
            text = f"[{color}]{alert['time']}[/] [{alert['market_id'][:8]}] {alert['reason'][:60]}"
            self.mount(Label(text))


class ControlPanel(Container):
    """Control panel with action buttons."""

    class ActionRequested(Message):
        """Message for action requests."""

        def __init__(self, action: str, market_id: Optional[str] = None):
            super().__init__()
            self.action = action
            self.market_id = market_id

    def compose(self) -> ComposeResult:
        with Horizontal(id="global-controls"):
            yield Button("Start All", id="btn-start-all", variant="success")
            yield Button("Pause All", id="btn-pause-all", variant="warning")
            yield Button("Cancel All", id="btn-cancel-all", variant="error")
            yield Button("Exit", id="btn-exit", variant="default")

        with Horizontal(id="market-controls"):
            yield Button("Activate", id="btn-activate", variant="success")
            yield Button("Pause", id="btn-pause", variant="warning")
            yield Button("Cancel Orders", id="btn-cancel", variant="error")
            yield Button("Validate", id="btn-validate", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        action_map = {
            "btn-start-all": "start_all",
            "btn-pause-all": "pause_all",
            "btn-cancel-all": "cancel_all",
            "btn-exit": "exit",
            "btn-activate": "activate",
            "btn-pause": "pause",
            "btn-cancel": "cancel",
            "btn-validate": "validate",
        }

        if button_id in action_map:
            self.post_message(self.ActionRequested(action_map[button_id]))


class MarketMakerApp(App):
    """
    Main TUI application for the market maker.
    """

    CSS = """
    Screen {
        layout: grid;
        grid-size: 1;
        grid-rows: auto 1fr auto auto;
    }

    #status-bar {
        height: 1;
        background: $surface;
        padding: 0 1;
    }

    #main-content {
        height: 100%;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        height: 100%;
    }

    DataTable {
        height: 100%;
    }

    #alert-panel {
        height: 8;
        border: solid $primary;
    }

    #control-panel {
        height: 3;
        background: $surface;
        padding: 0 1;
    }

    #global-controls {
        dock: left;
        width: 50%;
    }

    #market-controls {
        dock: right;
        width: 50%;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "start_all", "Start All"),
        Binding("p", "pause_all", "Pause All"),
        Binding("c", "cancel_all", "Cancel All"),
        Binding("a", "activate_selected", "Activate"),
        Binding("w", "pause_selected", "Pause"),
        Binding("r", "refresh", "Refresh"),
    ]

    # Reactive properties
    is_running = reactive(False)

    def __init__(
        self,
        on_action: Optional[Callable[[str, Optional[str]], None]] = None,
        *args, **kwargs
    ):
        """
        Initialize application.

        Args:
            on_action: Callback for actions (action_name, market_id)
        """
        super().__init__(*args, **kwargs)
        self._on_action = on_action

    def compose(self) -> ComposeResult:
        yield Header()

        yield StatusBar(id="status-bar")

        with Container(id="main-content"):
            with TabbedContent():
                with TabPane("Active Markets", id="tab-active"):
                    yield MarketTable(id="active-table")

                with TabPane("Watch List", id="tab-watch"):
                    yield MarketTable(id="watch-table")

                with TabPane("All Markets", id="tab-all"):
                    yield MarketTable(id="all-table")

        yield AlertPanel(id="alert-panel")

        yield ControlPanel(id="control-panel")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize on mount."""
        self.title = "Predict Market Maker"
        self.sub_title = "Conservative Market Making"

    def _trigger_action(self, action: str, market_id: Optional[str] = None) -> None:
        """Trigger an action callback."""
        if self._on_action:
            self._on_action(action, market_id)

    def on_control_panel_action_requested(
        self,
        message: ControlPanel.ActionRequested
    ) -> None:
        """Handle control panel action requests."""
        market_id = None

        # Get selected market for market-specific actions
        if message.action in ("activate", "pause", "cancel", "validate"):
            active_table = self.query_one("#active-table", MarketTable)
            market_id = active_table.get_selected_market_id()
            if not market_id:
                watch_table = self.query_one("#watch-table", MarketTable)
                market_id = watch_table.get_selected_market_id()

        self._trigger_action(message.action, market_id)

    # Action handlers
    def action_quit(self) -> None:
        """Quit application."""
        self._trigger_action("exit")
        self.exit()

    def action_start_all(self) -> None:
        """Start all markets."""
        self._trigger_action("start_all")

    def action_pause_all(self) -> None:
        """Pause all markets."""
        self._trigger_action("pause_all")

    def action_cancel_all(self) -> None:
        """Cancel all orders."""
        self._trigger_action("cancel_all")

    def action_activate_selected(self) -> None:
        """Activate selected market."""
        active_table = self.query_one("#active-table", MarketTable)
        market_id = active_table.get_selected_market_id()
        if market_id:
            self._trigger_action("activate", market_id)

    def action_pause_selected(self) -> None:
        """Pause selected market."""
        active_table = self.query_one("#active-table", MarketTable)
        market_id = active_table.get_selected_market_id()
        if market_id:
            self._trigger_action("pause", market_id)

    def action_refresh(self) -> None:
        """Refresh display."""
        self._trigger_action("refresh")

    # Public update methods

    def update_market(self, row: MarketRow) -> None:
        """Update market in appropriate table."""
        if row.state == MarketState.ACTIVE:
            table = self.query_one("#active-table", MarketTable)
        elif row.state == MarketState.WATCH:
            table = self.query_one("#watch-table", MarketTable)
        else:
            table = self.query_one("#all-table", MarketTable)

        table.update_market(row)

        # Also update in all-markets table
        all_table = self.query_one("#all-table", MarketTable)
        all_table.update_market(row)

    def remove_market(self, market_id: str) -> None:
        """Remove market from tables."""
        for table_id in ("#active-table", "#watch-table", "#all-table"):
            table = self.query_one(table_id, MarketTable)
            table.remove_market(market_id)

    def update_status(
        self,
        active: int = 0,
        watch: int = 0,
        orders: int = 0,
        running: bool = False
    ) -> None:
        """Update status bar."""
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update_status(active, watch, orders, running)
        self.is_running = running

    def add_alert(self, market_id: str, event: str, reason: str) -> None:
        """Add an alert."""
        alert_panel = self.query_one("#alert-panel", AlertPanel)
        alert_panel.add_alert(market_id, event, reason)

    def set_running(self, running: bool) -> None:
        """Set running state."""
        self.is_running = running
