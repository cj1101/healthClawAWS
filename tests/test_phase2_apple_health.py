"""Phase 2: Apple Health export.zip import."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app
from nemoclaw_health.connectors.apple_health import ingest_apple_health_export_from_zip
from nemoclaw_health.db import get_db


_SIMPLE_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="US">
  <Record type="HKQuantityTypeIdentifierStepCount"
          sourceName="UnitTest"
          sourceVersion="24.0"
          unit="count"
          value="500"
          startDate="2025-06-06 06:53:54 +0000"
          endDate="2025-06-06 06:54:54 +0000"/>
</HealthData>
"""


def _minimal_export_zip(prefix: str = "apple_health_export/export.xml") -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(prefix, _SIMPLE_EXPORT_XML)
    bio.seek(0)
    return bio.read()


def test_apple_health_import_via_function(iso_test_settings, tmp_path):
    zip_path = tmp_path / "h.zip"
    zip_path.write_bytes(_minimal_export_zip())

    db = get_db(iso_test_settings)

    first = ingest_apple_health_export_from_zip(db, zip_path)
    assert first["ok"] is True
    assert first["records_seen"] == 1
    assert first["records_ingested"] == 1

    second = ingest_apple_health_export_from_zip(db, zip_path)
    assert second["records_skipped_duplicate"] == 1

    with db.transaction() as cur:
        c = cur.execute(
            "SELECT COUNT(*) FROM raw_events WHERE source = ?",
            ("healthkit_export",),
        ).fetchone()[0]

    assert c == first["records_ingested"]


def test_http_apple_health_import(iso_test_settings):
    blob = _minimal_export_zip()
    client = TestClient(create_app(iso_test_settings))
    resp = client.post(
        "/v1/connectors/apple-health/import",
        files={"file": ("export.zip", blob, "application/zip")},
    )
    assert resp.status_code == 200
    j = resp.json()
    assert j["ok"] is True
    assert j["records_ingested"] == 1


def test_unknown_record_type_skipped(iso_test_settings, tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?><HealthData>
      <Record type="HKUnsupportedTypeXYZ" value="1" startDate="2025-06-06 06:53:54 +0000"/>
    </HealthData>"""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("apple_health_export/export.xml", xml)
    bio.seek(0)
    p = tmp_path / "bad.zip"
    p.write_bytes(bio.read())

    r = ingest_apple_health_export_from_zip(get_db(iso_test_settings), p)
    assert r["records_seen"] == 1
    assert r["records_ingested"] == 0
    assert r["records_skipped_type"] == 1


_MIXED_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="US">
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
          sourceName="Watch"
          unit="ms"
          value="48"
          startDate="2025-06-06 08:00:00 +0000"
          endDate="2025-06-06 08:01:00 +0000"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeRunning"
          duration="1200.5"
          sourceName="Watch"
          creationDate="2025-06-06 07:00:00 +0000"
          startDate="2025-06-06 07:00:00 +0000"
          endDate="2025-06-06 07:20:00 +0000"
          totalDistance="5000"
          totalEnergyBurned="350"/>
</HealthData>
"""


def test_apple_health_hrv_and_workout(iso_test_settings, tmp_path):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("apple_health_export/export.xml", _MIXED_EXPORT_XML)
    bio.seek(0)
    p = tmp_path / "mix.zip"
    p.write_bytes(bio.read())
    db = get_db(iso_test_settings)
    r = ingest_apple_health_export_from_zip(db, p)
    assert r["ok"] is True
    assert r["records_seen"] == 2
    assert r["records_ingested"] == 2
    with db.transaction() as cur:
        qrows = cur.execute(
            "SELECT event_type, domain_slug FROM raw_events WHERE source = ? ORDER BY event_type",
            ("healthkit_export",),
        ).fetchall()
        types = [qrows[i][0] for i in range(len(qrows))]
        d = cur.execute(
            "SELECT payload_json FROM raw_events WHERE event_type = ?",
            ("healthkit_workout",),
        ).fetchone()
    assert "healthkit_quantity" in types
    assert "healthkit_workout" in types
    assert d is not None
    assert "hk_workout_running" in str(d[0])
