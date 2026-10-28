"""Checkpoint v2 (v1.1.0): one checkpoint per unique configuration.

Before v1.1.0:
    ~/.config/synmich/checkpoint.json   ← shared across all configs

After v1.1.0:
    ~/.config/synmich/checkpoints/
    ├── <hash>.json                     ← one checkpoint per config
    ├── index.json                      ← user-friendly metadata
    └── <hash>.lock                     ← (optional, future multi-process)

The hash is derived from the tuple (synology_url, immich_url, sorted_user_names).
Changing a user, the Synology URL, or the Immich URL → new checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("synmich")


CHECKPOINTS_DIR_NAME = "checkpoints"
INDEX_FILE_NAME = "index.json"
LEGACY_CHECKPOINT_NAME = "checkpoint.json"


def _config_hash(config: Dict[str, Any]) -> str:
    """Compute a stable hash identifying this config.

    Based on:
    - Synology URL
    - Immich URL
    - Sorted list of user names

    These 3 dimensions are the "axes" of a migration. Changing them
    means we're migrating something different, hence a new checkpoint.
    """
    syno_url = (
        config.get("synology", {}).get("url")
        or config.get("synology", {}).get("base_url", "")
    )
    immich_url = (
        config.get("immich", {}).get("url")
        or config.get("immich", {}).get("base_url", "")
    )
    user_names = sorted([
        u.get("name", "") for u in config.get("users", [])
    ])

    payload = json.dumps({
        "synology": syno_url.rstrip("/"),
        "immich": immich_url.rstrip("/"),
        "users": user_names,
    }, sort_keys=True).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()[:12]


def _config_description(config: Dict[str, Any]) -> str:
    """User-friendly description of a config (for the index)."""
    syno_url = (
        config.get("synology", {}).get("url")
        or config.get("synology", {}).get("base_url", "?")
    )
    user_names = [u.get("name", "") for u in config.get("users", [])]
    return f"{syno_url} → {', '.join(user_names)}"


def get_checkpoints_dir() -> Path:
    """Directory where checkpoints are stored."""
    from synmich.config import get_config_dir
    d = get_config_dir() / CHECKPOINTS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_checkpoint_path_for_config(config: Dict[str, Any]) -> Path:
    """Return the checkpoint path for this config."""
    h = _config_hash(config)
    return get_checkpoints_dir() / f"{h}.json"


def _load_index() -> Dict[str, Any]:
    """Load the checkpoint index."""
    p = get_checkpoints_dir() / INDEX_FILE_NAME
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Cannot load checkpoint index: %s", e)
        return {}


def _save_index(index: Dict[str, Any]) -> None:
    """Save the checkpoint index."""
    p = get_checkpoints_dir() / INDEX_FILE_NAME
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    tmp.replace(p)


def update_index_for_config(config: Dict[str, Any]) -> None:
    """Update the index with this config's info."""
    h = _config_hash(config)
    index = _load_index()
    index[h] = {
        "description": _config_description(config),
        "synology_url": (
            config.get("synology", {}).get("url")
            or config.get("synology", {}).get("base_url", "")
        ),
        "immich_url": (
            config.get("immich", {}).get("url")
            or config.get("immich", {}).get("base_url", "")
        ),
        "users": [u.get("name", "") for u in config.get("users", [])],
        "last_used": datetime.now().isoformat(timespec="seconds"),
    }
    _save_index(index)


def list_all_checkpoints() -> List[Dict[str, Any]]:
    """List all available checkpoints with their metadata."""
    index = _load_index()
    out = []
    for h, meta in index.items():
        path = get_checkpoints_dir() / f"{h}.json"
        if path.exists():
            # Read counters
            try:
                with open(path) as f:
                    data = json.load(f)
                counters = data.get("counters", {})
                albums_done = len(data.get("albums_done", []))
            except Exception:
                counters = {}
                albums_done = 0
            out.append({
                "hash": h,
                "path": str(path),
                "description": meta.get("description", "?"),
                "synology_url": meta.get("synology_url", ""),
                "immich_url": meta.get("immich_url", ""),
                "users": meta.get("users", []),
                "last_used": meta.get("last_used", "?"),
                "counters": counters,
                "albums_done": albums_done,
            })
    return sorted(out, key=lambda x: x["last_used"], reverse=True)


def delete_checkpoint(hash_or_prefix: str) -> bool:
    """Delete a checkpoint by its hash (or prefix)."""
    cps = list_all_checkpoints()
    matches = [c for c in cps if c["hash"].startswith(hash_or_prefix)]
    if not matches:
        return False
    if len(matches) > 1:
        log.warning("Ambiguous hash prefix '%s', matches: %s",
                    hash_or_prefix, [m["hash"] for m in matches])
        return False

    cp = matches[0]
    Path(cp["path"]).unlink(missing_ok=True)
    # Remove from index
    index = _load_index()
    index.pop(cp["hash"], None)
    _save_index(index)
    return True


