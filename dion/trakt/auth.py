from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from rich.panel import Panel

from ..console import console
from ..platforms import prepare_config_directory
from .client import TraktClient
from .models import TraktToken

logger = logging.getLogger(__name__)


class TraktAuthManager:
    """Manages persistent Trakt OAuth credentials and Device Code authorization."""

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        client: Optional[TraktClient] = None,
    ):
        if config_dir is None:
            config_dir = prepare_config_directory()
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file = self.config_dir / "trakt_auth.json"
        self.client = client or TraktClient()
        self._token: Optional[TraktToken] = self._load_token()

    def _load_token(self) -> Optional[TraktToken]:
        if not self.auth_file.exists():
            return None
        try:
            with open(self.auth_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return TraktToken.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load Trakt credentials: %s", e)
            return None

    def _save_token(self, token: TraktToken) -> None:
        try:
            with open(self.auth_file, "w", encoding="utf-8") as f:
                json.dump(token.to_dict(), f, indent=2)
            # Secure file permissions on POSIX systems
            if sys.platform != "win32":
                try:
                    os.chmod(self.auth_file, 0o600)
                except Exception:
                    pass
            self._token = token
        except Exception as e:
            logger.error("Failed to save Trakt credentials: %s", e)

    def is_authenticated(self) -> bool:
        """Check if user has valid or refreshable Trakt credentials."""
        return self.get_valid_token() is not None

    def get_username(self) -> Optional[str]:
        """Return the authorized Trakt username if available."""
        if self._token and self._token.username:
            return self._token.username
        token_str = self.get_valid_token()
        if token_str:
            profile = self.client.get_user_profile(token_str)
            if profile and "username" in profile:
                self._token.username = profile["username"]
                self._save_token(self._token)
                return self._token.username
        return None

    def get_valid_token(self) -> Optional[str]:
        """Return a valid access token, auto-refreshing if expired."""
        if not self._token:
            return None

        # Check if token is expired or expiring soon
        if self._token.is_expired():
            if not self._token.refresh_token:
                return None
            refreshed = self.client.refresh_token(self._token.refresh_token)
            if refreshed:
                refreshed.username = self._token.username
                self._save_token(refreshed)
                return refreshed.access_token
            return None

        return self._token.access_token

    def login_interactive(self) -> bool:
        """Run interactive OAuth Device Code authorization in terminal."""
        console.print("[bold cyan]Connecting to Trakt.tv...[/bold cyan]")
        try:
            device = self.client.request_device_code()
        except Exception as e:
            console.print(f"[bold red]✕ Failed to request device authorization code:[/bold red] {e}")
            return False

        card = (
            f"[bold white]1. Open:[/bold white] [bold cyan underline]{device.verification_url}[/bold cyan underline]\n"
            f"[bold white]2. Enter code:[/bold white] [bold green]{device.user_code}[/bold green]\n\n"
            f"[dim]Waiting for authorization (code expires in {device.expires_in // 60} minutes)...[/dim]"
        )
        console.print(
            Panel(
                card,
                title="[bold purple] 🍇 Trakt.tv Authorization [/bold purple]",
                border_style="cyan",
                expand=False,
            )
        )

        interval = max(1, device.interval)
        deadline = time.time() + device.expires_in

        with console.status("[bold cyan]Waiting for Trakt authorization in browser...[/bold cyan]") as status:
            while time.time() < deadline:
                time.sleep(interval)
                token, state = self.client.poll_device_token(device.device_code)

                if state == "success" and token:
                    profile = self.client.get_user_profile(token.access_token)
                    if profile and "username" in profile:
                        token.username = profile["username"]
                    self._save_token(token)
                    user_tag = f" as [bold white]@{token.username}[/bold white]" if token.username else ""
                    console.print(f"[bold green]✓ Successfully connected to Trakt.tv{user_tag}![/bold green]")
                    return True
                elif state == "pending":
                    continue
                elif state == "slow_down":
                    interval += 2
                    continue
                elif state in ("denied", "expired", "invalid", "already_used"):
                    console.print(f"[bold red]✕ Authorization failed ({state}).[/bold red]")
                    return False
                else:
                    status.update(f"[yellow]Retrying authorization... ({state})[/yellow]")

        console.print("[bold red]✕ Device authorization timed out. Please try again.[/bold red]")
        return False

    def logout(self) -> bool:
        """Disconnect and revoke Trakt credentials."""
        if self._token:
            try:
                self.client.revoke_token(self._token.access_token)
            except Exception:
                pass
        if self.auth_file.exists():
            try:
                self.auth_file.unlink(missing_ok=True)
            except Exception:
                pass
        self._token = None
        console.print("[bold green]✓ Disconnected from Trakt.tv.[/bold green]")
        return True
