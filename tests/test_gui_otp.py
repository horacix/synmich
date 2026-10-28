"""Headless checks of the GUI OTP handoff and nested modal ownership."""

import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from synmich.gui.app import SynmichApp
from synmich.gui.dialogs import prompt_otp
from synmich.gui.views.migration import MigrationView


def test_nested_otp_dialog_is_owned_by_account_form_and_restores_grab():
    parent = MagicMock()
    account_form = parent.grab_current.return_value
    with patch("synmich.gui.dialogs.ctk.CTkToplevel") as toplevel:
        with patch("synmich.gui.dialogs.ctk.CTkLabel"), patch("synmich.gui.dialogs.ctk.CTkEntry"):
            with (
                patch("synmich.gui.dialogs.W.primary_button"),
                patch("synmich.gui.dialogs.W.fix_wrapping"),
                patch("synmich.gui.dialogs.W.font"),
            ):
                assert prompt_otp(parent, "alice") is None
    toplevel.assert_called_once_with(account_form)
    toplevel.return_value.transient.assert_called_once_with(account_form)
    account_form.grab_set.assert_called_once()


def test_otp_dialog_exception_unblocks_login_worker():
    callbacks = queue.Queue()
    parent = MagicMock()
    parent.after.side_effect = lambda delay, callback: callbacks.put(callback)
    result = []
    worker = threading.Thread(target=lambda: result.append(SynmichApp._gui_otp(parent, "alice")))
    worker.start()
    callback = callbacks.get(timeout=2)
    with patch("synmich.gui.dialogs.prompt_otp", side_effect=RuntimeError("dialog failed")):
        with pytest.raises(RuntimeError, match="dialog failed"):
            callback()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == [""]


def test_migration_view_pull_and_dry_run_wiring():
    view = MigrationView.__new__(MigrationView)
    view.cfg = {
        "migration": {
            "include_shared_space": True,
            "shared_space_owner": "Alice",
        },
        "users": [
            {"name": "Alice", "syno_username": "alice"},
            {"name": "Bob", "syno_username": "bob"},
        ],
    }
    view.toggle_mode = MagicMock()
    view.toggle_mode.get.return_value = "All albums"
    view.toggle_shared = MagicMock()
    view.toggle_shared.get.return_value = "mirror-syno"
    view.var_timeline = MagicMock()
    view.var_timeline.get.return_value = True
    view.var_shared_space = MagicMock()
    view.var_shared_space.get.return_value = True
    view.opt_shared_owner = MagicMock()
    view.opt_shared_owner.get.return_value = "Bob"
    view.var_external = MagicMock()
    view.var_external.get.return_value = False
    view.var_dry_run = MagicMock()
    view.var_dry_run.get.return_value = True
    view.album_vars = {}

    view._pull()
    assert view.cfg["migration"]["include_shared_space"] is True
    assert view.cfg["migration"]["shared_space_owner"] == "Bob"

    view.var_shared_space.get.return_value = False
    view._pull()
    assert view.cfg["migration"]["include_shared_space"] is False
    assert view.cfg["migration"]["shared_space_owner"] == ""

    view.refresh_users_options()
    view.opt_shared_owner.configure.assert_called_with(
        values=["Alice", "Bob"], state="normal"
    )
    view.opt_shared_owner.set.assert_called_with("Alice")
