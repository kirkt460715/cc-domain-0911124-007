import json
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from metrology.service import Service
from metrology.errors import Conflict, InputError, IntegrityError, Missing, StateError
from metrology.observations import Observation
from metrology.ingress.readers import InputRow
from metrology.proficiency import scoring
from metrology.proficiency.review import verify_export
from metrology.values import decimal_text
from metrology.console import dispatch

DEADLINE = "2026-11-20T00:00:00Z"
SUBMITTED = "2026-11-18T09:00:00Z"


def make_observation(observation_id, source, sequence, instrument, value, unit, uncertainty):
    return Observation.parse(
        {
            "id": observation_id,
            "source": source,
            "sequence": sequence,
            "instrument": instrument,
            "channel": "p",
            "event_time": f"2026-11-15T00:{sequence % 60:02d}:00Z",
            "value": value,
            "unit": unit,
            "uncertainty": uncertainty,
        }
    )


def certificate(instrument, unit="kPa", uncertainty=".001", upper="20", cert_id=None):
    return {
        "id": cert_id or f"cert-{instrument}",
        "instrument": instrument,
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


def reference(value="10.002", uncertainty=None):
    return {
        "kind": "reference",
        "source": "synthetic reference certificate",
        "value": value,
        "unit": "kPa",
        "uncertainty": uncertainty
        or {"kind": "expanded", "value": ".004", "coverage_factor": "2"},
    }


def plan_data(
    laboratories=("a", "b"),
    measurands=None,
    replicates=(2, 4),
    evaluation=None,
    units=None,
):
    units = units or {}
    measurands = measurands or [
        {
            "id": "m1",
            "item": "artifact-a",
            "unit": "kPa",
            "sigma": ".02",
            "reference": reference(),
        }
    ]
    return {
        "id": "pt-1",
        "label": "Synthetic round",
        "items": [{"id": "artifact-a", "label": "Synthetic artifact"}],
        "measurands": measurands,
        "laboratories": [
            {
                "id": f"lab-{lab}",
                "label": f"Synthetic laboratory {lab}",
                "channels": {
                    measurand["id"]: {
                        "instrument": f"press-{lab}",
                        "channel": "p",
                    }
                    for measurand in measurands
                },
                "replicates": {"minimum": replicates[0], "maximum": replicates[1]},
            }
            for lab in laboratories
        ],
        "deadline": DEADLINE,
        "evaluation": evaluation or {"satisfactory": "2", "warning": "3"},
    }


class ProficiencyFixture(unittest.TestCase):
    def setUp(self):
        self.app = Service()
        self.addCleanup(self.app.close)

    def add_laboratory(
        self, lab, unit="kPa", cert_uncertainty=".001", upper="20", policy_upper=None
    ):
        self.app.catalog.add_instrument(
            {
                "id": f"press-{lab}",
                "label": f"Synthetic instrument {lab}",
                "channels": {"p": unit},
            }
        )
        self.app.catalog.add_source(f"src-{lab}")
        self.app.catalog.add_certificate(
            certificate(
                f"press-{lab}",
                unit=unit,
                uncertainty=cert_uncertainty,
                upper=upper if unit == "kPa" else ".2",
            )
        )
        self.app.catalog.add_policy(
            {
                "id": f"q-{lab}",
                "unit": unit,
                "lower": "0",
                "upper": policy_upper or (upper if unit == "kPa" else ".2"),
            }
        )
        self.app.catalog.bind(f"press-{lab}", "p", f"q-{lab}")

    def observe(self, lab, values, unit="kPa", uncertainty=".003", start=0):
        rows = [
            InputRow(
                index + 1,
                observation=make_observation(
                    f"{lab}{start + index + 1}",
                    f"src-{lab}",
                    start + index,
                    f"press-{lab}",
                    value,
                    unit,
                    uncertainty,
                ),
            )
            for index, value in enumerate(values)
        ]
        result = self.app.pipeline.ingest(
            f"batch-{lab}-{start}", rows, received_at="2026-11-15T01:00:00Z"
        )
        assert result["accepted"] == len(values), result
        return [f"{lab}{start + index + 1}" for index in range(len(values))]

    def open_round(self, data=None):
        plan = self.app.proficiency.plan(data or plan_data())
        self.app.proficiency.open(plan["id"], 1)
        return plan["id"]

    def submit(self, round_id, lab, measurands, submission_id=None, at=SUBMITTED):
        return self.app.proficiency.submit(
            {
                "id": submission_id or f"sub-{lab}",
                "round": round_id,
                "laboratory": f"lab-{lab}",
                "submitted_at": at,
                "measurands": measurands,
            }
        )

    def close_and_score(self, round_id):
        current = self.app.proficiency.get(round_id)
        self.app.proficiency.close(round_id, current["revision"])
        return self.app.proficiency_review.score(round_id)


class PlanValidationTests(ProficiencyFixture):
    def setUp(self):
        super().setUp()
        self.add_laboratory("a")
        self.add_laboratory("b")

    def test_plan_registers_and_is_immutable(self):
        data = plan_data()
        planned = self.app.proficiency.plan(data)
        self.assertEqual(planned["state"], "planned")
        self.assertEqual(planned["revision"], 1)
        again = self.app.proficiency.plan(data)
        self.assertEqual(again["plan"], planned["plan"])
        changed = plan_data()
        changed["label"] = "Different label"
        with self.assertRaises(Conflict):
            self.app.proficiency.plan(changed)

    def test_evaluation_boundaries_must_increase(self):
        with self.assertRaises(InputError):
            self.app.proficiency.plan(
                plan_data(evaluation={"satisfactory": "3", "warning": "2"})
            )

    def test_sigma_must_be_positive(self):
        data = plan_data()
        data["measurands"][0]["sigma"] = "0"
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_replicate_rule_must_increase(self):
        with self.assertRaises(InputError):
            self.app.proficiency.plan(plan_data(replicates=(4, 2)))

    def test_duplicate_laboratory_rejected(self):
        with self.assertRaises(InputError):
            self.app.proficiency.plan(plan_data(laboratories=("a", "a")))

    def test_unmapped_measurand_rejected(self):
        data = plan_data()
        data["laboratories"][0]["channels"] = {}
        data["laboratories"][1]["channels"] = {}
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_unknown_measurand_mapping_rejected(self):
        data = plan_data()
        data["laboratories"][0]["channels"]["ghost"] = {
            "instrument": "press-a",
            "channel": "p",
        }
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_reference_uncertainty_must_be_explicit(self):
        data = plan_data()
        data["measurands"][0]["reference"] = reference(
            uncertainty={"kind": "expanded", "value": ".004"}
        )
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)
        data["measurands"][0]["reference"] = reference(
            uncertainty={"kind": "standard", "value": ".002", "coverage_factor": "2"}
        )
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)
        data["measurands"][0]["reference"] = {"kind": "consensus", "source": "x"}
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_reference_unit_must_be_convertible(self):
        data = plan_data()
        data["measurands"][0]["reference"] = {
            "kind": "reference",
            "source": "synthetic certificate",
            "value": "10",
            "unit": "kg",
            "uncertainty": {"kind": "standard", "value": ".1"},
        }
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_covariance_rules(self):
        correlations = {
            "covariances": [
                {"between": ["lab-a", "lab-b"], "covariance": "1", "basis": "x"}
            ]
        }
        data = plan_data()
        data["measurands"][0]["correlations"] = correlations
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)
        correlations["covariances"][0]["between"] = ["lab-a", "ghost"]
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)
        correlations["covariances"] = [
            {"between": ["lab-a", "reference"], "covariance": "1", "basis": "x"},
            {"between": ["reference", "lab-a"], "covariance": "1", "basis": "y"},
        ]
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)

    def test_shared_reference_group_validation(self):
        data = plan_data()
        data["measurands"][0]["correlations"] = {
            "shared_references": [
                {"id": "g1", "laboratories": ["lab-a", "ghost"]}
            ]
        }
        with self.assertRaises(InputError):
            self.app.proficiency.plan(data)


