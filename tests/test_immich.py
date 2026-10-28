"""Immich create_album handles duplicate names and list payloads."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from requests_toolbelt import MultipartEncoder

from synmich.core.immich import ImmichClient, ImmichError, sha1_b64


def test_create_album_returns_id():
    client = ImmichClient("http://immich:2283", "key")
    listed = MagicMock(status_code=200)
    listed.json.return_value = []
    listed.raise_for_status = MagicMock()
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": "alb-1"}
    resp.raise_for_status = MagicMock()
    with patch("synmich.core.immich.requests.Session.get", return_value=listed):
        with patch("synmich.core.immich.requests.Session.post", return_value=resp):
            assert client.create_album("Trip") == "alb-1"


def test_create_album_reuses_existing_before_post():
    client = ImmichClient("http://immich:2283", "key")
    listed = MagicMock(status_code=200)
    listed.json.return_value = [
        {"id": "empty", "albumName": "Trip", "assetCount": 0},
        {"id": "full", "albumName": "Trip", "assetCount": 5},
    ]
    listed.raise_for_status = MagicMock()
    with patch("synmich.core.immich.requests.Session.get", return_value=listed):
        with patch("synmich.core.immich.requests.Session.post") as post:
            assert client.create_album("Trip") == "full"
            post.assert_not_called()


def test_create_album_reuses_existing_on_400():
    client = ImmichClient("http://immich:2283", "key")
    created = MagicMock(status_code=400)
    empty = MagicMock(status_code=200)
    empty.json.return_value = []
    empty.raise_for_status = MagicMock()
    listed = MagicMock(status_code=200)
    listed.json.return_value = [{"id": "alb-9", "albumName": "Trip"}]
    listed.raise_for_status = MagicMock()
    with patch("synmich.core.immich.requests.Session.post", return_value=created):
        with patch(
            "synmich.core.immich.requests.Session.get",
            side_effect=[empty, listed],
        ):
            assert client.create_album("Trip") == "alb-9"


def test_create_album_rejects_list_payload():
    client = ImmichClient("http://immich:2283", "key")
    listed = MagicMock(status_code=200)
    listed.json.return_value = []
    listed.raise_for_status = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = [{"error": "bad"}]
    resp.raise_for_status = MagicMock()
    with patch("synmich.core.immich.requests.Session.get", return_value=listed):
        with patch("synmich.core.immich.requests.Session.post", return_value=resp):
            with pytest.raises(ImmichError, match="Unexpected create-album"):
                client.create_album("Album Name")


def test_upload_asset_sends_live_photo_fields(tmp_path):
    filepath = tmp_path / "IMG_7688.HEIC"
    filepath.write_bytes(b"image")
    response = MagicMock(status_code=201)
    response.json.return_value = {"id": "still-id", "status": "created"}

    with patch("synmich.core.immich.requests.Session.post", return_value=response) as post:
        result = ImmichClient("https://immich.example", "key").upload_asset(
            filepath,
            live_photo_video_id="motion-id",
            visibility="hidden",
        )

    assert result == ("still-id", False, None)
    encoder = post.call_args.kwargs["data"]
    assert isinstance(encoder, MultipartEncoder)
    assert encoder.fields["livePhotoVideoId"] == "motion-id"
    assert encoder.fields["visibility"] == "hidden"


def test_upload_asset_streams_multipart_instead_of_buffering(tmp_path):
    filepath = tmp_path / "clip.mp4"
    filepath.write_bytes(b"video-bytes")
    response = MagicMock(status_code=201)
    response.json.return_value = {"id": "vid-1", "status": "created"}

    with patch("synmich.core.immich.requests.Session.post", return_value=response) as post:
        result = ImmichClient("https://immich.example", "key").upload_asset(
            filepath,
            device_id="synmich",
            device_asset_id="user_synoid1",
        )

    assert result == ("vid-1", False, None)
    kwargs = post.call_args.kwargs
    assert "files" not in kwargs
    encoder = kwargs["data"]
    assert isinstance(encoder, MultipartEncoder)
    assert hasattr(encoder, "read")
    assert kwargs["headers"]["x-immich-checksum"] == sha1_b64(filepath)
    assert kwargs["headers"]["Content-Type"] == encoder.content_type
    assert "multipart/form-data" in encoder.content_type
    assert kwargs["timeout"] == (30, None)
    name, _fileobj, ctype = encoder.fields["assetData"]
    assert name == "clip.mp4"
    assert ctype == "application/octet-stream"
    assert encoder.fields["deviceAssetId"] == "user_synoid1"
    assert encoder.fields["isFavorite"] == "false"


def test_upload_asset_rebuilds_encoder_on_retry(tmp_path):
    filepath = tmp_path / "clip.mp4"
    filepath.write_bytes(b"video-bytes")
    ok = MagicMock(status_code=201)
    ok.json.return_value = {"id": "vid-2", "status": "created"}
    client = ImmichClient(
        "https://immich.example",
        "key",
        retry_backoff_s=0,
    )

    with patch(
        "synmich.core.immich.requests.Session.post",
        side_effect=[requests.exceptions.ConnectionError("boom"), ok],
    ) as post:
        result = client.upload_asset(filepath)

    assert result == ("vid-2", False, None)
    assert post.call_count == 2
    assert isinstance(post.call_args.kwargs["data"], MultipartEncoder)


@pytest.mark.parametrize("asset_type", ["VIDEO", "IMAGE"])
def test_motion_duplicate_type_is_checked_before_linking(tmp_path, asset_type):
    filepath = tmp_path / "motion.MOV"
    filepath.write_bytes(b"video")
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "old-asset", "status": "duplicate"}
    detail = MagicMock(status_code=200)
    detail.json.return_value = {"id": "old-asset", "type": asset_type}
    with patch("synmich.core.immich.requests.Session.post", return_value=response):
        with patch("synmich.core.immich.requests.Session.get", return_value=detail) as get:
            result = ImmichClient("https://immich.example", "key").upload_asset(
                filepath, expected_type="VIDEO"
            )
    assert get.call_args.args[0] == "https://immich.example/api/assets/old-asset"
    if asset_type == "VIDEO":
        assert result == ("old-asset", True, None)
    else:
        assert result[0] is None
        assert "duplicate_type_mismatch" in result[2]
        assert "old-asset" in result[2]


@pytest.mark.parametrize("link_fails", [False, True])
def test_existing_live_still_gets_explicit_motion_link(tmp_path, link_fails):
    filepath = tmp_path / "still.HEIC"
    filepath.write_bytes(b"image")
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "still", "status": "duplicate"}
    linked = MagicMock(status_code=200)
    if link_fails:
        linked.raise_for_status.side_effect = requests.exceptions.HTTPError("403 Forbidden")
    with patch("synmich.core.immich.requests.Session.post", return_value=response):
        with patch("synmich.core.immich.requests.Session.put", return_value=linked) as put:
            result = ImmichClient("https://immich.example", "key", max_retries=1).upload_asset(
                filepath, live_photo_video_id="motion"
            )
    assert put.call_args.kwargs["json"] == {"livePhotoVideoId": "motion"}
    if link_fails:
        assert result[0] is None
        assert "live_photo_link_failed" in result[2]
    else:
        assert result == ("still", True, None)


def test_partner_in_timeline_enabled_temporarily_and_restored_on_close():
    client = ImmichClient("https://immich.example", "key")

    partners_resp = MagicMock(status_code=200)
    partners_resp.json.return_value = [{"id": "partner-1", "inTimeline": False}]
    partners_resp.raise_for_status = MagicMock()

    put_resp = MagicMock(status_code=200)
    put_resp.raise_for_status = MagicMock()

    search_resp = MagicMock(status_code=200)
    search_resp.json.return_value = {
        "assets": {
            "items": [
                {
                    "id": "partner-asset-1",
                    "ownerId": "partner-1",
                    "originalFileName": "IMG_9999.HEIC",
                    "originalPath": "/homes/partner/IMG_9999.HEIC",
                    "libraryId": "lib-partner",
                    "fileCreatedAt": "2025-07-01T10:00:00Z",
                }
            ],
            "nextPage": None,
        }
    }
    search_resp.raise_for_status = MagicMock()

    with patch("synmich.core.immich.requests.Session.get", return_value=partners_resp):
        with patch("synmich.core.immich.requests.Session.put", return_value=put_resp) as put:
            with patch("synmich.core.immich.requests.Session.post", return_value=search_resp):
                assets = list(client.iter_assets())
                assert len(assets) == 1
                assert assets[0]["id"] == "partner-asset-1"
                assert put.call_count == 1
                assert put.call_args.args[0] == "https://immich.example/api/partners/partner-1"
                assert put.call_args.kwargs["json"] == {"inTimeline": True}

                client.close()
                assert put.call_count == 2
                assert put.call_args.args[0] == "https://immich.example/api/partners/partner-1"
                assert put.call_args.kwargs["json"] == {"inTimeline": False}


def test_partner_timeline_bucket_fallback_when_search_excludes_partner():
    client = ImmichClient("https://immich.example", "key")

    partners_resp = MagicMock(status_code=200)
    partners_resp.json.return_value = [{"id": "partner-1", "inTimeline": False}]
    partners_resp.raise_for_status = MagicMock()

    put_resp = MagicMock(status_code=200)
    put_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("403 Forbidden")

    # Search only returns own assets (partner-1 is missing from search results)
    search_resp = MagicMock(status_code=200)
    search_resp.json.return_value = {
        "assets": {
            "items": [
                {
                    "id": "own-asset-1",
                    "ownerId": "me",
                    "originalFileName": "IMG_0001.JPG",
                    "originalPath": "/homes/me/IMG_0001.JPG",
                    "libraryId": "lib-me",
                    "fileCreatedAt": "2025-01-01T00:00:00Z",
                }
            ],
            "nextPage": None,
        }
    }
    search_resp.raise_for_status = MagicMock()

    buckets_resp = MagicMock(status_code=200)
    buckets_resp.json.return_value = [{"timeBucket": "2025-07-01T00:00:00.000Z", "count": 1}]
    buckets_resp.raise_for_status = MagicMock()

    # Columnar timeline bucket response (Immich >= 1.133) where originalPath is absent
    bucket_resp = MagicMock(status_code=200)
    bucket_resp.json.return_value = {
        "id": ["partner-asset-2"],
        "originalFileName": ["IMG_2222.HEIC"],
        "fileCreatedAt": ["2025-07-15T12:00:00Z"],
        "ownerId": ["partner-1"],
    }
    bucket_resp.raise_for_status = MagicMock()

    detail_resp = MagicMock(status_code=200)
    detail_resp.json.return_value = {
        "id": "partner-asset-2",
        "ownerId": "partner-1",
        "originalFileName": "IMG_2222.HEIC",
        "originalPath": "/homes/partner/IMG_2222.HEIC",
        "libraryId": "lib-partner",
        "fileCreatedAt": "2025-07-15T12:00:00Z",
    }
    detail_resp.raise_for_status = MagicMock()

    with patch(
        "synmich.core.immich.requests.Session.get",
        side_effect=[partners_resp, buckets_resp, bucket_resp, detail_resp],
    ):
        with patch("synmich.core.immich.requests.Session.put", return_value=put_resp):
            with patch("synmich.core.immich.requests.Session.post", return_value=search_resp):
                idx = client.build_filename_index()

    assert "img_0001.jpg" in idx
    assert "img_2222.heic" in idx
    assert idx["img_2222.heic"][0][0] == "partner-asset-2"
    assert idx["img_2222.heic"][0][2] == "lib-partner"
