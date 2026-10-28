"""Migrator helpers: device_asset_id, album keys, download-fail stats."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synmich.core.checkpoint import Checkpoint
from synmich.core.migrator import (
    Migrator,
    MigrationControl,
    MigrationStats,
    UserSession,
    album_key,
    device_asset_id,
)


def test_device_asset_id_is_stable_and_unique_per_syno_item():
    a = device_asset_id("cyrille", "435425")
    b = device_asset_id("cyrille", "435426")
    assert a == "cyrille_synoid435425"
    assert a != b
    # Same filename+size would previously collide; ids must not.
    assert "watch.jpg" not in a


def test_album_key_shared_uses_passphrase():
    album = {"id": 1, "name": "Trip", "passphrase": "abc"}
    assert album_key("alice", album) == "abc"


def test_album_key_personal_includes_owner():
    album = {"id": 5, "name": "Private"}
    assert album_key("alice", album) == "local-alice-5"
    assert album_key("bob", album) == "local-bob-5"


def _migrator(tmp_path: Path, syno) -> Migrator:
    cp = Checkpoint(path=tmp_path / "cp.json")
    session = UserSession(
        name="cyrille",
        syno=syno,
        immich=MagicMock(),
        syno_user_id=1,
        immich_user_id="immich-1",
    )
    return Migrator(
        config={"migration": {}, "execution": {"mode": "sequential"}},
        sessions=[session],
        checkpoint=cp,
        stats=MigrationStats(),
        control=MigrationControl(),
    )


def test_download_failure_increments_failed_and_logs(tmp_path):
    syno = MagicMock()
    syno.download.return_value = None
    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    item = {"id": 42, "filename": "watch.jpg", "type": "photo"}

    asset_id, name = mig._process_one_asset(item, tmp_path, uploader)

    assert asset_id is None
    assert name == "cyrille"
    assert mig.stats.failed == 1
    assert mig.checkpoint.data["failed_items"]["cyrille"]["42"] == "download_failed"
    assert any("download failed" in m for m in mig.stats.last_messages)


def test_date_filter_skips_out_of_range(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.min_ts = 1_700_000_000.0
    mig.max_ts = 1_800_000_000.0
    item = {"id": 1, "filename": "old.jpg", "time": 1_000_000_000}
    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])
    assert asset_id is None
    assert mig.stats.skipped == 1
    syno.download.assert_not_called()


_LIVE_ITEM = {
    "id": 12534,
    "filename": "IMG_7688.HEIC",
    "type": "live",
    "time": 1_700_000_000,
    "additional": {
        "thumbnail": {
            "unit_id": 12542,
            "cache_key": "12542_1778357880",
        }
    },
}


def test_item_timestamp_normalizes_milliseconds(tmp_path):
    mig = _migrator(tmp_path, MagicMock())
    assert mig._item_timestamp({"time": 1_700_000_000}) == 1_700_000_000
    assert mig._item_timestamp({"time": 1_700_000_000_000}) == 1_700_000_000
    assert mig._item_timestamp({"indexed_time": 1_700_000_000_000}) == 1_700_000_000


def test_live_photo_uploads_motion_then_still_with_link(tmp_path):
    syno = MagicMock()
    still = tmp_path / "still.HEIC"
    motion = tmp_path / "motion.MOV"
    still.write_bytes(b"heic")
    motion.write_bytes(b"mov")
    syno.download_live_photo.return_value = (still, motion)

    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.side_effect = [
        ("motion-id", False, None),
        ("still-id", False, None),
    ]

    asset_id, name = mig._process_one_asset(_LIVE_ITEM, tmp_path, uploader)

    assert asset_id == "still-id"
    assert name == "cyrille"
    syno.download_live_photo.assert_called_once()
    syno.download.assert_not_called()
    calls = uploader.immich.upload_asset.call_args_list
    assert calls[0].kwargs["visibility"] == "hidden"
    assert calls[0].kwargs["device_asset_id"] == "cyrille_synoid12534_motion"
    assert calls[1].kwargs["live_photo_video_id"] == "motion-id"
    assert calls[1].kwargs["device_asset_id"] == "cyrille_synoid12534"
    assert calls[1].kwargs["file_created_at"] == 1_700_000_000
    assert mig.stats.uploaded == 1
    assert mig.checkpoint.is_uploaded("cyrille", "12534") == "still-id"


def test_duplicate_linked_jpeg_uses_regular_download(tmp_path):
    syno = MagicMock()
    jpeg = tmp_path / "watch.jpg"
    jpeg.write_bytes(b"jpeg")
    syno.download.return_value = jpeg

    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.return_value = ("imm-1", False, None)
    item = {
        "id": 435425,
        "filename": "watch.jpg",
        "type": "photo",
        "time": 1_700_000_000,
        "additional": {"thumbnail": {"unit_id": 435419}},
    }

    asset_id, _ = mig._process_one_asset(item, tmp_path, uploader)

    assert asset_id == "imm-1"
    syno.download.assert_called_once()
    syno.download_live_photo.assert_not_called()


def test_live_photo_not_skipped_when_videos_disabled(tmp_path):
    syno = MagicMock()
    still = tmp_path / "still.HEIC"
    still.write_bytes(b"heic")
    motion = tmp_path / "motion.MOV"
    motion.write_bytes(b"mov")
    syno.download_live_photo.return_value = (still, motion)

    mig = _migrator(tmp_path, syno)
    mig.include_videos = False
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.side_effect = [
        ("motion-id", False, None), ("still-id", False, None)
    ]

    asset_id, _ = mig._process_one_asset(_LIVE_ITEM, tmp_path, uploader)
    assert asset_id == "still-id"
    syno.download_live_photo.assert_called_once()


def test_video_skipped_when_videos_disabled(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.include_videos = False
    item = {"id": 9, "filename": "clip.mp4", "type": "video"}
    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])
    assert asset_id is None
    assert mig.stats.skipped == 1
    syno.download.assert_not_called()
    syno.download_live_photo.assert_not_called()


def test_live_still_download_failure_counts_as_failed(tmp_path):
    syno = MagicMock()
    syno.download_live_photo.return_value = (None, None)
    mig = _migrator(tmp_path, syno)
    asset_id, _ = mig._process_one_asset(
        _LIVE_ITEM, tmp_path, mig.sessions[0]
    )
    assert asset_id is None
    assert mig.stats.failed == 1
    assert mig.checkpoint.data["failed_items"]["cyrille"]["12534"] == (
        "download_failed"
    )


def test_external_library_link_skips_download(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    mig._asset_index["cyrille"] = {
        "watch.jpg": [("imm-ext", None, "lib-1", "/homes/cyrille/watch.jpg")]
    }
    item = {"id": 42, "filename": "watch.jpg", "type": "photo"}

    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])

    assert asset_id == "imm-ext"
    assert mig.stats.linked == 1
    assert mig.checkpoint.is_uploaded("cyrille", "42") == "imm-ext"
    syno.download.assert_not_called()
    syno.download_live_photo.assert_not_called()
    mig.sessions[0].immich.upload_asset.assert_not_called()


def test_external_library_falls_through_to_upload_when_no_match(tmp_path):
    syno = MagicMock()
    jpeg = tmp_path / "watch.jpg"
    jpeg.write_bytes(b"jpeg")
    syno.download.return_value = jpeg

    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    mig._asset_index["cyrille"] = {}
    mig.sessions[0].immich.find_assets_by_filename.return_value = []
    mig.external_library_fallback_upload = True
    mig.sessions[0].immich.upload_asset.return_value = ("imm-new", False, None)
    item = {"id": 42, "filename": "watch.jpg", "type": "photo"}

    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])

    assert asset_id == "imm-new"
    syno.download.assert_called_once()
    assert mig.stats.uploaded == 1
    assert mig.stats.linked == 0


def test_external_match_accepts_millisecond_synology_time(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    mig._asset_index["cyrille"] = {
        "a.jpg": [
            ("ext-old", "2023-10-01T00:00:00+00:00", "lib-a", "/a-old.jpg"),
            ("ext-hit", "2023-11-14T22:13:20+00:00", "lib-a", "/a.jpg"),
        ]
    }
    mig.sessions[0].immich.find_assets_by_filename.return_value = []
    item = {
        "id": 7,
        "filename": "a.jpg",
        "type": "photo",
        "time": 1_700_000_000_000,  # ms for 2023-11-14 22:13:20 UTC
    }

    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])
    assert asset_id == "ext-hit"
    syno.download.assert_not_called()


def test_shared_space_skipped_when_disabled(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.include_shared_space = False
    mig.run_shared_space()
    syno.list_shared_space_items.assert_not_called()


def test_already_uploaded_is_resumed_without_redownload(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.checkpoint.mark_uploaded("cyrille", "42", "imm-old")
    item = {"id": 42, "filename": "watch.jpg", "type": "photo"}
    asset_id, _ = mig._process_one_asset(item, tmp_path, mig.sessions[0])
    assert asset_id == "imm-old"
    syno.download.assert_not_called()


@pytest.mark.parametrize("lookup_error", [False, True])
def test_external_library_no_match_never_uploads_by_default(tmp_path, lookup_error):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    uploader = mig.sessions[0]
    uploader.immich.build_filename_index.side_effect = RuntimeError("index unavailable")
    if lookup_error:
        uploader.immich.find_assets_by_filename.side_effect = RuntimeError("search unavailable")
    else:
        uploader.immich.find_assets_by_filename.return_value = []
    item = {"id": 42, "filename": "IMG_0018.JPG", "type": "photo"}

    assert mig._process_one_asset(item, tmp_path, uploader)[0] is None
    assert mig.stats.failed == 1
    assert mig.checkpoint.data["failed_items"]["cyrille"]["42"] == "external_match_not_found"
    assert mig.checkpoint.is_uploaded("cyrille", "42") is None
    syno.download.assert_not_called()
    uploader.immich.upload_asset.assert_not_called()


def test_external_library_targeted_lookup_recovers_index_miss(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    mig._asset_index["cyrille"] = {}
    uploader = mig.sessions[0]
    uploader.immich.find_assets_by_filename.return_value = [("ext", None, "lib", "/a.jpg")]
    item = {"id": 42, "filename": "a.jpg", "type": "photo"}
    assert mig._process_one_asset(item, tmp_path, uploader)[0] == "ext"
    syno.download.assert_not_called()
    uploader.immich.upload_asset.assert_not_called()


@pytest.mark.parametrize("duplicate", [False, True])
def test_album_with_unmatched_external_asset_is_retried(tmp_path, duplicate):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    if duplicate:
        mig.shared_albums_mode = "duplicate"
    uploader = mig.sessions[0]
    uploader.immich.create_album.return_value = "album-id"
    uploader.immich.find_assets_by_filename.return_value = []
    uploader.immich.build_filename_index.return_value = {}
    uploader.immich.add_assets_to_album.return_value = (1, 0)
    syno.list_items.return_value = [
        {"id": 42, "filename": "a.jpg", "type": "photo", "owner_user_id": 1}
    ]
    album = {"id": 3, "name": "Trip", "owner_user_id": 1, "shared": duplicate}
    mig._process_album(album)
    assert not mig.checkpoint.is_album_done("local-cyrille-3")

    # A completed library scan makes the next run succeed without uploading.
    mig._fallback_cache.clear()
    uploader.immich.find_assets_by_filename.return_value = [("ext", None, "lib", "/a.jpg")]
    mig._process_album(album)
    assert mig.checkpoint.is_album_done("local-cyrille-3")
    assert mig.checkpoint.is_uploaded("cyrille", "42") == "ext"
    uploader.immich.upload_asset.assert_not_called()


def test_ambiguous_external_filename_is_reported_without_upload(tmp_path):
    mig = _migrator(tmp_path, MagicMock())
    mig.external_library_mode = True
    candidates = [
        ("old", "2020-01-01T00:00:00Z", "lib"),
        ("new", "2025-01-01T00:00:00Z", "lib"),
    ]
    mig._asset_index["cyrille"] = {"a.jpg": candidates}
    uploader = mig.sessions[0]
    uploader.immich.find_assets_by_filename.return_value = candidates
    item = {"id": 42, "filename": "a.jpg", "type": "photo", "time": 1_700_000_000}
    assert mig._process_one_asset(item, tmp_path, uploader)[0] is None
    assert mig.stats.failed == 1
    uploader.immich.upload_asset.assert_not_called()


def test_missing_live_motion_remains_retryable(tmp_path):
    syno = MagicMock()
    still = tmp_path / "still.HEIC"
    still.write_bytes(b"heic")
    syno.download_live_photo.return_value = (still, None)
    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    assert mig._process_one_asset(_LIVE_ITEM, tmp_path, uploader)[0] is None
    assert mig.stats.failed == 1
    assert mig.checkpoint.is_uploaded("cyrille", "12534") is None
    assert still.exists()
    uploader.immich.upload_asset.assert_not_called()


def test_failed_motion_upload_does_not_complete_or_delete_live_photo(tmp_path):
    syno = MagicMock()
    still = tmp_path / "still.HEIC"
    motion = tmp_path / "motion.MOV"
    still.write_bytes(b"heic")
    motion.write_bytes(b"mov")
    syno.download_live_photo.return_value = (still, motion)
    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.return_value = (None, False, "duplicate_type_mismatch")
    assert mig._process_one_asset(_LIVE_ITEM, tmp_path, uploader)[0] is None
    assert mig.stats.failed == 1
    assert mig.checkpoint.is_uploaded("cyrille", "12534") is None
    assert uploader.immich.upload_asset.call_count == 1
    assert uploader.immich.upload_asset.call_args.kwargs["expected_type"] == "VIDEO"
    assert still.exists() and motion.exists()


def test_failed_live_photo_in_old_completed_album_is_retried(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    uploader = mig.sessions[0]
    uploader.immich.create_album.return_value = "album"
    uploader.immich.add_assets_to_album.return_value = (1, 0)
    mig._asset_index["cyrille"] = {"a.jpg": [("ext", None, "lib")]}
    syno.list_items.return_value = [
        {"id": 42, "filename": "a.jpg", "owner_user_id": 1}
    ]
    mig.checkpoint.mark_failed("cyrille", "42", "download_failed")
    mig.checkpoint.mark_album_done("local-cyrille-3")
    mig._process_album({"id": 3, "name": "Trip", "owner_user_id": 1})
    assert mig.checkpoint.is_uploaded("cyrille", "42") == "ext"
    assert not mig.checkpoint.has_failed_items()
    assert mig.stats.linked == 1


def test_live_item_named_mov_uploads_still_with_image_extension(tmp_path):
    syno = MagicMock()
    still = tmp_path / "12534_IMG_7688.HEIC"
    motion = tmp_path / "12534_IMG_7688.MOV"
    still.write_bytes(b"heic")
    motion.write_bytes(b"mov")
    syno.download_live_photo.return_value = (still, motion)
    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.side_effect = [("motion", False, None), ("still", False, None)]
    mig._process_one_asset({**_LIVE_ITEM, "filename": "IMG_7688.MOV"}, tmp_path, uploader)
    assert uploader.immich.upload_asset.call_args.kwargs["original_filename"] == "IMG_7688.HEIC"


def test_transfer_timing_covers_download_and_upload(tmp_path):
    from unittest.mock import patch
    syno = MagicMock()
    path = tmp_path / "image.JPG"
    path.write_bytes(b"jpeg")
    syno.download.return_value = path
    mig = _migrator(tmp_path, syno)
    uploader = mig.sessions[0]
    uploader.immich.upload_asset.return_value = ("id", False, None)
    with patch("synmich.core.migrator.time.perf_counter", side_effect=[1.0, 3.0, 4.0, 9.0]):
        mig._process_one_asset({"id": 42, "filename": "image.JPG"}, tmp_path, uploader)
    assert mig.stats.download_seconds == 2.0
    assert mig.stats.upload_seconds == 5.0
    assert mig.stats.downloaded_bytes == 4


def test_migration_closes_connections_even_on_failure(tmp_path):
    from unittest.mock import patch
    mig = _migrator(tmp_path, MagicMock())
    with patch.object(mig, "run_albums", side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError):
            mig.run()
    mig.sessions[0].syno.close_downloads.assert_called_once()
    mig.sessions[0].immich.close.assert_called_once()
    assert any("Transfer totals" in msg for msg in mig.stats.last_messages)


def test_eta_and_rate_reset_when_starting_another_album():
    from unittest.mock import patch
    stats = MigrationStats(start_time=1.0, items_total=100, items_done=100)
    with patch("synmich.core.migrator.time.time", return_value=3600.0):
        stats.begin_items(100)
    stats.items_done = 10
    with patch("synmich.core.migrator.time.time", return_value=3601.0):
        assert stats.elapsed_seconds() == 3600.0
        assert stats.eta_seconds() == 9.0
        assert stats.rate_per_minute() == 600.0


def test_partner_owned_synology_items_and_albums_fallback_to_shared_owner(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    uploader = mig.sessions[0]
    uploader.immich.create_album.return_value = "album-partner"
    uploader.immich.add_assets_to_album.return_value = (1, 0)
    mig._asset_index["cyrille"] = {
        "img_1234.heic": [
            ("partner-asset-1", "2023-11-14T22:13:20+00:00", "lib-partner", "/homes/partner/IMG_1234.HEIC")
        ]
    }
    # owner_user_id=2 belongs to the unconfigured partner on Synology
    syno.list_items.return_value = [
        {"id": 99, "filename": "IMG_1234.HEIC", "type": "photo", "owner_user_id": 2, "time": 1_700_000_000}
    ]
    album = {"id": 10, "name": "Partner Album", "owner_user_id": 2, "shared": True, "passphrase": "p1"}
    mig._process_album(album)

    assert mig.checkpoint.is_album_done("p1")
    assert mig.checkpoint.is_uploaded("cyrille", "99") == "partner-asset-1"
    uploader.immich.add_assets_to_album.assert_called_once_with("album-partner", ["partner-asset-1"])


def test_wrong_date_bulk_index_candidate_falls_through_to_targeted_lookup(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    uploader = mig.sessions[0]
    # Bulk index only had user's own photo with same name from 2021
    mig._asset_index["cyrille"] = {
        "img_1234.heic": [
            ("own-wrong-date", "2021-01-01T00:00:00+00:00", "lib-own", "/homes/cyrille/IMG_1234.HEIC")
        ]
    }
    # Targeted lookup returns partner's photo with the matching 2023 date
    uploader.immich.find_assets_by_filename.return_value = [
        ("own-wrong-date", "2021-01-01T00:00:00+00:00", "lib-own", "/homes/cyrille/IMG_1234.HEIC"),
        ("partner-hit", "2023-11-14T22:13:20+00:00", "lib-partner", "/homes/partner/IMG_1234.HEIC"),
    ]
    item = {"id": 77, "filename": "IMG_1234.HEIC", "type": "photo", "time": 1_700_000_000}

    asset_id, _ = mig._process_one_asset(item, tmp_path, uploader)
    assert asset_id == "partner-hit"
    assert mig.checkpoint.is_uploaded("cyrille", "77") == "partner-hit"


def test_completed_album_with_false_matched_random_photo_is_repaired(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.external_library_mode = True
    uploader = mig.sessions[0]
    uploader.immich.create_album.return_value = "album-1"
    uploader.immich.add_assets_to_album.return_value = (1, 0)
    uploader.immich.remove_assets_from_album.return_value = 1

    # Previously, syno item 88 was false-matched to "own-wrong-date" and album marked done
    mig.checkpoint.mark_linked("cyrille", "88", "own-wrong-date")
    mig.checkpoint.mark_album_done("p-repair")

    # Now index has both the wrong-date own photo and the right-date partner photo
    mig._asset_index["cyrille"] = {
        "img_5555.heic": [
            ("own-wrong-date", "2020-05-01T00:00:00+00:00", "lib-own", "/homes/cyrille/IMG_5555.HEIC"),
            ("partner-right-date", "2023-11-14T22:13:20+00:00", "lib-partner", "/homes/partner/IMG_5555.HEIC"),
        ]
    }
    syno.list_items.return_value = [
        {"id": 88, "filename": "IMG_5555.HEIC", "type": "photo", "owner_user_id": 2, "time": 1_700_000_000}
    ]
    album = {"id": 12, "name": "Shared Trip", "owner_user_id": 1, "shared": True, "passphrase": "p-repair"}

    mig._process_album(album)

    assert mig.checkpoint.is_uploaded("cyrille", "88") == "partner-right-date"
    uploader.immich.remove_assets_from_album.assert_called_once_with("album-1", ["own-wrong-date"])
    uploader.immich.add_assets_to_album.assert_called_once_with("album-1", ["partner-right-date"])
    assert mig.checkpoint.is_album_done("p-repair")


def test_dry_run_counts_items_without_persisting_checkpoint(tmp_path):
    syno = MagicMock()
    mig = _migrator(tmp_path, syno)
    mig.dry_run = True
    mig.include_shared_space = True
    syno.list_albums.return_value = [{"id": 3, "name": "Trip", "owner_user_id": 1}]
    syno.list_items.side_effect = [
        [{"id": 10, "filename": "a.jpg", "owner_user_id": 1}],  # album
        [{"id": 20, "filename": "b.jpg", "owner_user_id": 1}],  # timeline
    ]
    syno.list_shared_space_items.return_value = [
        {"id": 30, "filename": "c.jpg", "owner_user_id": 1}
    ]

    mig.run()

    assert mig.stats.albums_done == 1
    assert mig.stats.uploaded == 3
    assert not mig.checkpoint.is_album_done("local-cyrille-3")
    assert not mig.checkpoint.is_shared_space_done()
    assert not mig.checkpoint.is_timeline_done("cyrille")
    assert not mig.checkpoint.path.exists()
    syno.download.assert_not_called()
    mig.sessions[0].immich.create_album.assert_not_called()
    mig.sessions[0].immich.upload_asset.assert_not_called()


def test_config_validation_for_hybrid_and_shared_space_modes():
    from synmich.config import (
        is_user_external_library,
        resolve_shared_space_mode,
        validate_config,
    )

    base = {
        "synology": {"url": "http://192.168.1.10:5000"},
        "immich": {"url": "http://192.168.1.20:2283"},
        "users": [
            {
                "name": "maria",
                "syno_username": "maria",
                "syno_password": "pw",
                "immich_api_key": "k1",
                "external_library": False,
            },
            {
                "name": "ext1",
                "syno_username": "ext1",
                "syno_password": "pw",
                "immich_api_key": "k2",
                "external_library": True,
            },
        ],
        "migration": {
            "albums_mode": "all",
            "shared_albums_mode": "link",
            "shared_space_mode": "external",
            "shared_space_owner": "",
            "shared_space_immich_api_key": "k-shared-6th",
            "external_library_mode": False,
        },
    }
    assert validate_config(base) == []
    assert resolve_shared_space_mode(base["migration"]) == "external"
    assert is_user_external_library(base["users"][0], base["migration"]) is False
    assert is_user_external_library(base["users"][1], base["migration"]) is True

    # When shared_space_mode is upload, shared_space_owner must be a valid user if set
    upload_cfg = {
        **base,
        "migration": {
            **base["migration"],
            "shared_space_mode": "upload",
            "shared_space_owner": "nonexistent",
        },
    }
    errs = validate_config(upload_cfg)
    assert any("shared_space_owner" in e for e in errs)


def test_hybrid_users_timeline_skips_external_and_uploads_internal(tmp_path):
    syno_ext = MagicMock()
    syno_int = MagicMock()
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"jpg")
    syno_int.download.return_value = photo
    syno_int.list_items.return_value = [
        {"id": 101, "filename": "p.jpg", "type": "photo", "owner_user_id": 2}
    ]

    ext_user = UserSession(
        name="ext_user",
        syno=syno_ext,
        immich=MagicMock(),
        syno_user_id=1,
        immich_user_id="imm-ext",
        external_library=True,
    )
    int_user = UserSession(
        name="maria",
        syno=syno_int,
        immich=MagicMock(),
        syno_user_id=2,
        immich_user_id="imm-maria",
        external_library=False,
    )
    int_user.immich.upload_asset.return_value = ("imm-asset-101", False, None)

    cp = Checkpoint(path=tmp_path / "cp.json")
    mig = Migrator(
        config={
            "migration": {"external_library_mode": False},
            "execution": {"mode": "sequential"},
        },
        sessions=[ext_user, int_user],
        checkpoint=cp,
        stats=MigrationStats(),
        control=MigrationControl(),
    )

    mig.run_timeline()

    syno_ext.list_items.assert_not_called()
    syno_int.list_items.assert_called_once()
    assert cp.is_uploaded("maria", "101") == "imm-asset-101"
    assert cp.is_timeline_done("maria")


def test_shared_space_external_mode_links_via_sixth_user_and_skips_upload(tmp_path):
    syno_maria = MagicMock()
    photo = tmp_path / "maria_own.jpg"
    photo.write_bytes(b"jpg")
    syno_maria.download.return_value = photo

    maria = UserSession(
        name="maria",
        syno=syno_maria,
        immich=MagicMock(),
        syno_user_id=1,
        immich_user_id="imm-maria",
        external_library=False,
    )
    maria.immich.create_album.return_value = "alb-maria-1"
    maria.immich.add_assets_to_album.return_value = (2, 0)
    maria.immich.upload_asset.return_value = ("imm-maria-own", False, None)

    shared_immich = MagicMock()
    shared_immich.build_filename_index.return_value = {
        "shared_pic.jpg": [
            ("imm-shared-999", "2023-11-14T22:13:20+00:00", "lib-shared", "/shared/shared_pic.jpg")
        ]
    }

    cp = Checkpoint(path=tmp_path / "cp.json")
    mig = Migrator(
        config={
            "migration": {
                "shared_space_mode": "external",
                "external_library_mode": False,
            },
            "execution": {"mode": "sequential"},
        },
        sessions=[maria],
        checkpoint=cp,
        stats=MigrationStats(),
        control=MigrationControl(),
        shared_space_immich=shared_immich,
    )

    # Album owned by maria (internal upload user) containing:
    # 1) maria's personal photo (owner_user_id=1 -> uploaded)
    # 2) a Shared Space photo (owner_user_id=0 -> linked from 6th user's external library)
    syno_maria.list_items.return_value = [
        {"id": 10, "filename": "maria_own.jpg", "type": "photo", "owner_user_id": 1, "time": 1_700_000_000},
        {"id": 20, "filename": "shared_pic.jpg", "type": "photo", "owner_user_id": 0, "time": 1_700_000_000},
    ]
    album = {"id": 5, "name": "Mixed Album", "owner_user_id": 1, "shared": False}
    mig._process_album(album)

    assert mig.stats.uploaded == 1
    assert mig.stats.linked == 1
    assert cp.is_uploaded("maria", "10") == "imm-maria-own"
    assert cp.is_uploaded("__shared_space__", "20") == "imm-shared-999"
    maria.immich.add_assets_to_album.assert_called_once_with(
        "alb-maria-1", ["imm-maria-own", "imm-shared-999"]
    )

    # run_shared_space() must skip uploading when shared_space_mode == "external"
    mig.run_shared_space()
    syno_maria.list_shared_space_items.assert_not_called()


def test_internal_user_reuses_existing_immich_assets_without_redownload(tmp_path):
    syno_maria = MagicMock()
    maria = UserSession(
        name="maria",
        syno=syno_maria,
        immich=MagicMock(),
        syno_user_id=1,
        immich_user_id="imm-maria",
        external_library=False,
    )
    # Maria already has assets in Immich internal storage from previous testing:
    # - item 501 matched by deviceAssetId ("maria_synoid501")
    # - item 502 matched by filename + timestamp in internal storage (libraryId=None)
    maria.immich.build_filename_index.return_value = {
        "already_there.jpg": [
            ("imm-existing-502", "2023-11-14T22:13:20+00:00", None, "/upload/already_there.jpg")
        ]
    }
    maria.immich.last_device_asset_index = {
        "maria_synoid501": "imm-existing-501",
        "Maria_synoid503": "imm-existing-live-503",
        "synoid:503": "imm-existing-live-503",
    }

    cp = Checkpoint(path=tmp_path / "cp.json")
    mig = Migrator(
        config={"migration": {}, "execution": {"mode": "sequential"}},
        sessions=[maria],
        checkpoint=cp,
        stats=MigrationStats(),
        control=MigrationControl(),
    )

    id1, _ = mig._process_one_asset(
        {"id": 501, "filename": "any.jpg", "type": "photo", "owner_user_id": 1},
        tmp_path,
        maria,
    )
    id2, _ = mig._process_one_asset(
        {"id": 502, "filename": "already_there.jpg", "type": "photo", "owner_user_id": 1, "time": 1_700_000_000},
        tmp_path,
        maria,
    )
    id3, _ = mig._process_one_asset(
        {"id": 503, "filename": "live_photo.heic", "type": "live", "owner_user_id": 1, "time": 1_700_000_000},
        tmp_path,
        maria,
    )

    assert id1 == "imm-existing-501"
    assert id2 == "imm-existing-502"
    assert id3 == "imm-existing-live-503"
    assert mig.stats.duplicate == 3
    syno_maria.download.assert_not_called()
    syno_maria.download_live_photo.assert_not_called()
    maria.immich.upload_asset.assert_not_called()


def test_checkpoint_seeds_user_mappings_when_adding_more_users(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNMICH_CONFIG_DIR", str(tmp_path))

    cfg_one_user = {
        "synology": {"url": "http://192.168.1.10:5000"},
        "immich": {"url": "http://192.168.1.20:2283"},
        "users": [{"name": "maria", "syno_username": "maria"}],
    }
    cp1 = Checkpoint(config=cfg_one_user)
    cp1.mark_uploaded("maria", "777", "imm-maria-777")
    cp1.mark_timeline_done("maria")
    cp1.mark_album_done("local-maria-1")
    cp1.save()

    # Now expand config to 5 users on the same servers (and rename display name
    # 'maria' -> 'mariapaz' via syno_username) -> new checkpoint hash
    cfg_five_users = {
        "synology": {"url": "http://192.168.1.10:5000"},
        "immich": {"url": "http://192.168.1.20:2283"},
        "users": [
            {"name": "mariapaz", "syno_username": "mariapaz"},
            {"name": "u2", "syno_username": "u2"},
            {"name": "u3", "syno_username": "u3"},
            {"name": "u4", "syno_username": "u4"},
            {"name": "u5", "syno_username": "u5"},
        ],
    }
    cp2 = Checkpoint(config=cfg_five_users)
    assert cp2.path != cp1.path
    assert cp2.is_uploaded("mariapaz", "777") == "imm-maria-777"
    assert cp2.is_timeline_done("mariapaz")
    assert not cp2.is_album_done("local-maria-1")


