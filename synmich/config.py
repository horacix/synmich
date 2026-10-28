"""synmich configuration management."""

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def get_config_dir() -> Path:
    """Return the config dir based on OS."""
    if os.name == "nt":  # Windows
        base = Path(os.environ.get("APPDATA", "~/.config"))
    else:
        base = Path(
            os.environ.get("XDG_CONFIG_HOME", "~/.config")
        )
    path = base.expanduser() / "synmich"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_config_file() -> Path:
    return get_config_dir() / "config.yaml"


def get_checkpoint_file() -> Path:
    return get_config_dir() / "checkpoint.json"


def get_log_file() -> Path:
    return get_config_dir() / "migration.log"


DEFAULT_CONFIG: Dict[str, Any] = {
    "synology": {
        "url": "",
        "verify_ssl": False,
    },
    "immich": {
        "url": "",
    },
    # List of {name, syno_username, syno_password, immich_api_key, external_library?}
    "users": [],
    "migration": {
        "include_albums": True,
        "include_timeline": True,
        # Which albums to migrate:
        #   all    - every album (default; still filtered by
        #            filters.include_albums_regex if set)
        #   select - only the albums chosen interactively, stored in
        #            selected_albums below (see `synmich migrate --select`)
        "albums_mode": "all",
        # Passphrases (or local-<user>-<id> keys) of the albums to migrate
        # when albums_mode == "select". Filled by the interactive selector.
        "selected_albums": [],
        # How to handle shared Synology albums:
        #   link      - one shared album in Immich, preserves owner +
        #               cross-user permissions (default, recommended)
        #   duplicate - each user gets their own copy of the album
        #               with only their own photos
        #   ignore    - skip shared albums entirely
        "shared_albums_mode": "link",
        # External-library mode: set True when your photos are already in
        # Immich through an *external library* (Immich reads them in place,
        # e.g. a mounted Synology share). synmich then matches each Synology
        # photo to the existing asset by filename + capture date and adds it
        # to the album WITHOUT re-uploading — avoiding the duplicate copy that
        # Immich's checksum dedup can't catch for external assets (see
        # immich-app/immich#7804). Unmatched photos are reported for retry.
        # Can also be overridden per user via users[i].external_library.
        "external_library_mode": False,
        # Opt in to uploading unmatched photos (may create external duplicates).
        "external_library_fallback_upload": False,
        "include_shared_space": True,
        # How to handle Synology Shared Space (FotoTeam, owner_user_id=0):
        #   ""         - auto (derived from include_shared_space /
        #                shared_space_immich_api_key / external_library_mode)
        #   "upload"   - download and upload Shared Space items to shared_space_owner
        #   "external" - Shared Space already lives in Immich as an external
        #                library (link album items only, never upload)
        #   "ignore"   - skip Shared Space
        "shared_space_mode": "",
        # Name of the user who becomes owner of Shared Space photos (upload mode)
        "shared_space_owner": "",
        # Optional Immich API key of a dedicated Immich account holding the
        # Shared Space external library (indexed once for linking across all users).
        "shared_space_immich_api_key": "",
        # Default Immich role for users with "view" permission on Syno
        # (users with "upload" permission always become "editor")
        "share_role": "editor",
    },
    "execution": {
        "mode": "parallel",  # parallel | sequential
        "workers": 4,
        "max_retries": 5,
        "retry_backoff_seconds": 3,
        # Only deletes LOCAL temporary files after upload.
        # Synology NAS files are NEVER modified.
        "delete_after_upload": True,
        "verbose": False,
    },
    "filters": {
        "include_albums_regex": "",
        "exclude_albums_regex": "",
        "min_date": "",
        "max_date": "",
        "include_videos": True,
        "max_file_size_mb": 0,  # 0 = no limit
    },
    "paths": {
        "work_dir": "./synmich_backup",
    },
}


def resolve_shared_space_mode(mig: Optional[Dict[str, Any]]) -> str:
    """Resolve effective Shared Space mode: 'upload', 'external', or 'ignore'."""
    mig = mig or {}
    raw = str(mig.get("shared_space_mode") or "").strip().lower()
    if raw in ("upload", "external", "ignore"):
        return raw
    if not mig.get("include_shared_space", True):
        return "ignore"
    if (mig.get("shared_space_immich_api_key") or "").strip():
        return "external"
    if mig.get("external_library_mode", False):
        return "external"
    return "upload"


def is_user_external_library(
    user_cfg: Optional[Dict[str, Any]],
    mig_cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True if this user's personal photos live in an Immich external library."""
    if isinstance(user_cfg, dict) and "external_library" in user_cfg and user_cfg["external_library"] is not None:
        return bool(user_cfg["external_library"])
    return bool((mig_cfg or {}).get("external_library_mode", False))


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge overlay onto a copy of base (dicts only)."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load YAML config, merged with defaults.

    Missing file → {} so callers can detect first-run. A present file is
    deep-merged onto DEFAULT_CONFIG so older configs pick up new keys
    (execution, filters, albums_mode, …) instead of KeyError.
    """
    if path is None:
        path = get_config_file()
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_CONFIG)
    return _deep_merge(copy.deepcopy(DEFAULT_CONFIG), data)


def save_config(
    data: Dict[str, Any], path: Optional[Path] = None
) -> None:
    """Save config as YAML."""
    if path is None:
        path = get_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(
            data,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )


def validate_config(data: Dict[str, Any]) -> List[str]:
    """Return the list of validation errors."""
    errors = []

    if not data.get("synology", {}).get("url"):
        errors.append("synology.url is missing")
    if not data.get("immich", {}).get("url"):
        errors.append("immich.url is missing")

    users = data.get("users", [])
    if not users:
        errors.append("No user configured")

    for i, u in enumerate(users):
        prefix = f"users[{i}]"
        if not u.get("name"):
            errors.append(f"{prefix}.name is missing")
        if not u.get("syno_username"):
            errors.append(f"{prefix}.syno_username is missing")
        if not u.get("syno_password"):
            errors.append(f"{prefix}.syno_password is missing")
        if not u.get("immich_api_key"):
            errors.append(f"{prefix}.immich_api_key is missing")

    mig = data.get("migration", {})

    # Validate shared_albums_mode
    sam = mig.get("shared_albums_mode", "link")
    if sam not in ("link", "duplicate", "ignore"):
        errors.append(
            f"migration.shared_albums_mode='{sam}' "
            f"must be 'link', 'duplicate', or 'ignore'"
        )

    # Validate albums_mode (which albums to migrate)
    am = mig.get("albums_mode", "all")
    if am not in ("all", "select"):
        errors.append(
            f"migration.albums_mode='{am}' "
            f"must be 'all' or 'select'"
        )

    ssm = (mig.get("shared_space_mode") or "").strip().lower()
    if ssm and ssm not in ("upload", "external", "ignore"):
        errors.append(
            f"migration.shared_space_mode='{ssm}' "
            f"must be 'upload', 'external', or 'ignore'"
        )

    if resolve_shared_space_mode(mig) == "upload":
        owner = mig.get("shared_space_owner")
        if owner and owner not in [u.get("name") for u in users]:
            errors.append(
                f"migration.shared_space_owner='{owner}' "
                f"not found in users"
            )

    return errors
