from synmich.core.migrator import _iso_to_epoch, match_existing_asset


def test_prefers_single_external_asset_over_upload_copies_with_shifted_video_time():
    filename = "2025-09-15_12-46-22_GX010229.MP4"
    candidates = [
        (
            "5fd7af69-d7b8-4997-bb8b-94003df0a541",
            "2025-09-15T12:45:46+00:00",
            None,
        ),
        (
            "7d10c5b6-884a-43c4-816f-2efc2ad9df94",
            "2025-09-15T12:45:46+00:00",
            None,
        ),
        (
            "7034ccbd-7fd8-46a8-9ecf-3c81007d9139",
            "2025-09-15T09:45:46+00:00",
            "1bb4e262-396e-4596-b051-c71e845aff33",
        ),
    ]

    assert (
        match_existing_asset(
            {filename.lower(): candidates},
            filename,
            _iso_to_epoch("2025-09-15T12:45:46+00:00"),
        )
        == "7034ccbd-7fd8-46a8-9ecf-3c81007d9139"
    )


def test_multiple_external_assets_still_require_capture_date_match():
    filename = "GOPR0023.JPG"
    candidates = [
        ("external-old", "2025-09-15T12:45:46+00:00", "library-a"),
        ("external-new", "2025-10-20T08:30:00+00:00", "library-a"),
    ]

    assert (
        match_existing_asset(
            {filename.lower(): candidates},
            filename,
            _iso_to_epoch("2025-10-20T08:30:30+00:00"),
        )
        == "external-new"
    )


def test_single_external_asset_with_wrong_capture_date_is_rejected():
    filename = "IMG_1234.HEIC"
    candidates = [
        ("own-random-photo", "2023-05-10T14:00:00+00:00", "own-library"),
    ]

    assert (
        match_existing_asset(
            {filename.lower(): candidates},
            filename,
            _iso_to_epoch("2025-08-22T09:15:00+00:00"),
        )
        is None
    )


def test_matches_timezone_shifted_and_path_dated_external_assets():
    # Synology stores local camera time as UTC epoch (5h offset from Immich UTC)
    pxl = "PXL_20261018_173319426.TS-000.jpg"
    assert (
        match_existing_asset(
            {
                pxl.lower(): [
                    (
                        "partner-pxl",
                        "2026-10-18T17:33:21.000Z",
                        "partner-lib",
                        "/mnt/synology/2026/1017-18 Camping/PXL_20261018_173319426.TS-000.jpg",
                    )
                ]
            },
            pxl,
            _iso_to_epoch("2026-10-18T12:33:19+00:00"),
        )
        == "partner-pxl"
    )

    # Disambiguates multiple same-named external assets using folder year/month
    # when Immich fileCreatedAt reflects NAS copy mtime.
    img = "IMG_1617.JPG"
    candidates = [
        ("wrong-2026", "2026-07-22T00:00:00Z", "partner-lib", "/mnt/synology/2026/07 Viaje/IMG_1617.JPG"),
        ("right-2022", "2022-07-30T18:00:00Z", "partner-lib", "/mnt/synology/2022/0704-18 Vacaciones/IMG_1617.JPG"),
        ("wrong-2016", "2016-08-20T00:00:00Z", "partner-lib", "/mnt/synology/2016/0820 Cumple/IMG_1617.JPG"),
    ]
    assert (
        match_existing_asset(
            {img.lower(): candidates},
            img,
            _iso_to_epoch("2022-07-15T19:48:00+00:00"),
        )
        == "right-2022"
    )


def test_prefers_owner_path_when_multiple_external_assets_have_same_date():
    filename = "2025-03-04_15-02-23_IMG_2146.HEIC"
    candidates = [
        (
            "shared-copy",
            "2025-03-04T14:02:23.486+00:00",
            "shared-library",
            "/volume1/photo/2025-misc/2025-03-04_15-02-23_IMG_2146.HEIC",
        ),
        (
            "nino-copy",
            "2025-03-04T14:02:23.486+00:00",
            "nino-library",
            "/volume1/homes/Nino/Photos/PhotoSync/2025/03/"
            "2025-03-04_15-02-23_IMG_2146.HEIC",
        ),
        (
            "uploaded-copy",
            "2025-03-04T14:02:23.486+00:00",
            None,
            "/data/upload/c0e83c7c/photo.HEIC",
        ),
    ]

    assert (
        match_existing_asset(
            {filename.lower(): candidates},
            filename,
            _iso_to_epoch("2025-03-04T14:02:23.486+00:00"),
            owner_name="Nino",
        )
        == "nino-copy"
    )
