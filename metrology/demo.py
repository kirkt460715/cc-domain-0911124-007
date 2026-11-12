import json
import tempfile
from pathlib import Path
from .service import Service
from .ingress.readers import read_jsonl
from .output.snapshots import verify_export
from .proficiency.review import verify_export as verify_review_export


def pt_observation(observation_id, source, sequence, instrument, value, unit, uncertainty, minute):
    return {
        "id": observation_id,
        "source": source,
        "sequence": sequence,
        "instrument": instrument,
        "channel": "p",
        "event_time": f"2026-11-15T00:{minute:02d}:00Z",
        "value": value,
        "unit": unit,
        "uncertainty": uncertainty,
    }


def proficiency_demonstration(root):
    """Synthetic interlaboratory round: outlier laboratory, unit conversion,
    shared reference with a registered covariance, late and withdrawn
    submissions, certificate invalidation, and versioned scoring."""
    with Service(root / "pt.sqlite3") as app:
        instruments = []
        sources = []
        certificates = []
        policies = []
        bindings = []
        for lab, unit, upper, uncertainty in (
            ("a", "kPa", "20", ".001"),
            ("b", "bar", ".2", ".00001"),
            ("c", "kPa", "20", ".001"),
            ("d", "kPa", "20", ".001"),
            ("e", "kPa", "20", ".001"),
            ("f", "kPa", "20", ".001"),
        ):
            instruments.append(
                {
                    "id": f"press-{lab}",
                    "label": f"Synthetic instrument {lab}",
                    "channels": {"p": unit},
                }
            )
            sources.append({"id": f"src-{lab}", "allowed_lateness": 0})
            certificates.append(
                {
                    "id": f"cert-{lab}",
                    "instrument": f"press-{lab}",
                    "channel": "p",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "valid_to": "2027-01-01T00:00:00Z",
                    "input_unit": unit,
                    "output_unit": unit,
                    "slope": "1",
                    "intercept": "0",
                    "uncertainty": uncertainty,
                    "minimum": "0",
                    "maximum": upper,
                    "issued_at": "2025-12-01T00:00:00Z",
                }
            )
            policies.append(
                {"id": f"q-{lab}", "unit": unit, "lower": "0", "upper": upper}
            )
            bindings.append(
                {"instrument": f"press-{lab}", "channel": "p", "policy": f"q-{lab}"}
            )
        app.configuration.apply(
            {
                "id": "pt-demo-config",
                "instruments": instruments,
                "sources": sources,
                "certificates": certificates,
                "policies": policies,
                "bindings": bindings,
            }
        )
        readings = {
            "a": ("kPa", ".003", ("10.001", "10.003", "10.001", "10.003")),
            "b": ("bar", ".00003", (".10003", ".10005", ".10003", ".10005")),
            "c": ("kPa", ".003", ("10.19", "10.20", "10.19", "10.20")),
            "d": ("kPa", ".003", ("10.044", "10.046", "10.044", "10.046")),
            "e": ("kPa", ".003", ("10.005", "10.007", "10.005", "10.007")),
            "f": ("kPa", ".003", ("10.002", "10.004")),
        }
        for lab, (unit, uncertainty, values) in readings.items():
            rows = read_jsonl(
                "\n".join(
                    json.dumps(
                        pt_observation(
                            f"{lab}{index + 1}",
                            f"src-{lab}",
                            index,
                            f"press-{lab}",
                            value,
                            unit,
                            uncertainty,
                            index,
                        )
                    )
                    for index, value in enumerate(values)
                )
            )
            app.pipeline.ingest(
                f"pt-batch-{lab}", rows, received_at="2026-11-15T01:00:00Z"
            )
        channels = lambda lab: {
            "m-ref": {"instrument": f"press-{lab}", "channel": "p"},
            "m-cons": {"instrument": f"press-{lab}", "channel": "p"},
        }
        app.proficiency.plan(
            {
                "id": "pt-2026-11",
                "label": "Synthetic November pressure comparison",
                "items": [{"id": "artifact-a", "label": "Synthetic transfer artifact"}],
                "measurands": [
                    {
                        "id": "m-ref",
                        "item": "artifact-a",
                        "unit": "kPa",
                        "sigma": ".02",
                        "reference": {
                            "kind": "reference",
                            "source": "synthetic CRM-7 certificate 2026-03",
                            "value": "10.002",
                            "unit": "kPa",
                            "uncertainty": {
                                "kind": "expanded",
                                "value": ".004",
                                "coverage_factor": "2",
                            },
                        },
                        "correlations": {
                            "shared_references": [
                                {
                                    "id": "g1",
                                    "laboratories": ["lab-a", "lab-b"],
                                    "includes_assigned_reference": True,
                                    "description": "synthetic CAL-9 calibration chain",
                                }
                            ],
                            "covariances": [
                                {
                                    "between": ["lab-a", "reference"],
                                    "covariance": ".000002",
                                    "basis": "synthetic CAL-9 uncertainty budget",
                                }
                            ],
                        },
                    },
                    {
                        "id": "m-cons",
                        "item": "artifact-a",
                        "unit": "kPa",
                        "sigma": ".02",
                        "reference": {"kind": "consensus"},
                    },
                ],
                "laboratories": [
                    {
                        "id": f"lab-{lab}",
                        "label": f"Synthetic laboratory {lab}",
                        "channels": channels(lab),
                        "replicates": {"minimum": 2, "maximum": 4},
                    }
                    for lab in ("a", "b", "c", "d", "e", "f")
                ],
                "deadline": "2026-11-20T00:00:00Z",
                "evaluation": {"satisfactory": "2", "warning": "3"},
            }
        )
        app.proficiency.open("pt-2026-11", 1)
        submitted = "2026-11-18T09:00:00Z"
        for lab in ("a", "b", "c", "e"):
            app.proficiency.submit(
                {
                    "id": f"sub-{lab}",
                    "round": "pt-2026-11",
                    "laboratory": f"lab-{lab}",
                    "submitted_at": submitted,
                    "measurands": {
                        "m-ref": [f"{lab}1", f"{lab}2"],
                        "m-cons": [f"{lab}3", f"{lab}4"],
                    },
                }
            )
        app.proficiency.submit(
            {
                "id": "sub-d-superseded",
                "round": "pt-2026-11",
                "laboratory": "lab-d",
                "submitted_at": submitted,
                "measurands": {"m-ref": ["d1", "d2"]},
            }
        )
        app.proficiency.withdraw(
            "pt-2026-11", "sub-d-superseded", "laboratory cited the wrong certificate"
        )
        app.proficiency.submit(
            {
                "id": "sub-d",
                "round": "pt-2026-11",
                "laboratory": "lab-d",
                "submitted_at": submitted,
                "measurands": {"m-ref": ["d1", "d2"], "m-cons": ["d3", "d4"]},
            }
        )
        late = app.proficiency.submit(
            {
                "id": "sub-f",
                "round": "pt-2026-11",
                "laboratory": "lab-f",
                "submitted_at": "2026-11-21T09:00:00Z",
                "measurands": {"m-ref": ["f1", "f2"]},
            }
        )
        assert late["status"] == "late"
        app.proficiency.close("pt-2026-11", 2)
        first = app.proficiency_review.score("pt-2026-11")
        assert first["version"] == 1 and not first["reused"]
        again = app.proficiency_review.score("pt-2026-11")
        assert again["reused"] and again["version"] == 1
        review1 = app.proficiency_review.review("pt-2026-11", 1)
        reference_scores = {
            entry["laboratory"]: entry
            for entry in review1["measurands"]["m-ref"]["scores"]
        }
        assert reference_scores["lab-c"]["classification"] == "unsatisfactory"
        assert reference_scores["lab-c"]["z"] == "9.65"
        assert reference_scores["lab-d"]["classification"] == "warning"
        assert reference_scores["lab-a"]["classification"] == "satisfactory"
        assert reference_scores["lab-a"]["interpretation"] == "standard"
        assert reference_scores["lab-b"]["interpretation"] == "limited"
        consensus = review1["measurands"]["m-cons"]["assigned"]
        assert consensus["kind"] == "consensus"
        assert consensus["participants"] == 5
        assert consensus["value"] == "10.006"
        influence = {
            entry["laboratory"]: entry["shift"]
            for entry in review1["measurands"]["m-cons"]["influence"]
        }
        assert influence["lab-c"] == "-0.001"
        assert influence["lab-a"] == "0.0195"
        assert {item["status"] for item in review1["excluded_submissions"]} == {
            "late",
            "withdrawn",
        }
        lab_a_report = app.proficiency_review.lab_report("pt-2026-11", "lab-a", 1)
        lab_a_ref = next(
            score for score in lab_a_report["scores"] if score["measurand"] == "m-ref"
        )
        assert lab_a_ref["covariance"] == "0.000002"
        app.proficiency_review.export("pt-2026-11", root / "pt-review-v1.json", 1)
        verified = verify_review_export(root / "pt-review-v1.json")
        assert verified["version"] == 1 and verified["measurands"] == 2
        app.catalog.revoke_certificate(
            "cert-c", "reference standard drift discovered after close"
        )
        second = app.proficiency_review.score("pt-2026-11")
        assert second["version"] == 2 and not second["reused"]
        review2 = app.proficiency_review.review("pt-2026-11", 2)
        lab_c_v2 = next(
            entry
            for entry in review2["measurands"]["m-ref"]["scores"]
            if entry["laboratory"] == "lab-c"
        )
        assert lab_c_v2["status"] == "excluded"
        assert lab_c_v2["reasons"] == ["certificate_invalidated"]
        assert review2["measurands"]["m-cons"]["assigned"]["participants"] == 4
        lab_c_v1 = app.proficiency_review.lab_report("pt-2026-11", "lab-c", 1)
        lab_c_ref_v1 = next(
            score for score in lab_c_v1["scores"] if score["measurand"] == "m-ref"
        )
        assert lab_c_ref_v1["classification"] == "unsatisfactory"
        return {
            "round": "pt-2026-11",
            "first_version": first,
            "second_version": second,
            "reference_z_scores": {
                lab: entry["z"] for lab, entry in sorted(reference_scores.items())
            },
            "consensus_assigned": consensus["value"],
            "consensus_influence": influence,
            "excluded_states": review1["excluded_submissions"],
            "export_verified": verified,
            "lab_c_after_revocation": lab_c_v2["status"],
            "scope": review1["scope"],
        }


