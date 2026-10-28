"""Synology to Immich view - options, album picker, live stats + logs.

Layout: a scrollable middle (options + album picker) so nothing is ever
squeezed off-screen, and a compact Run bar pinned at the bottom (Start +
stats + progress + log) that stays visible.

Listing albums only needs Synology (no Immich) - the Immich checks happen
at migration time.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import tkinter as tk

import customtkinter as ctk

from synmich.config import resolve_shared_space_mode, save_config
from synmich.core.checkpoint import Checkpoint
from synmich.core.migrator import Migrator, MigrationControl, MigrationStats
from synmich.gui import widgets as W
from synmich.ui.album_selector import _build_records

log = logging.getLogger("synmich")


def _plain(s):
    """Strip Rich markup and emoji/non-ASCII glyphs (they render as boxes in
    the Tk log) so the log stays clean and readable."""
    s = re.sub(r"\[/?[^\]]*\]", "", str(s))
    s = re.sub(r"[^\x00-\x7F]+", "", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


class MigrationView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=W.CONTENT)
        self.app = app
        self.cfg = app.cfg
        self.album_vars: dict = {}
        self.records: dict = {}
        self.stats = None
        self.control = None
        self._log_count = 0
        self._done = False
        self._was_dry_run = False
        mig = self.cfg.setdefault("migration", {})

        ctk.CTkLabel(self, text="Synology to Immich", font=W.font(20, "bold"),
                     text_color=W.TEXT).pack(anchor="w", padx=20, pady=(12, 0))
        ctk.CTkLabel(self, text="Migrate your Synology Photos (albums + "
                     "timeline) into Immich - each user into their own "
                     "account, keeping the original owners, shared albums and "
                     "permissions from Synology.", font=W.font(12),
                     text_color=W.MUTED, wraplength=900, justify="left",
                     anchor="w").pack(anchor="w", padx=20, pady=(0, 6))

        # ---- RUN bar pinned at the bottom (always visible) ----
        run = W.card(self, "Run")
        run.pack(side="bottom", fill="x", padx=16, pady=(4, 10))
        self.start_btn = W.primary_button(run.body, "START MIGRATION",
                                          self._start, height=42)
        self.start_btn.pack(fill="x")
        counters = ctk.CTkFrame(run.body, fg_color="transparent")
        counters.pack(fill="x", pady=(6, 0))
        self.v_up = tk.IntVar()
        self.v_dup = tk.IntVar()
        self.v_fail = tk.IntVar()
        for lbl, var, col in [("UPLOADED", self.v_up, W.GREEN),
                              ("DUPLICATE", self.v_dup, W.BLUE),
                              ("FAILED", self.v_fail, W.RED)]:
            b = ctk.CTkFrame(counters, fg_color="transparent")
            b.pack(side="left", expand=True)
            ctk.CTkLabel(b, textvariable=var, font=W.font(22, "bold"),
                         text_color=col).pack()
            ctk.CTkLabel(b, text=lbl, font=W.font(9, "bold"),
                         text_color=W.MUTED).pack()
        self.lbl_stats = ctk.CTkLabel(run.body, text="", text_color=W.MUTED,
                                      font=W.font(11))
        self.lbl_stats.pack(anchor="w", pady=(4, 0))
        self.progress = ctk.CTkProgressBar(run.body, progress_color=W.GREEN,
                                           corner_radius=0)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=4)
        ctl = ctk.CTkFrame(run.body, fg_color="transparent")
        ctl.pack(fill="x")
        self.pause_btn = W.primary_button(ctl, "Pause", self._toggle_pause,
                                          color=W.GREY, hover=W.GREY_DK,
                                          width=100, height=30)
        self.pause_btn.configure(state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = W.primary_button(ctl, "Stop", self._stop, color=W.RED,
                                         hover=W.RED_DK, width=100, height=30)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.log = ctk.CTkTextbox(run.body, height=60, fg_color="#0d161e",
                                  text_color="#cfe3e0", font=W.font(11),
                                  corner_radius=0)
        self.log.pack(fill="x", pady=(8, 0))

        # ---- Options + album picker (between header and the Run bar) ----
        self._mid = W.scroll_frame(self, fg="transparent")
        self._mid.pack(side="top", fill="both", expand=True, padx=16, pady=6)

        c = W.card(self._mid, "Options")
        c.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(c.body, text="Albums to migrate", text_color=W.TEXT,
                     font=W.font(13, "bold")).pack(anchor="w")
        init_mode = (
            "Specific albums"
            if mig.get("albums_mode", "all") == "select"
            else "All albums"
        )
        self.toggle_mode = W.SquareToggle(
            c.body, ["All albums", "Specific albums"], init_mode,
            command=self._on_mode)
        self.toggle_mode.pack(anchor="w", pady=(2, 2))

        ctk.CTkLabel(c.body, text="Shared albums", text_color=W.TEXT,
                     font=W.font(13, "bold")).pack(anchor="w", pady=(12, 2))
        self.toggle_shared = W.SquareToggle(
            c.body, [n for n, _v, _d in W.SHARED_MODES],
            W.INTERNAL_TO_SHARED.get(mig.get("shared_albums_mode", "link"),
                                     "mirror-syno"),
            command=self._shared_desc, width=120)
        self.toggle_shared.pack(anchor="w", pady=(2, 2))
        self.lbl_shared = ctk.CTkLabel(c.body, text="", text_color=W.MUTED,
                                       font=W.font(11), wraplength=640,
                                       justify="left")
        self.lbl_shared.pack(anchor="w")
        self._shared_desc(self.toggle_shared.get())
        self.var_timeline = ctk.BooleanVar(
            value=bool(mig.get("include_timeline", True)))
        ctk.CTkCheckBox(
            c.body,
            text="Also migrate each user's full timeline (recommended)",
            variable=self.var_timeline, fg_color=W.GREEN,
            hover_color=W.GREEN_DK, checkbox_width=20, checkbox_height=20,
            corner_radius=0, border_width=2, font=W.font(12)).pack(
            anchor="w", pady=(12, 0))

        # Shared Space (FotoTeam): upload to a user, link from an external
        # library (optionally owned by a dedicated 6th Immich user), or skip.
        ctk.CTkLabel(c.body, text="Synology Shared Space (FotoTeam)",
                     text_color=W.TEXT, font=W.font(13, "bold")).pack(
            anchor="w", pady=(12, 2))
        ss_mode_map = {
            "upload": "Upload to owner",
            "external": "External library",
            "ignore": "Skip",
        }
        init_ss = ss_mode_map.get(resolve_shared_space_mode(mig), "Upload to owner")
        self.toggle_shared_space = W.SquareToggle(
            c.body, ["Upload to owner", "External library", "Skip"],
            init_ss, command=self._toggle_shared_space_owner, width=140)
        self.toggle_shared_space.pack(anchor="w", pady=(2, 2))
        # Backward-compat BooleanVar synced with toggle_shared_space
        self.var_shared_space = ctk.BooleanVar(
            value=(init_ss != "Skip"))
        self.shared_owner_row = ctk.CTkFrame(c.body, fg_color="transparent")
        ctk.CTkLabel(
            self.shared_owner_row,
            text="Shared Space owner in Immich:",
            text_color=W.MUTED, font=W.font(11)).pack(side="left", padx=(26, 8))
        self.opt_shared_owner = ctk.CTkOptionMenu(
            self.shared_owner_row, values=[""],
            fg_color=W.GREY, button_color=W.GREEN,
            button_hover_color=W.GREEN_DK, dropdown_fg_color=W.SIDEBAR,
            dropdown_hover_color=W.HOVER, text_color="white",
            corner_radius=0, width=180, height=28, font=W.font(12))
        self.opt_shared_owner.pack(side="left")
        self.shared_ext_row = ctk.CTkFrame(c.body, fg_color="transparent")
        ctk.CTkLabel(
            self.shared_ext_row,
            text="Dedicated Shared Space Immich API key (optional 6th user):",
            text_color=W.MUTED, font=W.font(11)).pack(side="left", padx=(26, 8))
        self.e_shared_api_key = ctk.CTkEntry(
            self.shared_ext_row, corner_radius=0, width=240, show="*",
            placeholder_text="Leave blank to search partners")
        self.e_shared_api_key.pack(side="left")
        if mig.get("shared_space_immich_api_key"):
            self.e_shared_api_key.insert(0, mig["shared_space_immich_api_key"])
        self._shared_space_anchor = ctk.CTkFrame(c.body, height=0,
                                                 fg_color="transparent")
        self._shared_space_anchor.pack(anchor="w")

        # External-library mode: default fallback when per-user external_library
        # is not explicitly set on a user in Settings.
        self.var_external = ctk.BooleanVar(
            value=bool(mig.get("external_library_mode", False)))
        ctk.CTkCheckBox(
            c.body,
            text="Default all users to external library mode unless overridden per user in Settings",
            variable=self.var_external, fg_color=W.GREEN,
            hover_color=W.GREEN_DK, checkbox_width=20, checkbox_height=20,
            corner_radius=0, border_width=2, font=W.font(12)).pack(
            anchor="w", pady=(6, 0))

        # Dry-run mode: simulate the migration without downloading or
        # uploading anything.
        self.var_dry_run = ctk.BooleanVar(
            value=bool(mig.get("dry_run", False)))
        ctk.CTkCheckBox(
            c.body,
            text="Dry run (simulate without downloading or uploading anything)",
            variable=self.var_dry_run,
            command=self._on_dry_run_toggle,
            fg_color=W.ORANGE, hover_color=W.ORANGE_DK,
            checkbox_width=20, checkbox_height=20,
            corner_radius=0, border_width=2, font=W.font(12)).pack(
            anchor="w", pady=(6, 0))

        self.albums_card = W.card(self._mid, "Albums (tick the ones to migrate)")
        top = ctk.CTkFrame(self.albums_card.body, fg_color="transparent")
        top.pack(fill="x")
        W.primary_button(top, "Load albums", self._load, color=W.BLUE,
                         hover=W.BLUE_DK).pack(side="left")
        self.sel_count = ctk.CTkLabel(top, text="0 selected",
                                      text_color=W.GREEN, font=W.font(11,
                                                                      "bold"))
        self.sel_count.pack(side="left", padx=12)
        W.primary_button(top, "Uncheck all", lambda: self._set_all(False),
                         color=W.GREY, hover=W.GREY_DK, width=100,
                         height=30).pack(side="right")
        W.primary_button(top, "Check all", lambda: self._set_all(True),
                         color=W.GREY, hover=W.GREY_DK, width=90,
                         height=30).pack(side="right", padx=(0, 6))
        self.albums_box = ctk.CTkFrame(self.albums_card.body,
                                       fg_color="transparent")
        self.albums_box.pack(fill="both", expand=True, pady=(8, 0))
        ctk.CTkLabel(self.albums_box,
                     text="Click \"Load albums\" to choose what to migrate.",
                     text_color=W.MUTED).pack(anchor="w", padx=8, pady=8)
        self.refresh_users_options()
        self._toggle_shared_space_owner()
        self._on_dry_run_toggle()
        self._toggle_albums(
            "Pick" if self.toggle_mode.get() == "Specific albums" else "All")
        W.fix_wrapping(self)

    # ---------------------------------------------------------- options
    def _shared_desc(self, name):
        desc = {n: d for n, _v, d in W.SHARED_MODES}.get(name, "")
        self.lbl_shared.configure(text=desc)

    def _toggle_shared_space_owner(self, _val=None):
        choice = self.toggle_shared_space.get()
        self.var_shared_space.set(choice != "Skip")
        self.shared_owner_row.pack_forget()
        self.shared_ext_row.pack_forget()
        if choice == "Upload to owner":
            self.shared_owner_row.pack(anchor="w", pady=(4, 0),
                                       before=self._shared_space_anchor)
        elif choice == "External library":
            self.shared_ext_row.pack(anchor="w", pady=(4, 0),
                                     before=self._shared_space_anchor)

    def _on_dry_run_toggle(self):
        if self.var_dry_run.get():
            self.start_btn.configure(
                text="START DRY RUN (SIMULATE)",
                fg_color=W.ORANGE, hover_color=W.ORANGE_DK)
        else:
            self.start_btn.configure(
                text="START MIGRATION",
                fg_color=W.GREEN, hover_color=W.GREEN_DK)

    def refresh_users_options(self):
        users = self.cfg.get("users", [])
        names = [u.get("name") or u.get("syno_username") or ""
                 for u in users]
        names = [n for n in names if n]
        mig = self.cfg.get("migration", {})
        cur = mig.get("shared_space_owner", "")
        if not names:
            self.opt_shared_owner.configure(values=[""], state="disabled")
            self.opt_shared_owner.set("")
            return
        self.opt_shared_owner.configure(values=names, state="normal")
        if cur in names:
            self.opt_shared_owner.set(cur)
        else:
            self.opt_shared_owner.set(names[0])

    def _on_mode(self, val):
        self._toggle_albums("Pick" if val == "Specific albums" else "All")

    def _toggle_albums(self, value):
        if value == "Pick":
            self.albums_card.pack(fill="both", expand=True, pady=(0, 6))
        else:
            self.albums_card.pack_forget()

    def _busy(self, on):
        if on:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0)

    # ---------------------------------------------------------- albums
    def _load(self):
        if not self.cfg.get("synology", {}).get("url"):
            self._set_log("Set the Synology address or IP in Settings (left "
                          "panel) first.")
            return
        if not self.cfg.get("users"):
            self._set_log("No user yet. Add a user in the Settings panel "
                          "(left), then load albums.")
            return
        self._busy(True)
        # Listing albums only needs Synology (no Immich key) for the migration
        # users, so we log them in via Synology only.
        if not self.app.users_syno_sessions:
            self._set_log("Connecting to Synology...")
            self.app.connect_users_syno(self._after_connect_load)
        else:
            self._set_log("Loading albums...")
            threading.Thread(target=self._load_worker, daemon=True).start()

    def _after_connect_load(self, ok, msg):
        if ok:
            self._set_log("Connected. Loading albums...")
            threading.Thread(target=self._load_worker, daemon=True).start()
        else:
            self._busy(False)
            self._set_log(msg)

    def _load_worker(self):
        try:
            records = _build_records(self.app.users_syno_sessions)
        except Exception as e:  # noqa: BLE001
            msg = f"Load albums failed: {e}"
            self.after(0, lambda: (self._busy(False), self._set_log(msg)))
            return
        self.after(0, lambda: self._populate(records))

    def _populate(self, records):
        self._busy(False)
        self.records = records
        pre = set(self.cfg.get("migration", {}).get("selected_albums") or [])
        n = W.render_album_groups(self.albums_box, records, self.album_vars,
                                  preselected=pre, on_change=self._update_count)
        self._update_count()
        self._set_log(f"Loaded {n} album(s)." if n else "No album found.")

    def _update_count(self):
        n = sum(1 for v in self.album_vars.values() if v.get())
        self.sel_count.configure(text=f"{n} selected")

    def clear_albums(self):
        """Drop the loaded album list (e.g. after an account changed)."""
        self.album_vars = {}
        self.records = {}
        for w in self.albums_box.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.albums_box,
                     text="Click \"Load albums\" to choose what to migrate.",
                     text_color=W.MUTED).pack(anchor="w", padx=8, pady=8)
        self.sel_count.configure(text="0 selected")
        self.refresh_users_options()

    def _set_all(self, value):
        for v in self.album_vars.values():
            v.set(value)
        self._update_count()

    # ---------------------------------------------------------- run
    def _pull(self):
        mig = self.cfg.setdefault("migration", {})
        mig["albums_mode"] = (
            "select" if self.toggle_mode.get() == "Specific albums" else "all")
        mig["shared_albums_mode"] = W.SHARED_TO_INTERNAL.get(
            self.toggle_shared.get(), "link")
        mig["include_timeline"] = bool(self.var_timeline.get())
        toggle_ss = getattr(self, "toggle_shared_space", None)
        if toggle_ss is not None:
            ss_choice = toggle_ss.get()
            ss_rev = {
                "Upload to owner": "upload",
                "External library": "external",
                "Skip": "ignore",
            }
            ss_mode = ss_rev.get(ss_choice, "upload")
            if hasattr(self, "var_shared_space") and not self.var_shared_space.get():
                ss_mode = "ignore"
            mig["shared_space_mode"] = ss_mode
            mig["include_shared_space"] = (ss_mode != "ignore")
        else:
            mig["include_shared_space"] = bool(self.var_shared_space.get())
            ss_mode = "upload" if mig["include_shared_space"] else "ignore"
        owner = (self.opt_shared_owner.get() or "").strip()
        mig["shared_space_owner"] = owner if ss_mode == "upload" else ""
        e_key = getattr(self, "e_shared_api_key", None)
        if e_key is not None:
            mig["shared_space_immich_api_key"] = (
                e_key.get().strip() if ss_mode == "external" else ""
            )
        mig["external_library_mode"] = bool(self.var_external.get())
        if mig["albums_mode"] == "select":
            mig["selected_albums"] = [
                k for k, v in self.album_vars.items() if v.get()]

    def _start(self):
        self._pull()
        issues = W.friendly_config(self.cfg)
        if issues:
            self._set_log("Cannot start: " + issues[0])
            return
        if not self.app.sessions:
            self._set_log("Connecting...")
            self.app.connect(lambda ok, msg: self._launch() if ok
                             else self._set_log(msg))
        else:
            self._launch()

    def _launch(self):
        self.stats = MigrationStats()
        self.control = MigrationControl()
        self.stats.start_time = time.time()
        self._log_count = 0
        self._done = False
        self._was_dry_run = bool(self.var_dry_run.get())
        self.log.delete("1.0", "end")
        save_config(self.cfg)
        if self._was_dry_run:
            self._set_log(
                "Dry run mode: simulating migration - no files will be "
                "downloaded or uploaded to Immich.")
        migrator = Migrator(
            config=self.cfg, sessions=self.app.sessions,
            checkpoint=Checkpoint(config=self.cfg),
            stats=self.stats, control=self.control,
            dry_run=self._was_dry_run)
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._run_worker, args=(migrator,),
                         daemon=True).start()
        self._poll()

    def _run_worker(self, migrator):
        try:
            migrator.run()
        except Exception as e:  # noqa: BLE001
            log.exception("Migration crashed in GUI")
            self.stats.log_message(f"Migration crashed: {e}")
        finally:
            self.stats.current_step = "done"

    def _poll(self):
        s = self.stats
        if s is None:
            return
        self.v_up.set(s.uploaded)
        self.v_dup.set(s.duplicate)
        self.v_fail.set(s.failed)
        el, eta, rate = (int(s.elapsed_seconds()), int(s.eta_seconds()),
                         s.rate_per_minute())
        cur = f"{s.current_album}    " if s.current_album else ""
        self.lbl_stats.configure(
            text=f"{cur}Albums {s.albums_done}/{s.albums_total}    "
                 f"Photos {s.items_done}/{s.items_total}    "
                 f"Elapsed {W.fmt_duration(el)}    "
                 f"ETA {W.fmt_duration(eta)}    {rate:.0f}/min")
        if s.items_total:
            self.progress.set(s.items_done / s.items_total)
        total = getattr(s, "total_messages_logged", len(s.last_messages))
        if total > self._log_count:
            new_n = min(total - self._log_count, len(s.last_messages))
            for m in s.last_messages[-new_n:]:
                self.log.insert("end", _plain(m) + "\n")
            self.log.see("end")
            self._log_count = total
        if s.current_step == "done" and not self._done:
            self._done = True
            self.progress.set(1)
            self.pause_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.start_btn.configure(state="normal")
            if self._was_dry_run:
                rows = [
                    ("Would migrate", str(s.uploaded), W.ORANGE),
                    ("Albums", str(s.albums_done), W.TEXT),
                    ("Total time", W.fmt_duration(el), W.MUTED),
                ]
                self.app.last_run = {
                    "label": "Synology to Immich (Dry run)",
                    "rows": rows,
                }
                self._set_log(
                    f"\nDry run complete - {s.uploaded} photo(s) would be "
                    f"migrated across {s.albums_done} album(s) in "
                    f"{W.fmt_duration(el)} (nothing downloaded or uploaded).")
            else:
                rows = [("Uploaded", str(s.uploaded), W.GREEN)]
                if s.linked:
                    rows.append(
                        ("Linked (external)", str(s.linked), W.GREEN))
                rows += [
                    ("Duplicate", str(s.duplicate), W.BLUE),
                    ("Failed", str(s.failed), W.RED),
                    ("Albums", str(s.albums_done), W.TEXT),
                    ("Total time", W.fmt_duration(el), W.MUTED)]
                self.app.last_run = {
                    "label": "Synology to Immich",
                    "rows": rows,
                }
                linked_txt = f"{s.linked} linked, " if s.linked else ""
                self._set_log(
                    f"\nMigration complete - {s.uploaded} uploaded, "
                    f"{linked_txt}"
                    f"{s.duplicate} duplicate, {s.failed} failed, "
                    f"{s.albums_done} album(s) in {W.fmt_duration(el)}.")
        else:
            self.after(300, self._poll)

    def _toggle_pause(self):
        c = self.control
        if not c:
            return
        if c.is_paused():
            c.resume()
            self.pause_btn.configure(text="Pause")
        else:
            c.pause()
            self.pause_btn.configure(text="Resume")

    def _stop(self):
        if self.control:
            self.control.stop()

    def _set_log(self, msg):
        self.log.insert("end", _plain(msg) + "\n")
        self.log.see("end")
