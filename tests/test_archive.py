from garmin_outreach.archive import archive_bytes


def test_content_archive_is_idempotent(tmp_path):
    first, first_created = archive_bytes(b"same bytes", tmp_path, "export", ".kml")
    second, second_created = archive_bytes(b"same bytes", tmp_path, "another name", ".kml")

    assert first_created is True
    assert second_created is False
    assert first == second

    third, third_created = archive_bytes(b"same bytes", tmp_path, "export", ".kml")
    assert third == first
    assert third_created is False