class LifecycleTests(ProficiencyFixture):
    def setUp(self):
        super().setUp()
        self.add_laboratory("a")
        self.add_laboratory("b")

    def test_state_transitions_and_revisions(self):
        round_id = self.app.proficiency.plan(plan_data())["id"]
        with self.assertRaises(Conflict):
            self.app.proficiency.open(round_id, 2)
        with self.assertRaises(StateError):
            self.app.proficiency.close(round_id, 1)
        opened = self.app.proficiency.open(round_id, 1)
        self.assertEqual(opened["state"], "open")
        with self.assertRaises(StateError):
            self.app.proficiency.open(round_id, 2)
        closed = self.app.proficiency.close(round_id, 2)
        self.assertEqual(closed["state"], "closed")
        with self.assertRaises(StateError):
            self.app.proficiency.submit(
                {
                    "id": "sub-a",
                    "round": round_id,
                    "laboratory": "lab-a",
                    "submitted_at": SUBMITTED,
                    "measurands": {"m1": ["a1", "a2"]},
                }
            )
        reopened = self.app.proficiency.reopen(round_id, 3, "admin-1", "late data")
        self.assertEqual(reopened["state"], "open")
        self.assertEqual(reopened["revision"], 4)

    def test_submit_only_while_open(self):
        round_id = self.app.proficiency.plan(plan_data())["id"]
        with self.assertRaises(StateError):
            self.submit(round_id, "a", {"m1": ["a1", "a2"]})

    def test_reference_change_requires_closed_or_planned(self):
        round_id = self.open_round()
        with self.assertRaises(StateError):
            self.app.proficiency.change_reference(
                round_id, "m1", reference(value="10.5"), 2, "admin-1", "swap"
            )
        self.app.proficiency.close(round_id, 2)
        with self.assertRaises(Missing):
            self.app.proficiency.change_reference(
                round_id, "ghost", reference(), 3, "admin-1", "swap"
            )

    def test_reference_change_allowed_while_planned(self):
        round_id = self.app.proficiency.plan(plan_data())["id"]
        changed = self.app.proficiency.change_reference(
            round_id, "m1", reference(value="10.5"), 1, "admin-1", "early swap"
        )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["references"]["m1"]["value"], "10.5")

    def test_reference_change_bumps_revision_and_is_audited(self):
        round_id = self.open_round()
        self.observe("a", ["10.001", "10.003"])
        self.observe("b", ["10.004", "10.006"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        first = self.close_and_score(round_id)
        changed = self.app.proficiency.change_reference(
            round_id, "m1", reference(value="10.01"), 3, "admin-1", "CRM reissue"
        )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["revision"], 4)
        second = self.app.proficiency_review.score(round_id)
        self.assertEqual(second["version"], 2)
        review = self.app.proficiency_review.review(round_id, 2)
        self.assertEqual(review["measurands"]["m1"]["assigned"]["value"], "10.01")
        earlier = self.app.proficiency_review.review(round_id, 1)
        self.assertEqual(earlier["measurands"]["m1"]["assigned"]["value"], "10.002")
        unchanged = self.app.proficiency.change_reference(
            round_id, "m1", reference(value="10.01"), 4, "admin-1", "no-op"
        )
        self.assertFalse(unchanged["changed"])


class SubmissionTests(ProficiencyFixture):
    def setUp(self):
        super().setUp()
        self.add_laboratory("a")
        self.add_laboratory("b")
        self.round_id = self.open_round()

    def test_submission_links_measurement_and_certificate(self):
        self.observe("a", ["10.001", "10.003"])
        result = self.submit(self.round_id, "a", {"m1": ["a1", "a2"]})
        self.assertEqual(result["status"], "submitted")
        pin = result["payload"]["items"]["m1"][0]
        self.assertEqual(pin["observation"], "a1")
        self.assertEqual(pin["revision"], 1)
        self.assertEqual(pin["certificate"], "cert-press-a")
        self.assertEqual(len(pin["certificate_fingerprint"]), 64)

    def test_late_submission_is_flagged(self):
        self.observe("a", ["10.001", "10.003"])
        result = self.submit(
            self.round_id, "a", {"m1": ["a1", "a2"]}, at="2026-11-21T00:00:00Z"
        )
        self.assertEqual(result["status"], "late")

    def test_unknown_laboratory_and_observation(self):
        with self.assertRaises(Missing):
            self.submit(self.round_id, "ghost", {"m1": ["a1", "a2"]})
        with self.assertRaises(Missing):
            self.submit(self.round_id, "a", {"m1": ["a1", "a2"]})

    def test_replicate_rule_enforced(self):
        self.observe("a", ["10.001"])
        with self.assertRaises(InputError):
            self.submit(self.round_id, "a", {"m1": ["a1"]})

    def test_duplicate_observation_in_submission(self):
        self.observe("a", ["10.001", "10.003"])
        with self.assertRaises(InputError):
            self.submit(self.round_id, "a", {"m1": ["a1", "a1"]})

    def test_measurement_cannot_serve_two_participants(self):
        self.observe("a", ["10.001", "10.003"])
        self.observe("b", ["10.004", "10.006"])
        self.submit(self.round_id, "a", {"m1": ["a1", "a2"]})
        with self.assertRaises(Conflict):
            self.submit(self.round_id, "b", {"m1": ["a1", "b2"]})
        with self.assertRaises(Conflict):
            self.submit(self.round_id, "b", {"m1": ["a1", "a2"]})

    def test_one_active_submission_per_laboratory(self):
        self.observe("a", ["10.001", "10.003", "10.002", "10.004"])
        self.submit(self.round_id, "a", {"m1": ["a1", "a2"]})
        with self.assertRaises(Conflict):
            self.submit(self.round_id, "a", {"m1": ["a3", "a4"]}, submission_id="sub-a2")
        self.app.proficiency.withdraw(self.round_id, "sub-a", "superseded")
        result = self.submit(
            self.round_id, "a", {"m1": ["a3", "a4"]}, submission_id="sub-a2"
        )
        self.assertEqual(result["status"], "submitted")
        with self.assertRaises(StateError):
            self.app.proficiency.withdraw(self.round_id, "sub-a", "already withdrawn")

    def test_withdraw_only_while_open(self):
        self.observe("a", ["10.001", "10.003"])
        self.submit(self.round_id, "a", {"m1": ["a1", "a2"]})
        self.app.proficiency.close(self.round_id, 2)
        with self.assertRaises(StateError):
            self.app.proficiency.withdraw(self.round_id, "sub-a", "too late")

    def test_rejected_measurement_cannot_be_submitted(self):
        self.add_laboratory("r", policy_upper="19")
        rows = [
            InputRow(
                1,
                observation=make_observation(
                    "r1", "src-r", 0, "press-r", "19.5", "kPa", ".003"
                ),
            ),
            InputRow(
                2,
                observation=make_observation(
                    "r2", "src-r", 1, "press-r", "10", "kPa", ".003"
                ),
            ),
        ]
        result = self.app.pipeline.ingest(
            "batch-reject", rows, received_at="2026-11-15T01:00:00Z"
        )
        self.assertEqual(result["rejected"], 1)
        data = plan_data(laboratories=("a", "b"))
        data["id"] = "pt-reject"
        data["laboratories"][0]["id"] = "lab-r"
        data["laboratories"][0]["channels"]["m1"] = {
            "instrument": "press-r",
            "channel": "p",
        }
        round_id = self.app.proficiency.plan(data)["id"]
        self.app.proficiency.open(round_id, 1)
        with self.assertRaises(InputError):
            self.submit(round_id, "r", {"m1": ["r1", "r2"]})

    def test_channel_mismatch_rejected(self):
        self.add_laboratory("c")
        self.observe("c", ["10.001", "10.003"])
        with self.assertRaises(InputError):
            self.submit(self.round_id, "a", {"m1": ["c1", "c2"]})


class ScoringTests(ProficiencyFixture):
    def setUp(self):
        super().setUp()
        for lab in ("a", "b", "c", "d"):
            self.add_laboratory(lab)

    def test_z_classification_boundaries(self):
        round_id = self.open_round(plan_data(laboratories=("a", "b", "c", "d")))
        self.observe("a", ["10.001", "10.003"])  # mean 10.002, z = 0
        self.observe("b", ["10.041", "10.043"])  # mean 10.042, z = 2
        self.observe("c", ["10.051", "10.053"])  # mean 10.052, z = 2.5
        self.observe("d", ["10.061", "10.063"])  # mean 10.062, z = 3
        for lab in ("a", "b", "c", "d"):
            self.submit(round_id, lab, {"m1": [f"{lab}1", f"{lab}2"]})
        self.close_and_score(round_id)
        review = self.app.proficiency_review.review(round_id)
        classes = {
            entry["laboratory"]: entry["classification"]
            for entry in review["measurands"]["m1"]["scores"]
        }
        self.assertEqual(
            classes,
            {
                "lab-a": "satisfactory",
                "lab-b": "satisfactory",
                "lab-c": "warning",
                "lab-d": "unsatisfactory",
            },
        )
        self.assertEqual(review["measurands"]["m1"]["classifications"]["warning"], 1)
        self.assertEqual(review["measurands"]["m1"]["unsatisfactory"], ["lab-d"])

    def test_zeta_uses_registered_covariance(self):
        correlations = {
            "shared_references": [
                {
                    "id": "g1",
                    "laboratories": ["lab-a", "lab-b"],
                    "includes_assigned_reference": True,
                }
            ],
            "covariances": [
                {
                    "between": ["lab-a", "reference"],
                    "covariance": ".000002",
                    "basis": "synthetic shared calibration",
                }
            ],
        }
        data = plan_data(laboratories=("a", "b"))
        data["measurands"][0]["correlations"] = correlations
        round_id = self.open_round(data)
        self.observe("a", ["10.003", "10.005"])  # mean 10.004
        self.observe("b", ["10.003", "10.005"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        self.close_and_score(round_id)
        report_a = self.app.proficiency_review.lab_report(round_id, "lab-a")
        entry_a = report_a["scores"][0]
        self.assertEqual(entry_a["covariance"], "0.000002")
        self.assertEqual(entry_a["interpretation"], "standard")
        u_lab = Decimal(entry_a["summary"]["standard_uncertainty"])
        with localcontext() as ctx:
            ctx.prec = 40
            with_covariance = Decimal("0.002") / (
                u_lab**2 + Decimal("0.002") ** 2 - 2 * Decimal("0.000002")
            ).sqrt()
            without_covariance = Decimal("0.002") / (
                u_lab**2 + Decimal("0.002") ** 2
            ).sqrt()
        self.assertLess(abs(Decimal(entry_a["zeta"]) - with_covariance), Decimal("1e-30"))
        self.assertNotEqual(entry_a["zeta"], decimal_text(without_covariance))
        report_b = self.app.proficiency_review.lab_report(round_id, "lab-b")
        entry_b = report_b["scores"][0]
        self.assertIsNone(entry_b["covariance"])
        self.assertEqual(entry_b["interpretation"], "limited")
        self.assertIn("shared reference", entry_b["notes"][0])
        self.assertLess(
            abs(Decimal(entry_b["zeta"]) - without_covariance), Decimal("1e-30")
        )

    def test_covariance_cannot_force_nonpositive_variance(self):
        correlations = {
            "covariances": [
                {
                    "between": ["lab-a", "reference"],
                    "covariance": ".00001",
                    "basis": "inflated synthetic covariance",
                }
            ]
        }
        data = plan_data(laboratories=("a", "b"))
        data["measurands"][0]["correlations"] = correlations
        round_id = self.open_round(data)
        self.observe("a", ["10.003", "10.005"])
        self.observe("b", ["10.003", "10.005"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        self.close_and_score(round_id)
        entry = self.app.proficiency_review.lab_report(round_id, "lab-a")["scores"][0]
        self.assertIsNone(entry["zeta"])
        self.assertEqual(entry["reasons"], ["nonpositive_variance"])
        self.assertEqual(entry["classification"], "satisfactory")

    def test_zero_scale_is_not_computed(self):
        self.add_laboratory("e", cert_uncertainty="0")
        self.add_laboratory("f", cert_uncertainty="0")
        data = plan_data(laboratories=("e", "f"))
        data["measurands"][0]["reference"] = reference(
            uncertainty={"kind": "standard", "value": "0"}
        )
        round_id = self.open_round(data)
        self.observe("e", ["10.003", "10.005"], uncertainty="0")
        self.observe("f", ["10.003", "10.005"], uncertainty="0")
        self.submit(round_id, "e", {"m1": ["e1", "e2"]})
        self.submit(round_id, "f", {"m1": ["f1", "f2"]})
        self.close_and_score(round_id)
        entry = self.app.proficiency_review.lab_report(round_id, "lab-e")["scores"][0]
        self.assertEqual(entry["z"], "0.1")
        self.assertIsNone(entry["zeta"])
        self.assertEqual(entry["reasons"], ["zero_scale"])

    def test_consensus_aggregates_per_laboratory_first(self):
        data = plan_data(laboratories=("a", "b", "c"), replicates=(2, 4))
        data["measurands"][0]["reference"] = {"kind": "consensus"}
        round_id = self.open_round(data)
        self.observe("a", ["10.000", "10.000", "10.000", "10.000"])
        self.observe("b", ["10.004", "10.004"])
        self.observe("c", ["10.008", "10.008"])
        self.submit(round_id, "a", {"m1": ["a1", "a2", "a3", "a4"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        self.submit(round_id, "c", {"m1": ["c1", "c2"]})
        self.close_and_score(round_id)
        assigned = self.app.proficiency_review.review(round_id)["measurands"]["m1"][
            "assigned"
        ]
        # Pooling the eight measurements would give 10.000; per-laboratory
        # aggregation gives the median of the three laboratory means.
        self.assertEqual(assigned["value"], "10.004")
        self.assertEqual(assigned["participants"], 3)
        self.assertEqual(assigned["mad"], "0.004")
        self.assertEqual(assigned["robust_standard_deviation"], "0.005932")
        self.assertEqual(len(assigned["leave_one_out"]), 3)
        shifts = {
            entry["laboratory"]: entry["shift"]
            for entry in assigned["leave_one_out"]
        }
        self.assertEqual(shifts, {"lab-a": "0.002", "lab-b": "0", "lab-c": "-0.002"})

    def test_consensus_requires_two_participants(self):
        data = plan_data(laboratories=("a", "b"))
        data["measurands"][0]["reference"] = {"kind": "consensus"}
        round_id = self.open_round(data)
        self.observe("a", ["10.001", "10.003"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.close_and_score(round_id)
        review = self.app.proficiency_review.review(round_id)
        assigned = review["measurands"]["m1"]["assigned"]
        self.assertEqual(assigned["reason"], "insufficient_participants")
        entry = review["measurands"]["m1"]["scores"][0]
        self.assertEqual(entry["status"], "not_computable")
        self.assertIsNone(entry["z"])

    def test_unit_conversion_at_scoring(self):
        self.add_laboratory("e", unit="bar")
        data = plan_data(laboratories=("a", "e"))
        round_id = self.open_round(data)
        self.observe("a", ["10.001", "10.003"])
        self.observe("e", [".10001", ".10003"], unit="bar", uncertainty=".00003")
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "e", {"m1": ["e1", "e2"]})
        self.close_and_score(round_id)
        entry = self.app.proficiency_review.lab_report(round_id, "lab-e")["scores"][0]
        self.assertEqual(entry["summary"]["mean"], "10.002")
        self.assertEqual(entry["z"], "0")
        self.assertEqual(entry["unit"], "kPa")

    def test_certificate_revocation_creates_new_version(self):
        round_id = self.open_round(plan_data(laboratories=("a", "b")))
        self.observe("a", ["10.001", "10.003"])
        self.observe("b", ["10.061", "10.063"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        first = self.close_and_score(round_id)
        self.assertEqual(first["version"], 1)
        reused = self.app.proficiency_review.score(round_id)
        self.assertTrue(reused["reused"])
        self.app.catalog.revoke_certificate("cert-press-b", "synthetic drift")
        second = self.app.proficiency_review.score(round_id)
        self.assertEqual(second["version"], 2)
        review = self.app.proficiency_review.review(round_id, 2)
        entry_b = next(
            item
            for item in review["measurands"]["m1"]["scores"]
            if item["laboratory"] == "lab-b"
        )
        self.assertEqual(entry_b["status"], "excluded")
        self.assertEqual(entry_b["reasons"], ["certificate_invalidated"])
        # The first version notification is not rewritten in place.
        earlier = self.app.proficiency_review.lab_report(round_id, "lab-b", 1)
        self.assertEqual(earlier["scores"][0]["classification"], "unsatisfactory")
        self.assertEqual(earlier["scores"][0]["z"], "3")

    def test_late_and_withdrawn_states_survive_close(self):
        round_id = self.open_round(plan_data(laboratories=("a", "b", "c")))
        self.observe("a", ["10.001", "10.003"])
        self.observe("b", ["10.004", "10.006"])
        self.observe("c", ["10.005", "10.007"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        self.submit(round_id, "b", {"m1": ["b1", "b2"]}, at="2026-11-22T00:00:00Z")
        self.submit(round_id, "c", {"m1": ["c1", "c2"]})
        self.app.proficiency.withdraw(round_id, "sub-c", "synthetic withdrawal")
        self.close_and_score(round_id)
        review = self.app.proficiency_review.review(round_id)
        states = {
            (item["laboratory"], item["status"])
            for item in review["excluded_submissions"]
        }
        self.assertEqual(states, {("lab-b", "late"), ("lab-c", "withdrawn")})
        scored = review["measurands"]["m1"]["scores"]
        self.assertEqual([entry["laboratory"] for entry in scored], ["lab-a"])

    def test_reopen_allows_additional_submissions(self):
        round_id = self.open_round(plan_data(laboratories=("a", "b")))
        self.observe("a", ["10.001", "10.003"])
        self.submit(round_id, "a", {"m1": ["a1", "a2"]})
        first = self.close_and_score(round_id)
        self.app.proficiency.reopen(round_id, 3, "admin-1", "late laboratory")
        self.observe("b", ["10.004", "10.006"])
        self.submit(round_id, "b", {"m1": ["b1", "b2"]})
        self.app.proficiency.close(round_id, 4)
        second = self.app.proficiency_review.score(round_id)
        self.assertEqual(second["version"], 2)
        review = self.app.proficiency_review.review(round_id, 2)
        self.assertEqual(len(review["measurands"]["m1"]["scores"]), 2)
        self.assertEqual(first["version"], 1)


class ReportAndExportTests(ProficiencyFixture):
    def setUp(self):
        super().setUp()
        for lab in ("a", "b", "c"):
            self.add_laboratory(lab)
        measurands = [
            {
                "id": "m1",
                "item": "artifact-a",
                "unit": "kPa",
                "sigma": ".02",
                "reference": reference(),
            },
            {
                "id": "m2",
                "item": "artifact-a",
                "unit": "kPa",
                "sigma": ".02",
                "reference": {"kind": "consensus"},
            },
        ]
        data = plan_data(laboratories=("a", "b", "c"), measurands=measurands)
        self.round_id = self.open_round(data)
        self.observe("a", ["10.001", "10.003", "10.001", "10.003"])
        self.observe("b", ["10.004", "10.006", "10.004", "10.006"])
        self.observe("c", ["10.061", "10.063", "10.064", "10.066"])
        for lab in ("a", "b", "c"):
            self.submit(
                self.round_id,
                lab,
                {
                    "m1": [f"{lab}1", f"{lab}2"],
                    "m2": [f"{lab}3", f"{lab}4"],
                },
            )
        self.close_and_score(self.round_id)

    def test_lab_report_shows_inputs_and_reasons(self):
        report = self.app.proficiency_review.lab_report(self.round_id, "lab-c")
        self.assertEqual(report["laboratory"], "lab-c")
        self.assertIn("scope", report)
        entry = next(score for score in report["scores"] if score["measurand"] == "m1")
        self.assertEqual(entry["summary"]["mean"], "10.062")
        self.assertEqual(entry["assigned"]["source"], "synthetic reference certificate")
        self.assertEqual(entry["assigned"]["declared_uncertainty"]["kind"], "expanded")
        self.assertEqual(entry["sigma"], "0.02")
        self.assertEqual(entry["evaluation"], {"satisfactory": "2", "warning": "3"})
        self.assertEqual(entry["classification"], "unsatisfactory")
        self.assertEqual(len(entry["items"]), 2)
        self.assertEqual(entry["items"][0]["state"], "valid")
        consensus_entry = next(
            score for score in report["scores"] if score["measurand"] == "m2"
        )
        self.assertEqual(consensus_entry["interpretation"], "limited")
        self.assertIn("consensus", consensus_entry["notes"][0])

    def test_lab_report_requires_registered_laboratory(self):
        with self.assertRaises(Missing):
            self.app.proficiency_review.lab_report(self.round_id, "lab-ghost")

    def test_manager_review_shows_distribution_and_influence(self):
        review = self.app.proficiency_review.review(self.round_id)
        consensus = review["measurands"]["m2"]
        self.assertEqual(consensus["assigned"]["value"], "10.005")
        self.assertEqual(consensus["distribution"]["median"], "10.005")
        self.assertEqual(consensus["distribution"]["count"], 3)
        self.assertEqual(consensus["unsatisfactory"], ["lab-c"])
        influence = {entry["laboratory"] for entry in consensus["influence"]}
        self.assertEqual(influence, {"lab-a", "lab-b", "lab-c"})
        self.assertEqual(review["version"], 1)
        self.assertIn("scope", review)

    def test_versions_are_listed(self):
        versions = self.app.proficiency_review.versions(self.round_id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)

    def test_export_roundtrip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "review.json"
            self.app.proficiency_review.export(self.round_id, destination)
            verified = verify_export(destination)
            self.assertEqual(verified["round"], self.round_id)
            self.assertEqual(verified["version"], 1)
            self.assertEqual(verified["measurands"], 2)
            envelope = json.loads(destination.read_text())
            envelope["payload"]["measurands"]["m1"]["scores"][0]["z"] = "99"
            destination.write_text(json.dumps(envelope))
            with self.assertRaises(IntegrityError):
                verify_export(destination)

    def test_console_dispatch_reaches_proficiency_actions(self):
        round_info = dispatch(self.app, {"action": "pt_round", "id": self.round_id})
        self.assertEqual(round_info["state"], "closed")
        versions = dispatch(self.app, {"action": "pt_versions", "id": self.round_id})
        self.assertEqual(len(versions), 1)
        review = dispatch(self.app, {"action": "pt_review", "id": self.round_id})
        self.assertEqual(review["version"], 1)
        report = dispatch(
            self.app,
            {"action": "pt_lab_report", "id": self.round_id, "laboratory": "lab-a"},
        )
        self.assertEqual(report["laboratory"], "lab-a")
        submissions = dispatch(
            self.app, {"action": "pt_submissions", "id": self.round_id}
        )
        self.assertEqual(len(submissions), 3)


class ScoringPrimitiveTests(unittest.TestCase):
    def test_per_laboratory_summary(self):
        summary = scoring.per_laboratory_summary(
            [(Decimal("10.001"), Decimal(".003")), (Decimal("10.003"), Decimal(".003"))]
        )
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["mean"], "10.002")
        with localcontext() as ctx:
            ctx.prec = 40
            expected_u = (Decimal("0.003") ** 2 * 2).sqrt() / 2
        self.assertEqual(summary["standard_uncertainty"], decimal_text(expected_u))
        with self.assertRaises(InputError):
                scoring.per_laboratory_summary([])

    def test_classify_uses_fixed_boundaries(self):
        evaluation = {"satisfactory": "2", "warning": "3"}
        self.assertEqual(scoring.classify("0", evaluation), "satisfactory")
        self.assertEqual(scoring.classify("-2", evaluation), "satisfactory")
        self.assertEqual(scoring.classify("2.0001", evaluation), "warning")
        self.assertEqual(scoring.classify("-2.9999", evaluation), "warning")
        self.assertEqual(scoring.classify("3", evaluation), "unsatisfactory")

    def test_zeta_guards(self):
        value, reason = scoring.zeta("1", "0", "0")
        self.assertIsNone(value)
        self.assertEqual(reason, "zero_scale")
        value, reason = scoring.zeta("1", ".001", ".001", covariance=".0000011")
        self.assertIsNone(value)
        self.assertEqual(reason, "nonpositive_variance")
        value, reason = scoring.zeta("0", ".001", ".001")
        self.assertEqual(value, 0)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
