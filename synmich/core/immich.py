"""Client Immich API."""

import base64
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests_toolbelt import MultipartEncoder

from synmich.core.http_pool import SessionPool


class ImmichError(Exception):
    pass


def sha1_b64(filepath: Path) -> str:
    """SHA1 du fichier en base64 (pour x-immich-checksum)."""
    h = hashlib.sha1()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def iso_utc(ts: Optional[float] = None) -> str:
    if ts is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ImmichClient:
    """Client pour l'API Immich (v1.106+)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        max_retries: int = 5,
        retry_backoff_s: int = 3,
        timeout: int = 60,
    ):
        # Normalize: ensure '/api' suffix
        url = base_url.rstrip("/")
        if not url.endswith("/api"):
            url = url + "/api"
        self.base_url = url
        self.api_key = api_key
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.timeout = timeout
        self._http_pool = SessionPool()
        self._partners_checked = False
        self._partner_ids: List[str] = []
        self._restorable_partner_ids: List[str] = []
        self._partner_fallback_assets: List[Dict[str, Any]] = []
        self.last_device_asset_index: Dict[str, str] = {}

    def close(self) -> None:
        for pid in list(self._restorable_partner_ids):
            try:
                self.set_partner_in_timeline(pid, False)
            except Exception:
                pass
        self._restorable_partner_ids.clear()
        self._http_pool.close()

    def _request(self, method: str, *args, **kwargs):
        # All Immich responses are buffered (uploads still stream their request
        # body), so the lease can end as soon as Session.get/post/etc returns.
        with self._http_pool.session() as session:
            return getattr(session, method)(*args, **kwargs)

    def _retry(self, fn, *args, **kwargs):
        last = None
        for i in range(1, self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except (
                requests.exceptions.RequestException,
                ConnectionError,
            ) as e:
                last = e
                if i < self.max_retries:
                    time.sleep(self.retry_backoff_s * i)
        raise last

    def _headers(self, extra: Optional[Dict] = None) -> Dict:
        h = {"x-api-key": self.api_key}
        if extra:
            h.update(extra)
        return h

    def ping(self) -> bool:
        """Test de connexion."""
        try:
            r = self._request("get",
                f"{self.base_url}/server/ping",
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def me(self) -> Dict[str, Any]:
        def _do():
            r = self._request("get",
                f"{self.base_url}/users/me",
                headers=self._headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()

        return self._retry(_do)

    def list_partners(
        self, direction: str = "shared-with"
    ) -> List[Dict[str, Any]]:
        """Return partner accounts sharing their timeline with this user."""
        try:
            r = self._retry(
                lambda: self._request(
                    "get",
                    f"{self.base_url}/partners",
                    headers=self._headers(),
                    params={"direction": direction},
                    timeout=self.timeout,
                )
            )
            if getattr(r, "status_code", None) != 200:
                return []
            payload = r.json()
            if not isinstance(payload, list):
                return []
            return [
                p
                for p in payload
                if isinstance(p, dict) and isinstance(p.get("id"), str)
            ]
        except Exception:
            return []

    def set_partner_in_timeline(
        self, partner_id: str, in_timeline: bool
    ) -> bool:
        """Update whether a partner's assets are included in timeline/search."""
        try:
            r = self._retry(
                lambda: self._request(
                    "put",
                    f"{self.base_url}/partners/{partner_id}",
                    headers=self._headers(
                        {"Content-Type": "application/json"}
                    ),
                    json={"inTimeline": in_timeline},
                    timeout=self.timeout,
                )
            )
            return getattr(r, "status_code", None) in (200, 201)
        except Exception:
            return False

    def _ensure_partners_searchable(self) -> List[str]:
        """Ensure shared-with partner timelines are visible to /search/metadata.

        Immich's POST /search/metadata filters partner assets via
        `getMyPartnerIds({ timelineEnabled: true })`. When a partner shares
        their timeline with `inTimeline=False` ("Show in timeline" off, viewed
        under Partners), /search/metadata excludes all of that partner's photos.
        Temporarily enabling `inTimeline=True` for the migration run makes
        /search/metadata include them; `close()` restores `inTimeline=False`.
        """
        if self._partners_checked:
            return self._partner_ids
        self._partners_checked = True
        for partner in self.list_partners("shared-with"):
            pid = partner.get("id")
            if not pid:
                continue
            self._partner_ids.append(pid)
            if not partner.get("inTimeline"):
                if self.set_partner_in_timeline(pid, True):
                    self._restorable_partner_ids.append(pid)
        return self._partner_ids

    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata for a single asset by UUID."""
        try:
            r = self._retry(
                lambda: self._request(
                    "get",
                    f"{self.base_url}/assets/{asset_id}",
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            )
            if getattr(r, "status_code", None) != 200:
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _iter_partner_timeline_assets(self, partner_id: str):
        """Yield assets from a partner's timeline buckets as a fallback when
        POST /search/metadata does not return that partner's assets (e.g. if
        the API key lacks `partner.update` or on older Immich servers).
        """
        def _get_buckets(params: Dict[str, Any]):
            return self._retry(
                lambda: self._request(
                    "get",
                    f"{self.base_url}/timeline/buckets",
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout,
                )
            )

        try:
            r = _get_buckets({"userId": partner_id, "size": "MONTH"})
            if getattr(r, "status_code", None) == 400:
                r = _get_buckets({"userId": partner_id})
            if getattr(r, "status_code", None) != 200:
                return
            buckets = r.json()
        except Exception:
            return

        if not isinstance(buckets, list):
            return

        for b in buckets:
            tb = b.get("timeBucket") if isinstance(b, dict) else None
            if not tb:
                continue

            def _get_bucket(params: Dict[str, Any]):
                return self._retry(
                    lambda: self._request(
                        "get",
                        f"{self.base_url}/timeline/bucket",
                        headers=self._headers(),
                        params=params,
                        timeout=self.timeout,
                    )
                )

            try:
                rb = _get_bucket(
                    {"userId": partner_id, "timeBucket": tb, "size": "MONTH"}
                )
                if getattr(rb, "status_code", None) == 400:
                    rb = _get_bucket(
                        {"userId": partner_id, "timeBucket": tb}
                    )
                if getattr(rb, "status_code", None) != 200:
                    continue
                bdata = rb.json()
            except Exception:
                continue

            if isinstance(bdata, list):
                for it in bdata:
                    if isinstance(it, dict) and it.get("id"):
                        yield it
            elif isinstance(bdata, dict):
                ids = bdata.get("id") or []
                if not isinstance(ids, list):
                    continue
                names = bdata.get("originalFileName")
                dtos = (
                    bdata.get("fileCreatedAt")
                    or bdata.get("localDateTime")
                    or []
                )
                libs = bdata.get("libraryId") or []
                paths = bdata.get("originalPath") or []
                if isinstance(names, list) and len(names) == len(ids):
                    for idx, aid in enumerate(ids):
                        dto = dtos[idx] if idx < len(dtos) else None
                        lib = libs[idx] if idx < len(libs) else None
                        path = paths[idx] if idx < len(paths) else None
                        if not lib and not path:
                            asset = self.get_asset(str(aid))
                            if asset and asset.get("id"):
                                asset.setdefault("ownerId", partner_id)
                                yield asset
                                continue
                        yield {
                            "id": aid,
                            "originalFileName": names[idx],
                            "exifInfo": {"dateTimeOriginal": dto},
                            "libraryId": lib or f"partner:{partner_id}",
                            "originalPath": path,
                            "ownerId": partner_id,
                        }
                else:
                    for aid in ids:
                        if not isinstance(aid, str):
                            continue
                        asset = self.get_asset(aid)
                        if asset and asset.get("id"):
                            yield asset

    def list_albums(
        self, shared: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        params = {}
        if shared is not None:
            params["shared"] = "true" if shared else "false"

        def _do():
            r = self._request("get",
                f"{self.base_url}/albums",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()

        return self._retry(_do)

    def get_album(self, album_id: str) -> Dict[str, Any]:
        def _do():
            r = self._request("get",
                f"{self.base_url}/albums/{album_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()

        return self._retry(_do)

    def _find_album_id(self, name: str) -> Optional[str]:
        """Return an existing album id for ``name``, preferring the fullest."""
        matches = [
            a for a in self.list_albums() if a.get("albumName") == name
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda a: a.get("assetCount") or 0,
            reverse=True,
        )
        return matches[0].get("id")

    def create_album(self, name: str) -> str:
        """Create an album and return its id.

        Reuse an existing album of the same name when present. Older Immich
        rejected duplicate names with HTTP 400; current Immich allows two
        albums with the same name, so we must look up first or a second
        empty album is created. A 200 body that is a list (older error
        shape) used to raise ``list indices must be integers or slices,
        not str``.
        """
        existing = self._find_album_id(name)
        if existing:
            return existing

        def _do():
            r = self._request("post",
                f"{self.base_url}/albums",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={"albumName": name},
                timeout=self.timeout,
            )
            if r.status_code == 400:
                return None
            r.raise_for_status()
            try:
                payload = r.json()
            except ValueError as e:
                raise ImmichError(f"invalid JSON creating album '{name}': {e}") from e
            if isinstance(payload, dict) and payload.get("id"):
                return payload["id"]
            raise ImmichError(
                f"Unexpected create-album response for '{name}': "
                f"{payload!r}"[:240]
            )

        album_id = self._retry(_do)
        if album_id:
            return album_id

        existing = self._find_album_id(name)
        if existing:
            return existing
        raise ImmichError(
            f"Album '{name}' already exists on Immich, but the existing "
            f"album could not be found"
        )

    def rename_album(self, album_id: str, name: str) -> bool:
        def _do():
            return self._request("patch",
                f"{self.base_url}/albums/{album_id}",
                headers=self._headers({"Content-Type": "application/json"}),
                json={"albumName": name},
                timeout=self.timeout,
            )

        r = self._retry(_do)
        return r.status_code in (200, 201)

    def delete_album(self, album_id: str) -> bool:
        def _do():
            return self._request("delete",
                f"{self.base_url}/albums/{album_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )

        r = self._retry(_do)
        return r.status_code in [200, 204]

    def share_album(
        self,
        album_id: str,
        user_id: str,
        role: str = "editor",
    ) -> bool:
        """Share with a user. Falls back to legacy schema if needed."""

        def _do_new():
            return self._request("put",
                f"{self.base_url}/albums/{album_id}/users",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={
                    "albumUsers": [
                        {"userId": user_id, "role": role}
                    ]
                },
                timeout=self.timeout,
            )

        r = self._retry(_do_new)
        if r.status_code in [200, 201]:
            return True

        # Legacy schema fallback
        def _do_legacy():
            return self._request("put",
                f"{self.base_url}/albums/{album_id}/users",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={"sharedUserIds": [user_id]},
                timeout=self.timeout,
            )

        r2 = self._retry(_do_legacy)
        return r2.status_code in [200, 201]

    def upload_asset(
        self,
        filepath: Path,
        device_id: str = "synmich",
        device_asset_id: Optional[str] = None,
        file_created_at: Optional[float] = None,
        is_favorite: bool = False,
        original_filename: Optional[str] = None,
        live_photo_video_id: Optional[str] = None,
        visibility: Optional[str] = None,
        expected_type: Optional[str] = None,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """
        Upload a file to Immich.
        Returns (asset_id, is_duplicate, error_msg).

        original_filename: if set, sent to Immich as the asset filename
        (useful when filepath.name is a temp/prefixed name).
        """
        if not filepath.exists():
            return None, False, "file_not_found"

        size = filepath.stat().st_size
        if size == 0:
            return None, False, "empty_file"

        upload_name = original_filename or filepath.name

        if not device_asset_id:
            device_asset_id = f"{upload_name}_{size}"

        checksum = sha1_b64(filepath)
        created = iso_utc(file_created_at)
        modified = iso_utc(filepath.stat().st_mtime)

        def _do():
            # Rebuild the encoder on every attempt: a retry after a
            # partial send cannot rewind a consumed file handle.
            # MultipartEncoder streams from disk. requests `files=` +
            # `data=` would buffer the whole multipart body in RAM and
            # OOM on ~2GB+ videos (issue #10).
            with open(filepath, "rb") as f:
                fields = {
                    "deviceAssetId": device_asset_id,
                    "deviceId": device_id,
                    "fileCreatedAt": created,
                    "fileModifiedAt": modified,
                    "isFavorite": (
                        "true" if is_favorite else "false"
                    ),
                    "assetData": (
                        upload_name,
                        f,
                        "application/octet-stream",
                    ),
                }
                if live_photo_video_id:
                    fields["livePhotoVideoId"] = live_photo_video_id
                if visibility:
                    fields["visibility"] = visibility
                encoder = MultipartEncoder(fields=fields)
                return self._request("post",
                    f"{self.base_url}/assets",
                    headers=self._headers(
                        {
                            "Content-Type": encoder.content_type,
                            "x-immich-checksum": checksum,
                        }
                    ),
                    data=encoder,
                    # Connect 30s; no read cap — multi-GB LAN uploads
                    # routinely exceed the old 300s timeout.
                    timeout=(30, None),
                )

        try:
            r = self._retry(_do)
        except Exception as e:
            return None, False, f"network: {e}"

        if r.status_code not in [200, 201]:
            return (
                None,
                False,
                f"http_{r.status_code}: {r.text[:200]}",
            )

        try:
            d = r.json()
        except Exception:
            return None, False, "invalid_json"

        if live_photo_video_id and d.get("status") == "duplicate" and d.get("id"):
            # The upload deduplication response does not apply linkage fields.
            # Explicitly attach the motion when the still already exists.
            def _link_motion():
                response = self._request("put",
                    f"{self.base_url}/assets/{d['id']}",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"livePhotoVideoId": live_photo_video_id},
                    timeout=self.timeout,
                )
                response.raise_for_status()

            try:
                self._retry(_link_motion)
            except Exception as e:
                return None, False, f"live_photo_link_failed: {e}"

        if expected_type and d.get("status") == "duplicate" and d.get("id"):
            # A previous broken migration may have uploaded MOV bytes under a
            # HEIC filename. Its checksum then deduplicates to an IMAGE asset,
            # which Immich refuses as livePhotoVideoId. Never link it blindly.
            def _get_type():
                response = self._request("get",
                    f"{self.base_url}/assets/{d['id']}",
                    headers=self._headers(), timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json().get("type")

            try:
                actual_type = self._retry(_get_type)
            except Exception as e:
                return None, False, f"duplicate_type_check_failed: {e}"
            if actual_type != expected_type:
                return None, False, (
                    f"duplicate_type_mismatch: expected {expected_type}, got {actual_type}; "
                    f"existing asset {d['id']} needs review in Immich"
                )

        return (
            d.get("id"),
            d.get("status") == "duplicate",
            None,
        )

    def add_assets_to_album(
        self, album_id: str, asset_ids: List[str]
    ) -> Tuple[int, int]:
        """
        Add assets to an album.
        Returns (success_count, fail_count).
        Les 'duplicate' comptent comme success.
        """
        if not asset_ids:
            return 0, 0

        def _do():
            return self._request("put",
                f"{self.base_url}/albums/{album_id}/assets",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={"ids": asset_ids},
                timeout=120,
            )

        try:
            r = self._retry(_do)
        except Exception:
            return 0, len(asset_ids)

        if r.status_code not in [200, 201]:
            return 0, len(asset_ids)

        ok, fail = 0, 0
        try:
            for entry in r.json():
                if entry.get("success") or entry.get(
                    "error"
                ) == "duplicate":
                    ok += 1
                else:
                    fail += 1
        except Exception:
            ok = len(asset_ids)
        return ok, fail

    def remove_assets_from_album(
        self, album_id: str, asset_ids: List[str]
    ) -> bool:
        """Remove assets from an album."""
        if not asset_ids:
            return True

        def _do():
            return self._request(
                "delete",
                f"{self.base_url}/albums/{album_id}/assets",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={"ids": asset_ids},
                timeout=120,
            )

        try:
            r = self._retry(_do)
            return getattr(r, "status_code", None) in (200, 201, 204)
        except Exception:
            return False

    def search_assets(
        self, size: int = 1000
    ) -> Dict[str, Any]:
        """Search assets (used for wipe/verify)."""

        def _do():
            r = self._request("post",
                f"{self.base_url}/search/metadata",
                headers=self._headers(
                    {"Content-Type": "application/json"}
                ),
                json={"size": size},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()

        return self._retry(_do)

    def iter_assets(
        self, page_size: int = 1000, include_partners: bool = True
    ):
        """Yield every asset visible to this API key, across ALL libraries
        (the user's upload library, external libraries, AND timelines shared
        with this user by partners when `include_partners=True`).

        Paginates POST /search/metadata. `withExif` is requested so each
        item carries `exifInfo.dateTimeOriginal`, which external-library
        mode needs to disambiguate same-named photos.

        Why this exists: Immich stores a *dummy* checksum for
        external-library assets — `sha1('path:' + originalPath)`, not a hash
        of the file's bytes (confirmed in immich-app/immich#7804). So a
        content-checksum dedup can never match a file that lives in an
        external library; matching has to be done on metadata instead.
        """
        partner_ids = (
            self._ensure_partners_searchable() if include_partners else []
        )
        seen_ids = set()
        seen_owners = set()
        page = 1
        while True:
            def _do(p=page):
                r = self._request("post",
                    f"{self.base_url}/search/metadata",
                    headers=self._headers(
                        {"Content-Type": "application/json"}
                    ),
                    json={
                        "page": p,
                        "size": page_size,
                    },
                    timeout=self.timeout,
                )
                r.raise_for_status()
                return r.json()

            data = self._retry(_do)
            assets = data.get("assets", {}) or {}
            for it in assets.get("items", []) or []:
                aid = it.get("id")
                if aid:
                    seen_ids.add(aid)
                owner_id = it.get("ownerId")
                if owner_id:
                    seen_owners.add(owner_id)
                yield it
            nxt = assets.get("nextPage")
            if not nxt:
                break
            try:
                page = int(nxt)
            except (TypeError, ValueError):
                break

        # If any shared-with partner's assets were not returned by
        # POST /search/metadata (e.g. `inTimeline` could not be toggled or
        # the server version scopes /search/metadata strictly to the caller),
        # enumerate their timeline buckets directly.
        if partner_ids:
            self._partner_fallback_assets = []
            for pid in partner_ids:
                if pid in seen_owners:
                    continue
                for it in self._iter_partner_timeline_assets(pid):
                    aid = it.get("id")
                    if not aid or aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                    self._partner_fallback_assets.append(it)
                    yield it

    def build_filename_index(
        self, page_size: int = 1000, include_partners: bool = True
    ) -> Dict[str, List[Tuple[str, Optional[str], Optional[str], Optional[str]]]]:
        """Index existing Immich assets by filename for external-library mode.

        Returns
        {originalFileName.lower(): [(asset_id, dateTimeOriginal, libraryId, originalPath), …]}
        covering every asset this key can see (external libraries included).
        Multiple entries per name are kept so the caller can disambiguate by
        capture date when a filename isn't unique, and `libraryId` lets it
        prefer the external asset over a leftover upload-library copy. The
        path lets the matcher break ties when the same external asset exists
        in more than one Immich external library.

        Also populates `self.last_device_asset_index` ({deviceAssetId: asset_id})
        for internal-storage assets (`libraryId is None`) so upload users can
        reuse previously uploaded assets without re-downloading from Synology.
        """
        index: Dict[
            str, List[Tuple[str, Optional[str], Optional[str], Optional[str]]]
        ] = {}
        device_idx: Dict[str, str] = {}
        try:
            assets_iter = self.iter_assets(
                page_size=page_size, include_partners=include_partners
            )
        except TypeError:
            assets_iter = self.iter_assets(page_size=page_size)
        for a in assets_iter:
            aid = a.get("id")
            if not aid:
                continue
            dev_aid = a.get("deviceAssetId")
            if dev_aid and a.get("libraryId") is None:
                device_idx[str(dev_aid)] = str(aid)
            name = a.get("originalFileName")
            if not name:
                continue
            dto = (a.get("exifInfo") or {}).get("dateTimeOriginal") or a.get(
                "fileCreatedAt"
            )
            index.setdefault(name.lower(), []).append(
                (aid, dto, a.get("libraryId"), a.get("originalPath"))
            )
        self.last_device_asset_index = device_idx
        return index

    def find_assets_by_filename(
        self, filename: str, page_size: int = 250
    ) -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
        """Exact-name lookup for a single filename (external-library fallback).

        build_filename_index() paginates the *whole* library with
        POST /search/metadata, which orders by a non-unique key
        (`fileCreatedAt`) and uses offset pagination — so on large libraries
        with many identical timestamps (duplicates, burst shots) it silently
        skips assets at page boundaries, and a file that really is in Immich
        can be missing from the bulk index. This re-checks one filename
        directly: the `originalFileName` filter narrows the DB scan to a
        handful of rows, so the same pagination weakness no longer bites.

        Immich treats `originalFileName` as a substring `ILIKE`, so we
        re-filter the results down to an exact (case-insensitive) name match.
        Returns [(asset_id, dateTimeOriginal, libraryId, originalPath), ...].
        """
        if not filename:
            return []
        self._ensure_partners_searchable()
        target = filename.lower()
        results: List[
            Tuple[str, Optional[str], Optional[str], Optional[str]]
        ] = []
        seen_ids = set()
        page = 1
        while True:
            def _do(p=page):
                r = self._request("post",
                    f"{self.base_url}/search/metadata",
                    headers=self._headers(
                        {"Content-Type": "application/json"}
                    ),
                    json={
                        "page": p,
                        "size": page_size,
                        "originalFileName": filename,
                    },
                    timeout=self.timeout,
                )
                r.raise_for_status()
                return r.json()

            data = self._retry(_do)
            assets = data.get("assets", {}) or {}
            for it in assets.get("items", []) or []:
                name = it.get("originalFileName")
                aid = it.get("id")
                if not name or not aid:
                    continue
                if name.lower() != target:
                    continue
                dto = (it.get("exifInfo") or {}).get(
                    "dateTimeOriginal"
                ) or it.get("fileCreatedAt")
                seen_ids.add(aid)
                results.append(
                    (aid, dto, it.get("libraryId"), it.get("originalPath"))
                )
            nxt = assets.get("nextPage")
            if not nxt:
                break
            try:
                page = int(nxt)
            except (TypeError, ValueError):
                break

        for it in self._partner_fallback_assets:
            name = it.get("originalFileName")
            aid = it.get("id")
            if not name or not aid or aid in seen_ids:
                continue
            if name.lower() != target:
                continue
            dto = (it.get("exifInfo") or {}).get(
                "dateTimeOriginal"
            ) or it.get("fileCreatedAt")
            seen_ids.add(aid)
            results.append(
                (aid, dto, it.get("libraryId"), it.get("originalPath"))
            )
        return results
