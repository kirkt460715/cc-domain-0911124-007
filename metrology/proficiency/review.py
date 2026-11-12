"""Scoring orchestration, per-laboratory reports, manager review, and export.

Scoring runs only on a closed round and produces immutable, versioned
payloads.  Re-scoring with unchanged inputs reuses the latest version;
a changed reference, new or withdrawn submissions, or a revoked certificate
yields a new version while earlier notifications stay untouched.
"""
from collections import Counter
from decimal import localcontext

from ..errors import InputError, IntegrityError, Missing, StateError
from ..ledger.audit import append
from ..output.snapshots import atomic_write
from ..processing.diagnostics import robust_summary
from ..values import canonical, decimal_text, digest, identifier, number, strict_json
from . import scoring

SCOPE_NOTE = (
    "Scores apply only to the results submitted for the registered measurands "
    "and test items of this round. They are performance indicators for this "
    "comparison and do not establish method equivalence, measurement "
    "capability, or competence beyond this round."
)


class ProficiencyReview:
    def __init__(self, database, rounds, catalog):
        self.db = database
        self.rounds = rounds
        self.catalog = catalog
        with self.db.transaction() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS pt_scoring(round TEXT NOT NULL,
                version INTEGER NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(round,version))"""
            )

    def _laboratory_entries(self, specification, submissions, revoked, registry):
        unit = specification["unit"]
        entries = []
        for submission in submissions:
            pins = submission["items"].get(specification["id"])
            if pins is None:
                continue
            items = []
            valid = []
            for pin in pins:
                row = self.db.one(
                    "SELECT payload FROM results WHERE observation=? AND revision=?",
                    (pin["observation"], pin["revision"]),
                )
                if row is None or row["payload"] is None:
                    items.append({**pin, "state": "missing_measurement"})
                    continue
                measurement = strict_json(row["payload"])
                value = registry.convert(
                    number(measurement["corrected_value"]),
                    measurement["unit"],
                    unit,
                )
                uncertainty = registry.uncertainty(
                    number(measurement["corrected_uncertainty"]),
                    measurement["unit"],
                    unit,
                )
                state = (
                    "certificate_invalidated"
                    if measurement["certificate"] in revoked
                    else "valid"
                )
                items.append(
                    {
                        **pin,
                        "state": state,
                        "value": decimal_text(value),
                        "standard_uncertainty": decimal_text(uncertainty),
                    }
                )
                if state == "valid":
                    valid.append((value, uncertainty))
            entry = {
                "laboratory": submission["laboratory"],
                "submission": submission["id"],
                "items": items,
            }
            if not valid:
                entry.update(
                    {"status": "excluded", "reasons": ["certificate_invalidated"]}
                )
            else:
                entry["summary"] = scoring.per_laboratory_summary(valid)
            entries.append(entry)
        entries.sort(key=lambda entry: entry["laboratory"])
        return entries

    def _assigned(self, specification, reference, entries, registry):
        unit = specification["unit"]
        if reference["kind"] == "reference":
            value = registry.convert(reference["value"], reference["unit"], unit)
            uncertainty = registry.uncertainty(
                scoring.reference_uncertainty(reference), reference["unit"], unit
            )
            return {
                "kind": "reference",
                "source": reference["source"],
                "value": decimal_text(value),
                "standard_uncertainty": decimal_text(uncertainty),
                "declared_uncertainty": reference["uncertainty"],
                "unit": unit,
            }, None
        means = [
            {"laboratory": entry["laboratory"], "mean": entry["summary"]["mean"]}
            for entry in entries
            if "summary" in entry
        ]
        if len(means) < scoring.MINIMUM_CONSENSUS_PARTICIPANTS:
            return None, {
                "kind": "consensus",
                "participants": len(means),
                "reason": "insufficient_participants",
            }
        result = scoring.consensus(means)
        return {
            "kind": "consensus",
            "value": result["median"],
            "standard_uncertainty": result["standard_uncertainty"],
            "unit": unit,
            **result,
        }, None

    def _score_laboratory(self, entry, specification, assigned):
        summary = entry["summary"]
        evaluation = specification["evaluation"]
        with localcontext() as ctx:
            ctx.prec = 40
            difference = number(summary["mean"]) - number(assigned["value"])
            z_value = scoring.z_scores(difference, specification["sigma"])
        notes = []
        limited = False
        covariance = None
        correlations = specification["correlations"]
        if assigned["kind"] == "reference":
            pair = next(
                (
                    item
                    for item in correlations["covariances"]
                    if entry["laboratory"] in item["between"]
                ),
                None,
            )
            if pair is not None:
                covariance = pair["covariance"]
                notes.append("registered covariance applied: " + pair["basis"])
            elif any(
                group["includes_assigned_reference"]
                and entry["laboratory"] in group["laboratories"]
                for group in correlations["shared_references"]
            ):
                notes.append(
                    "shared reference declared without a registered covariance"
                )
                limited = True
        else:
            notes.append("laboratory contributes to the consensus value")
            limited = True
        zeta_value, zeta_reason = scoring.zeta(
            difference,
            summary["standard_uncertainty"],
            assigned["standard_uncertainty"],
            covariance,
        )
        if zeta_reason is not None:
            notes.append("zeta not computed: " + zeta_reason)
        interpretation = "limited" if notes else "standard"
        result = {
            "laboratory": entry["laboratory"],
            "submission": entry["submission"],
            "status": "scored",
            "items": entry["items"],
            "summary": summary,
            "difference": decimal_text(difference),
            "z": decimal_text(z_value),
            "classification": scoring.classify(z_value, evaluation),
            "zeta": decimal_text(zeta_value) if zeta_value is not None else None,
            "covariance": covariance,
            "interpretation": "limited" if limited else "standard",
            "notes": notes,
            "reasons": [],
        }
        if zeta_reason is not None:
            result["reasons"].append(zeta_reason)
        return result

    def score(self, round_id):
        with self.db.transaction() as con:
            round_row = self.rounds.get(round_id)
            if round_row["state"] != "closed":
                raise StateError("round must be closed before scoring")
            plan = round_row["plan"]
            registry = self.catalog.registry()
            revoked = {
                row["id"]
                for row in self.db.rows(
                    "SELECT id FROM certificates WHERE revoked=1"
                )
            }
            submissions = self.rounds.submissions(round_id)
            effective = [item for item in submissions if item["status"] == "submitted"]
            excluded = [
                {
                    "submission": item["id"],
                    "laboratory": item["laboratory"],
                    "status": item["status"],
                    "measurands": sorted(item["items"]),
                }
                for item in submissions
                if item["status"] != "submitted"
            ]
            evaluation = plan["evaluation"]
            measurands = {}
            notifications = {}
            for specification in plan["measurands"]:
                spec = {**specification, "evaluation": evaluation}
                entries = self._laboratory_entries(
                    specification, effective, revoked, registry
                )
                assigned, failure = self._assigned(
                    specification,
                    round_row["references"][specification["id"]],
                    entries,
                    registry,
                )
                scored = []
                for entry in entries:
                    if "summary" not in entry:
                        scored.append({**entry, "notes": []})
                        continue
                    if assigned is None:
                        scored.append(
                            {
                                **entry,
                                "status": "not_computable",
                                "reasons": [failure["reason"]],
                                "notes": [],
                            }
                        )
                        continue
                    scored.append(self._score_laboratory(entry, spec, assigned))
                measurands[specification["id"]] = {
                    "unit": specification["unit"],
                    "sigma": specification["sigma"],
                    "assigned": assigned if assigned is not None else failure,
                    "laboratories": scored,
                }
                for entry in scored:
                    notifications.setdefault(
                        entry["laboratory"], {"laboratory": entry["laboratory"], "scores": []}
                    )["scores"].append(
                        {
                            "measurand": specification["id"],
                            "status": entry["status"],
                            "classification": entry.get("classification"),
                            "z": entry.get("z"),
                            "zeta": entry.get("zeta"),
                            "reasons": entry.get("reasons", []),
                        }
                    )
            content = {
                "round": round_id,
                "plan_fingerprint": digest(plan),
                "round_revision": round_row["revision"],
                "evaluation": evaluation,
                "references": round_row["references"],
                "measurands": measurands,
                "notifications": notifications,
                "excluded_submissions": excluded,
            }
            fingerprint = digest(content)
            latest = con.execute(
                "SELECT version,fingerprint FROM pt_scoring WHERE round=? ORDER BY version DESC LIMIT 1",
                (round_id,),
            ).fetchone()
            if latest and latest["fingerprint"] == fingerprint:
                return {
                    "round": round_id,
                    "version": latest["version"],
                    "fingerprint": fingerprint,
                    "reused": True,
                }
            version = (latest["version"] if latest else 0) + 1
            payload = {"version": version, **content}
            con.execute(
                "INSERT INTO pt_scoring VALUES(?,?,?,?)",
                (round_id, version, canonical(payload), fingerprint),
            )
            append(
                con,
                "pt_scored",
                round_id,
                {"version": version, "fingerprint": fingerprint},
            )
        return {
            "round": round_id,
            "version": version,
            "fingerprint": fingerprint,
            "reused": False,
        }

    def versions(self, round_id):
        self.rounds.get(round_id)
        return [
            {"version": row["version"], "fingerprint": row["fingerprint"]}
            for row in self.db.rows(
                "SELECT version,fingerprint FROM pt_scoring WHERE round=? ORDER BY version",
                (round_id,),
            )
        ]

    def version_payload(self, round_id, version=None):
        self.rounds.get(round_id)
        if version is None:
            row = self.db.one(
                "SELECT * FROM pt_scoring WHERE round=? ORDER BY version DESC LIMIT 1",
                (round_id,),
            )
        else:
            row = self.db.one(
                "SELECT * FROM pt_scoring WHERE round=? AND version=?",
                (round_id, version),
            )
        if row is None:
            raise Missing("scoring version not found", field=round_id)
        payload = strict_json(row["payload"])
        content = {key: value for key, value in payload.items() if key != "version"}
        if digest(content) != row["fingerprint"]:
            raise IntegrityError("scoring payload fingerprint differs")
        return payload

    def lab_report(self, round_id, laboratory, version=None):
        laboratory = identifier(laboratory, "laboratory")
        round_row = self.rounds.get(round_id)
        if laboratory not in {
            item["id"] for item in round_row["plan"]["laboratories"]
        }:
            raise Missing("laboratory not registered in round", field=laboratory)
        payload = self.version_payload(round_id, version)
        scores = []
        for measurand_id, result in sorted(payload["measurands"].items()):
            entry = next(
                (
                    item
                    for item in result["laboratories"]
                    if item["laboratory"] == laboratory
                ),
                None,
            )
            if entry is None:
                continue
            scores.append(
                {
                    "measurand": measurand_id,
                    "unit": result["unit"],
                    "sigma": result["sigma"],
                    "evaluation": payload["evaluation"],
                    "assigned": result["assigned"],
                    **entry,
                }
            )
        return {
            "round": round_id,
            "version": payload["version"],
            "laboratory": laboratory,
            "scores": scores,
            "scope": SCOPE_NOTE,
        }

    def review(self, round_id, version=None):
        round_row = self.rounds.get(round_id)
        payload = self.version_payload(round_id, version)
        measurands = {}
        for measurand_id, result in sorted(payload["measurands"].items()):
            scored = [
                entry
                for entry in result["laboratories"]
                if entry["status"] == "scored"
            ]
            distribution = robust_summary(
                [entry["summary"]["mean"] for entry in scored]
            )
            distribution["outliers"] = [
                scored[index]["laboratory"] for index in distribution["outliers"]
            ]
            counts = Counter(entry["classification"] for entry in scored)
            assigned = result["assigned"]
            influence = []
            if assigned.get("kind") == "consensus" and "leave_one_out" in assigned:
                influence = sorted(
                    assigned["leave_one_out"],
                    key=lambda item: abs(number(item["shift"])),
                    reverse=True,
                )
            measurands[measurand_id] = {
                "unit": result["unit"],
                "sigma": result["sigma"],
                "assigned": assigned,
                "distribution": distribution,
                "classifications": dict(sorted(counts.items())),
                "unsatisfactory": sorted(
                    entry["laboratory"]
                    for entry in scored
                    if entry["classification"] == "unsatisfactory"
                ),
                "influence": influence,
                "scores": [
                    {
                        key: entry.get(key)
                        for key in (
                            "laboratory",
                            "status",
                            "classification",
                            "z",
                            "zeta",
                            "interpretation",
                            "reasons",
                        )
                    }
                    for entry in result["laboratories"]
                ],
            }
        return {
            "kind": "proficiency_round_review",
            "schema": 1,
            "round": round_id,
            "state": round_row["state"],
            "version": payload["version"],
            "evaluation": payload["evaluation"],
            "measurands": measurands,
            "excluded_submissions": payload["excluded_submissions"],
            "scope": SCOPE_NOTE,
        }

    def export(self, round_id, destination, version=None):
        review = self.review(round_id, version)
        envelope = {"fingerprint": digest(review), "payload": review}
        return atomic_write(destination, (canonical(envelope) + "\n").encode("utf-8"))


def verify_export(path):
    from ..ingress.readers import load_text

    value = strict_json(load_text(path))
    if not isinstance(value, dict) or set(value) != {"payload", "fingerprint"}:
        raise InputError("invalid review export envelope")
    if digest(value["payload"]) != value["fingerprint"]:
        raise IntegrityError("review export fingerprint mismatch")
    payload = value["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("kind") != "proficiency_round_review"
    ):
        raise InputError("unsupported review export schema")
    return {
        "round": payload["round"],
        "version": payload["version"],
        "fingerprint": value["fingerprint"],
        "measurands": len(payload["measurands"]),
    }