def migrate_legacy_checkpoint(config: Dict[str, Any]) -> Optional[Path]:
    """Migrate the old checkpoint.json (v1.0.x) to the new format.

    If ~/.config/synmich/checkpoint.json exists AND already contains
    data, migrate it to the checkpoint of the current config.
    """
    from synmich.config import get_checkpoint_file
    legacy = get_checkpoint_file()
    if not legacy.exists():
        return None

    try:
        with open(legacy) as f:
            data = json.load(f)
    except Exception:
        return None

    # Skip if truly empty
    has_data = (
        data.get("syno_to_immich")
        or data.get("albums_done")
        or data.get("counters", {}).get("uploaded", 0) > 0
    )
    if not has_data:
        return None

    # Compare users in the legacy checkpoint vs config
    legacy_users = set(data.get("syno_to_immich", {}).keys())
    config_users = set(u.get("name", "") for u in config.get("users", []))

    if not legacy_users.issubset(config_users) and legacy_users:
        log.warning(
            "Legacy checkpoint has users %s but current config has %s. "
            "Skipping auto-migration. Run 'synmich checkpoints migrate' manually.",
            legacy_users, config_users,
        )
        return None

    # Migrate
    new_path = get_checkpoint_path_for_config(config)
    if new_path.exists():
        log.info("Checkpoint for current config already exists, skipping migration")
        return None

    # Backup legacy then move
    backup = legacy.with_suffix(".json.legacy-backup")
    shutil.copy2(legacy, backup)
    shutil.copy2(legacy, new_path)
    update_index_for_config(config)

    log.info(
        "Migrated legacy checkpoint to %s (backup at %s)",
        new_path.name, backup.name,
    )
    return new_path