def demonstration():
    with tempfile.TemporaryDirectory(prefix="liangheng-") as directory:
        root = Path(directory)
        with Service(root / "lab.sqlite3") as app:
            app.configuration.apply(
                {
                    "id": "demo-config",
                    "instruments": [
                        {
                            "id": "press-1",
                            "label": "Synthetic pressure instrument",
                            "channels": {"p": "kPa"},
                        }
                    ],
                    "sources": [{"id": "bench", "allowed_lateness": 0}],
                    "certificates": [
                        {
                            "id": "cert-1",
                            "instrument": "press-1",
                            "channel": "p",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_to": "2027-01-01T00:00:00Z",
                            "input_unit": "kPa",
                            "output_unit": "kPa",
                            "slope": "1",
                            "intercept": ".1",
                            "uncertainty": ".01",
                            "minimum": "0",
                            "maximum": "100",
                            "issued_at": "2025-12-01T00:00:00Z",
                        }
                    ],
                    "policies": [
                        {
                            "id": "q1",
                            "unit": "kPa",
                            "lower": "0",
                            "upper": "20",
                            "required_tags": ["lot"],
                        }
                    ],
                    "bindings": [
                        {"instrument": "press-1", "channel": "p", "policy": "q1"}
                    ],
                }
            )
            values = [
                {
                    "id": f"r{i}",
                    "source": "bench",
                    "sequence": i,
                    "instrument": "press-1",
                    "channel": "p",
                    "event_time": f"2026-11-11T00:00:{i * 10:02d}Z",
                    "value": str(10 + i),
                    "unit": "kPa",
                    "uncertainty": "0",
                    "tags": {"lot": "synthetic-A"},
                }
                for i in range(3)
            ]
            rows = read_jsonl("\n".join(json.dumps(value) for value in values))
            ingestion = app.pipeline.ingest(
                "demo-batch", rows, received_at="2026-11-11T00:01:00Z"
            )
            duplicate = app.pipeline.ingest(
                "demo-batch", rows, received_at="2026-11-11T00:01:00Z"
            )
            app.catalog.watermark("bench", "2026-11-11T00:01:00Z")
            window = app.windows.materialize(
                {
                    "id": "minute",
                    "instrument": "press-1",
                    "channel": "p",
                    "start": "2026-11-11T00:00:00Z",
                    "end": "2026-11-11T00:01:00Z",
                    "seconds": 60,
                    "sources": ["bench"],
                }
            )
            app.windows.seal("minute", window["generation"])
            snapshot = app.snapshots.create("release-1", ["minute"])
            app.snapshots.export("release-1", root / "snapshot.json")
            verification = verify_export(root / "snapshot.json")
            integrity = app.inspector.check()
            assert ingestion["accepted"] == 3
            assert duplicate["reused"] is True
            assert window["windows"][0]["statistics"]["mean"] == "11.1"
            assert verification["observations"] == 3
            proficiency = proficiency_demonstration(root)
            return {
                "ingestion": ingestion,
                "duplicate_reused": duplicate["reused"],
                "mean": window["windows"][0]["statistics"]["mean"],
                "snapshot": snapshot,
                "export_verified": verification,
                "integrity": integrity,
                "proficiency": proficiency,
            }