class Checkpoint:
    """Atomic + thread-safe persistence, with config-aware paths."""

    def __init__(
        self,
        path: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Init with an explicit path OR derived from the config.

        If config is provided, use the path derived from the config hash.
        If path is provided explicitly, use it as-is (legacy).
        """
        self._config = config
        if config is not None:
            # Try to migrate a legacy checkpoint if not done yet
            migrate_legacy_checkpoint(config)
            self.path = get_checkpoint_path_for_config(config)
            # Update index for this config
            update_index_for_config(config)
            self._config_hash = _config_hash(config)
        elif path is not None:
            self.path = path
            self._config_hash = None
        else:
            # Fallback legacy
            from synmich.config import get_checkpoint_file
            self.path = get_checkpoint_file()
            self._config_hash = None

        self.lock = threading.Lock()
        self.data: Dict[str, Any] = self._load()

    def _seed_from_peer_checkpoints(self, d: Dict[str, Any]) -> None:
        """Carry over per-user `syno_to_immich` mappings and `timeline_done`
        flags from older checkpoints for the same Synology + Immich servers
        when users are added/removed or renamed (e.g. 'Maria' -> 'mariapaz').

        Does NOT copy `albums_done` so shared albums are re-evaluated with the
        expanded user set, and does NOT write to disk until `save()` is called.
        """
        if not self._config:
            return
        syno_url = (
            self._config.get("synology", {}).get("url")
            or self._config.get("synology", {}).get("base_url", "")
        ).rstrip("/")
        immich_url = (
            self._config.get("immich", {}).get("url")
            or self._config.get("immich", {}).get("base_url", "")
        ).rstrip("/")
        if not syno_url or not immich_url:
            return
        users_cfg = [
            u for u in self._config.get("users", [])
            if isinstance(u, dict) and (u.get("name") or u.get("syno_username"))
        ]
        if not users_cfg:
            return
        index = _load_index()
        peers = []
        for h, meta in index.items():
            if h == self._config_hash:
                continue
            m_syno = (meta.get("synology_url") or "").rstrip("/")
            m_imm = (meta.get("immich_url") or "").rstrip("/")
            if m_syno == syno_url and m_imm == immich_url:
                p = get_checkpoints_dir() / f"{h}.json"
                if p.exists():
                    peers.append((meta.get("last_used") or "", p))
        peers.sort(key=lambda item: item[0])

        def _match_peer_user(u_cfg: Dict[str, Any], peer_keys: List[str]) -> Optional[str]:
            u_name = u_cfg.get("name") or ""
            u_syno = u_cfg.get("syno_username") or ""
            candidates = [c for c in (u_name, u_syno) if c]
            # 1) Exact match
            for c in candidates:
                if c in peer_keys:
                    return c
            # 2) Case-insensitive match
            lower_map = {pk.lower(): pk for pk in peer_keys}
            for c in candidates:
                if c.lower() in lower_map:
                    return lower_map[c.lower()]
            # 3) Prefix match (e.g. display name "Maria" <-> syno_username "mariapaz")
            for c in candidates:
                cl = c.lower()
                pref_hits = [
                    pk for pk in peer_keys
                    if pk.lower().startswith(cl) or cl.startswith(pk.lower())
                ]
                if len(pref_hits) == 1:
                    return pref_hits[0]
            return None

        for _, peer_path in peers:
            try:
                with open(peer_path, encoding="utf-8") as f:
                    peer_data = json.load(f)
            except Exception:
                continue
            peer_map = peer_data.get("syno_to_immich") or {}
            peer_tl = peer_data.get("timeline_done") or {}
            peer_keys = list(peer_map.keys())
            for u_cfg in users_cfg:
                u_name = u_cfg.get("name") or u_cfg.get("syno_username") or ""
                if not u_name:
                    continue
                peer_u = _match_peer_user(u_cfg, peer_keys)
                if peer_u and isinstance(peer_map.get(peer_u), dict):
                    target = d.setdefault("syno_to_immich", {}).setdefault(
                        u_name, {}
                    )
                    for k, v in peer_map[peer_u].items():
                        target.setdefault(k, v)
                if peer_u and peer_tl.get(peer_u) is True:
                    d.setdefault("timeline_done", {}).setdefault(u_name, True)

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            d = self._default()
            self._seed_from_peer_checkpoints(d)
            return d
        try:
            with open(self.path) as f:
                d = json.load(f)
            d.setdefault("syno_to_immich", {})
            d.setdefault("albums_done", [])
            d.setdefault("timeline_done", {})
            d.setdefault("failed_items", {})
            d.setdefault("counters", {})
            d.setdefault("shared_space_done", False)
            d["counters"].setdefault("uploaded", 0)
            d["counters"].setdefault("duplicate", 0)
            d["counters"].setdefault("linked", 0)
            d["counters"].setdefault("failed", 0)
            d["counters"].setdefault("skipped_already_done", 0)
            self._seed_from_peer_checkpoints(d)
            return d
        except Exception as e:
            log.warning("Could not load checkpoint, using default: %s", e)
            return self._default()

    @staticmethod
    def _default() -> Dict[str, Any]:
        return {
            "syno_to_immich": {},
            "albums_done": [],
            "timeline_done": {},
            "failed_items": {},
            "shared_space_done": False,
            "counters": {
                "uploaded": 0,
                "duplicate": 0,
                "linked": 0,
                "failed": 0,
                "skipped_already_done": 0,
            },
        }

    def save(self) -> None:
        """Atomic save."""
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            tmp.replace(self.path)

    # Existing API (v1.0.x compat)
    def is_uploaded(self, user: str, syno_id: str) -> Optional[str]:
        with self.lock:
            return self.data.get("syno_to_immich", {}).get(user, {}).get(str(syno_id))

    def mark_uploaded(
        self,
        user: str,
        syno_id: str,
        immich_id: str,
        was_duplicate: bool = False,
    ) -> None:
        """Record a syno_id -> immich_id mapping after a successful upload.

        was_duplicate routes the count to the 'duplicate' counter instead of
        'uploaded' (Immich reported the asset already existed). The Migrator
        passes this flag; without accepting it, mark_uploaded raised TypeError
        on every successful upload, which the parallel worker loop swallowed —
        leaving albums marked done with 0 items recorded.
        """
        with self.lock:
            self.data.setdefault("syno_to_immich", {}).setdefault(user, {})[
                str(syno_id)
            ] = immich_id
            self.data.get("failed_items", {}).get(user, {}).pop(
                str(syno_id), None
            )
            key = "duplicate" if was_duplicate else "uploaded"
            self.data["counters"][key] = (
                self.data["counters"].get(key, 0) + 1
            )

    def mark_linked(self, user: str, syno_id: str, immich_id: str) -> None:
        """External-library mode: record that this Synology photo was matched
        to an existing Immich asset (no upload) and linked to the album.

        Stores the syno_id -> immich_id mapping like a normal upload so resumes
        skip it, but counts it under 'linked' rather than 'uploaded'.
        """
        with self.lock:
            self.data.setdefault("syno_to_immich", {}).setdefault(user, {})[
                str(syno_id)
            ] = immich_id
            self.data.get("failed_items", {}).get(user, {}).pop(
                str(syno_id), None
            )
            self.data["counters"]["linked"] = (
                self.data["counters"].get("linked", 0) + 1
            )

    def mark_duplicate(self, user: str, syno_id: str, immich_id: str) -> None:
        with self.lock:
            self.data.setdefault("syno_to_immich", {}).setdefault(user, {})[
                str(syno_id)
            ] = immich_id
            self.data.get("failed_items", {}).get(user, {}).pop(
                str(syno_id), None
            )
            self.data["counters"]["duplicate"] = (
                self.data["counters"].get("duplicate", 0) + 1
            )

    def mark_failed(self, user: str, syno_id: str, error: str = "") -> None:
        with self.lock:
            self.data.get("syno_to_immich", {}).get(user, {}).pop(
                str(syno_id), None
            )
            self.data.setdefault("failed_items", {}).setdefault(user, {})[
                str(syno_id)
            ] = error
            self.data["counters"]["failed"] = (
                self.data["counters"].get("failed", 0) + 1
            )

    def has_failed_items(self, syno_ids: Optional[List[str]] = None) -> bool:
        """Whether unresolved failures exist, optionally restricted to an album.

        Older versions marked albums complete even when some items failed.
        Ignore stale failure records for items since migrated successfully.
        """
        wanted = set(syno_ids) if syno_ids is not None else None
        with self.lock:
            for user, failed in self.data.get("failed_items", {}).items():
                completed = self.data.get("syno_to_immich", {}).get(user, {})
                if any(
                    item_id not in completed and (wanted is None or item_id in wanted)
                    for item_id in failed
                ):
                    return True
        return False

    def is_album_done(self, passphrase: str) -> bool:
        with self.lock:
            return passphrase in self.data.get("albums_done", [])

    def mark_album_done(self, passphrase: str) -> None:
        with self.lock:
            done = self.data.setdefault("albums_done", [])
            if passphrase not in done:
                done.append(passphrase)

    def is_timeline_done(self, user: str) -> bool:
        """Check if the timeline has been fully processed for this user."""
        with self.lock:
            return self.data.get("timeline_done", {}).get(user, False) is True

    def mark_timeline_done(self, user: str) -> None:
        """Mark the timeline as fully processed for this user."""
        with self.lock:
            self.data.setdefault("timeline_done", {})[user] = True

    def is_shared_space_done(self) -> bool:
        with self.lock:
            return self.data.get("shared_space_done", False) is True

    def mark_shared_space_done(self) -> None:
        with self.lock:
            self.data["shared_space_done"] = True

    def mark_skipped_already_done(self) -> None:
        """Increment the counter of skipped items (already migrated)."""
        with self.lock:
            self.data["counters"]["skipped_already_done"] = (
                self.data["counters"].get("skipped_already_done", 0) + 1
            )

    def get_summary(self) -> Dict[str, Any]:
        """Return a summary for display."""
        counters = self.data.get("counters", {})
        return {
            "uploaded": counters.get("uploaded", 0),
            "duplicate": counters.get("duplicate", 0),
            "linked": counters.get("linked", 0),
            "failed": counters.get("failed", 0),
            "skipped": counters.get("skipped_already_done", 0),
            "albums_done": len(self.data.get("albums_done", [])),
            "users": list(self.data.get("syno_to_immich", {}).keys()),
            "items_per_user": {
                u: len(m)
                for u, m in self.data.get("syno_to_immich", {}).items()
            },
        }


    def reset(self) -> None:
        """Reset the checkpoint to empty state."""
        with self.lock:
            self.data = self._default()
        # save() re-acquires self.lock; calling it while still holding the
        # lock deadlocks (threading.Lock is non-reentrant) and hangs
        # `synmich migrate --reset`. reset() runs single-threaded at startup,
        # so saving just after releasing the lock is safe.
        self.save()


def detect_config_mismatch(
    config: Dict[str, Any],
    checkpoint_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Detect whether the current checkpoint does not match the config.

    Returns:
        None if everything is fine
        Dict with the mismatch details otherwise:
            {
                "type": "users_mismatch" | "urls_mismatch",
                "config_users": [...],
                "checkpoint_users": [...],
                ...
            }
    """
    if checkpoint_path is None:
        checkpoint_path = get_checkpoint_path_for_config(config)

    if not checkpoint_path.exists():
        return None

    try:
        with open(checkpoint_path) as f:
            cp_data = json.load(f)
    except Exception:
        return None

    config_users = set(u.get("name", "") for u in config.get("users", []))
    cp_users = set(cp_data.get("syno_to_immich", {}).keys())

    extra_in_cp = cp_users - config_users
    if extra_in_cp:
        return {
            "type": "extra_users_in_checkpoint",
            "config_users": sorted(list(config_users)),
            "checkpoint_users": sorted(list(cp_users)),
            "extra_in_checkpoint": sorted(list(extra_in_cp)),
        }

    return None
