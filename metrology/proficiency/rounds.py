"""Proficiency round registration, lifecycle, and submissions.

A round plan freezes laboratories, test items, measurands, the submission
deadline, replicate rules, and the evaluation boundaries before collection
starts.  Reference specifications are stored separately so an administrator
can replace a faulty reference between scoring versions without rewriting
the plan; every change is audited and bumps the round revision.
"""
from ..errors import Conflict, InputError, Missing, StateError
from ..ledger.audit import append
from ..values import (
    bool_value,
    bounded_list,
    canonical,
    decimal_text,
    digest,
    identifier,
    instant,
    integer,
    label,
    number,
    require_object,
    strict_json,
    time_text,
)

REFERENCE_PARTIES = "reference"


def parse_reference(data, registry, unit):
    require_object(
        data,
        {"kind", "source", "value", "unit", "uncertainty"},
        {"kind"},
    )
    kind = data["kind"]
    if kind == "consensus":
        if set(data) != {"kind"}:
            raise InputError("consensus reference takes no further fields")
        return {"kind": "consensus"}
    if kind != "reference":
        raise InputError("unknown reference kind", field="kind")
    source = label(data["source"], "source")
    value = number(data["value"], "value")
    reference_unit = identifier(data["unit"], "unit")
    registry.validate_pair(reference_unit, unit)
    raw = require_object(
        data.get("uncertainty"),
        {"kind", "value", "coverage_factor"},
        {"kind", "value"},
    )
    uncertainty = {
        "kind": raw["kind"],
        "value": decimal_text(number(raw["value"], "uncertainty", nonnegative=True)),
    }
    if raw["kind"] == "expanded":
        if "coverage_factor" not in raw:
            raise InputError("expanded uncertainty requires a coverage factor")
        uncertainty["coverage_factor"] = decimal_text(
            number(raw["coverage_factor"], "coverage_factor", positive=True)
        )
    elif raw["kind"] == "standard":
        if "coverage_factor" in raw:
            raise InputError("standard uncertainty has no coverage factor")
    else:
        raise InputError("unknown uncertainty kind", field="uncertainty")
    return {
        "kind": "reference",
        "source": source,
        "value": decimal_text(value),
        "unit": reference_unit,
        "uncertainty": uncertainty,
    }


def parse_correlations(data, laboratory_ids):
    require_object(data, {"shared_references", "covariances"})
    groups = []
    for group in bounded_list(data.get("shared_references", []), "shared_references", 16):
        require_object(
            group,
            {"id", "laboratories", "includes_assigned_reference", "description"},
            {"id", "laboratories"},
        )
        members = bounded_list(group["laboratories"], "laboratories", 64)
        if len(members) < 2 or len(set(members)) != len(members):
            raise InputError("shared reference needs at least two distinct laboratories")
        members = sorted(identifier(member, "laboratory") for member in members)
        unknown = set(members) - laboratory_ids
        if unknown:
            raise InputError(
                "shared reference names unregistered laboratories",
                details={"laboratories": sorted(unknown)},
            )
        groups.append(
            {
                "id": identifier(group["id"]),
                "laboratories": members,
                "includes_assigned_reference": bool_value(
                    group.get("includes_assigned_reference", False),
                    "includes_assigned_reference",
                ),
                "description": label(
                    group.get("description", "shared reference"), "description"
                ),
            }
        )
    if len({group["id"] for group in groups}) != len(groups):
        raise InputError("duplicate shared reference group")
    covariances = []
    seen_pairs = set()
    for entry in bounded_list(data.get("covariances", []), "covariances", 64):
        require_object(entry, {"between", "covariance", "basis"}, {"between", "covariance", "basis"})
        between = bounded_list(entry["between"], "between", 2)
        if len(between) != 2:
            raise InputError("covariance requires exactly two parties")
        parties = sorted(identifier(party, "party") for party in between)
        if parties[0] == parties[1]:
            raise InputError("covariance parties must differ")
        if REFERENCE_PARTIES not in parties:
            raise InputError("covariance must involve the assigned reference")
        for party in parties:
            if party != REFERENCE_PARTIES and party not in laboratory_ids:
                raise InputError(
                    "covariance names an unregistered laboratory", field=party
                )
        pair = tuple(parties)
        if pair in seen_pairs:
            raise InputError("duplicate covariance for a party pair")
        seen_pairs.add(pair)
        covariances.append(
            {
                "between": parties,
                "covariance": decimal_text(number(entry["covariance"], "covariance")),
                "basis": label(entry["basis"], "basis"),
            }
        )
    return {"shared_references": groups, "covariances": covariances}


def parse_plan(data, catalog):
    fields = {
        "id",
        "label",
        "items",
        "measurands",
        "laboratories",
        "deadline",
        "evaluation",
    }
    require_object(data, fields, fields)
    registry = catalog.registry()
    round_id = identifier(data["id"])
    items = []
    for item in bounded_list(data["items"], "items", 64):
        require_object(item, {"id", "label"}, {"id", "label"})
        items.append({"id": identifier(item["id"]), "label": label(item["label"])})
    if not items or len({item["id"] for item in items}) != len(items):
        raise InputError("round requires one to sixty-four distinct test items")
    items.sort(key=lambda item: item["id"])
    item_ids = {item["id"] for item in items}

    laboratories = []
    for laboratory in bounded_list(data["laboratories"], "laboratories", 64):
        require_object(
            laboratory,
            {"id", "label", "channels", "replicates"},
            {"id", "label", "channels", "replicates"},
        )
        channels = {}
        raw_channels = require_object(laboratory["channels"])
        if not raw_channels or len(raw_channels) > 32:
            raise InputError("laboratory must map one to thirty-two measurands")
        for key, target in raw_channels.items():
            require_object(
                target, {"instrument", "channel"}, {"instrument", "channel"}
            )
            instrument = identifier(target["instrument"], "instrument")
            channel = identifier(target["channel"], "channel")
            catalog.channel(instrument, channel)
            channels[identifier(key, "measurand")] = {
                "instrument": instrument,
                "channel": channel,
            }
        replicates = require_object(
            laboratory["replicates"], {"minimum", "maximum"}, {"minimum", "maximum"}
        )
        minimum = integer(replicates["minimum"], "minimum", 1, 100)
        maximum = integer(replicates["maximum"], "maximum", 1, 100)
        if maximum < minimum:
            raise InputError("replicate maximum is below the minimum")
        laboratories.append(
            {
                "id": identifier(laboratory["id"]),
                "label": label(laboratory["label"]),
                "channels": channels,
                "replicates": {"minimum": minimum, "maximum": maximum},
            }
        )
    if not laboratories:
        raise InputError("round requires at least one laboratory")
    laboratory_ids = {laboratory["id"] for laboratory in laboratories}
    if len(laboratory_ids) != len(laboratories):
        raise InputError("duplicate laboratory")
    laboratories.sort(key=lambda laboratory: laboratory["id"])

    measurands = []
    for measurand in bounded_list(data["measurands"], "measurands", 32):
        require_object(
            measurand,
            {"id", "item", "unit", "sigma", "reference", "correlations"},
            {"id", "item", "unit", "sigma", "reference"},
        )
        item = identifier(measurand["item"], "item")
        if item not in item_ids:
            raise InputError("measurand references an unknown test item", field=item)
        unit = identifier(measurand["unit"], "unit")
        registry.get(unit)
        measurands.append(
            {
                "id": identifier(measurand["id"]),
                "item": item,
                "unit": unit,
                "sigma": decimal_text(
                    number(measurand["sigma"], "sigma", positive=True)
                ),
                "reference": parse_reference(measurand["reference"], registry, unit),
                "correlations": parse_correlations(
                    measurand.get("correlations", {}), laboratory_ids
                ),
            }
        )
    if not measurands or len({m["id"] for m in measurands}) != len(measurands):
        raise InputError("round requires one to thirty-two distinct measurands")
    measurands.sort(key=lambda measurand: measurand["id"])
    measurand_ids = {measurand["id"] for measurand in measurands}
    covered = set()
    for laboratory in laboratories:
        unknown = set(laboratory["channels"]) - measurand_ids
        if unknown:
            raise InputError(
                "laboratory maps an unknown measurand",
                details={"measurands": sorted(unknown)},
            )
        covered |= set(laboratory["channels"])
    if covered != measurand_ids:
        raise InputError(
            "every measurand must be mapped by at least one laboratory",
            details={"unmapped": sorted(measurand_ids - covered)},
        )

    evaluation = require_object(
        data["evaluation"], {"satisfactory", "warning"}, {"satisfactory", "warning"}
    )
    satisfactory = number(evaluation["satisfactory"], "satisfactory", positive=True)
    warning = number(evaluation["warning"], "warning", positive=True)
    if warning <= satisfactory:
        raise InputError("evaluation boundaries must increase")
    return {
        "id": round_id,
        "label": label(data["label"]),
        "items": items,
        "measurands": measurands,
        "laboratories": laboratories,
        "deadline": time_text(instant(data["deadline"], "deadline")),
        "evaluation": {
            "satisfactory": decimal_text(satisfactory),
            "warning": decimal_text(warning),
        },
    }


class ProficiencyRounds:
    def __init__(self, database, catalog):
        self.db = database
        self.catalog = catalog
        with self.db.transaction() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS pt_rounds(id TEXT PRIMARY KEY,plan TEXT NOT NULL,
                fingerprint TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,refs TEXT NOT NULL)"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS pt_submissions(id TEXT PRIMARY KEY,round TEXT NOT NULL,
                laboratory TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL)"""
            )
            con.execute(
                """CREATE INDEX IF NOT EXISTS pt_submission_round
                ON pt_submissions(round,laboratory)"""
            )

    def plan(self, data):
        plan = parse_plan(data, self.catalog)
        fingerprint = digest(plan)
        references = {
            measurand["id"]: measurand["reference"]
            for measurand in plan["measurands"]
        }
        with self.db.transaction() as con:
            row = con.execute(
                "SELECT fingerprint FROM pt_rounds WHERE id=?", (plan["id"],)
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("round plan is immutable")
                return self.get(plan["id"])
            con.execute(
                "INSERT INTO pt_rounds VALUES(?,?,?,'planned',1,?)",
                (plan["id"], canonical(plan), fingerprint, canonical(references)),
            )
            append(con, "pt_round_planned", plan["id"], plan)
        return self.get(plan["id"])

    def get(self, round_id):
        row = self.db.one("SELECT * FROM pt_rounds WHERE id=?", (round_id,))
        if row is None:
            raise Missing("proficiency round not found", field=round_id)
        counts = self.db.rows(
            "SELECT status,COUNT(*) AS n FROM pt_submissions WHERE round=? GROUP BY status",
            (round_id,),
        )
        return {
            "id": round_id,
            "state": row["state"],
            "revision": row["revision"],
            "plan": strict_json(row["plan"]),
            "references": strict_json(row["refs"]),
            "submissions": {entry["status"]: entry["n"] for entry in counts},
        }

    def _expect(self, round_id, expected_revision):
        integer(expected_revision, "revision", 1)
        round_row = self.get(round_id)
        if round_row["revision"] != expected_revision:
            raise Conflict("round revision changed")
        return round_row

    def _transition(self, round_id, expected_revision, source, target, kind, extra=None):
        with self.db.transaction() as con:
            round_row = self._expect(round_id, expected_revision)
            if round_row["state"] != source:
                raise StateError(
                    f"only a {source} round can become {target}",
                    details={"state": round_row["state"]},
                )
            con.execute(
                "UPDATE pt_rounds SET state=?,revision=revision+1 WHERE id=?",
                (target, round_id),
            )
            payload = {"revision": expected_revision + 1}
            payload.update(extra or {})
            append(con, kind, round_id, payload)
        return self.get(round_id)

    def open(self, round_id, expected_revision):
        return self._transition(
            round_id, expected_revision, "planned", "open", "pt_round_opened"
        )

    def close(self, round_id, expected_revision):
        return self._transition(
            round_id, expected_revision, "open", "closed", "pt_round_closed"
        )

    def reopen(self, round_id, expected_revision, administrator, reason):
        return self._transition(
            round_id,
            expected_revision,
            "closed",
            "open",
            "pt_round_reopened",
            {
                "administrator": identifier(administrator, "administrator"),
                "reason": label(reason, "reason"),
            },
        )

    def change_reference(
        self, round_id, measurand_id, reference, expected_revision, administrator, reason
    ):
        measurand_id = identifier(measurand_id, "measurand")
        administrator = identifier(administrator, "administrator")
        reason = label(reason, "reason")
        with self.db.transaction() as con:
            round_row = self._expect(round_id, expected_revision)
            if round_row["state"] == "open":
                raise StateError("reference cannot change while the round is open")
            spec = next(
                (
                    item
                    for item in round_row["plan"]["measurands"]
                    if item["id"] == measurand_id
                ),
                None,
            )
            if spec is None:
                raise Missing("measurand not registered", field=measurand_id)
            parsed = parse_reference(
                reference, self.catalog.registry(), spec["unit"]
            )
            references = dict(round_row["references"])
            if references[measurand_id] == parsed:
                return {**self.get(round_id), "changed": False}
            references[measurand_id] = parsed
            con.execute(
                "UPDATE pt_rounds SET refs=?,revision=revision+1 WHERE id=?",
                (canonical(references), round_id),
            )
            append(
                con,
                "pt_reference_changed",
                round_id,
                {
                    "measurand": measurand_id,
                    "reference": parsed,
                    "administrator": administrator,
                    "reason": reason,
                    "revision": expected_revision + 1,
                },
            )
        return {**self.get(round_id), "changed": True}

    def submit(self, data):
        fields = {"id", "round", "laboratory", "submitted_at", "measurands"}
        require_object(data, fields, fields)
        submission_id = identifier(data["id"])
        round_id = identifier(data["round"], "round")
        laboratory_id = identifier(data["laboratory"], "laboratory")
        submitted_at = instant(data["submitted_at"], "submitted_at")
        with self.db.transaction() as con:
            round_row = self.get(round_id)
            if round_row["state"] != "open":
                raise StateError("round is not collecting submissions")
            plan = round_row["plan"]
            laboratory = next(
                (
                    item
                    for item in plan["laboratories"]
                    if item["id"] == laboratory_id
                ),
                None,
            )
            if laboratory is None:
                raise Missing("laboratory not registered in round", field=laboratory_id)
            deadline = instant(plan["deadline"])
            status = "late" if submitted_at > deadline else "submitted"
            old = con.execute(
                "SELECT id FROM pt_submissions WHERE id=?",
                (submission_id,),
            ).fetchone()
            if old:
                raise Conflict("submission id already used", field=submission_id)
            active = con.execute(
                "SELECT id FROM pt_submissions WHERE round=? AND laboratory=? AND status IN('submitted','late')",
                (round_id, laboratory_id),
            ).fetchone()
            if active:
                raise Conflict(
                    "laboratory already has an active submission",
                    details={"submission": active["id"]},
                )
            used = set()
            for row in con.execute(
                "SELECT payload FROM pt_submissions WHERE round=? AND status IN('submitted','late')",
                (round_id,),
            ).fetchall():
                for pins in strict_json(row["payload"])["items"].values():
                    used.update(pin["observation"] for pin in pins)
            specifications = {
                item["id"]: item for item in plan["measurands"]
            }
            raw_measurands = require_object(data["measurands"])
            if not raw_measurands:
                raise InputError("submission must cover at least one measurand")
            registry = self.catalog.registry()
            items = {}
            seen = set()
            for key, raw_observations in raw_measurands.items():
                measurand_id = identifier(key, "measurand")
                if measurand_id not in laboratory["channels"]:
                    raise InputError(
                        "laboratory is not registered for measurand",
                        field=measurand_id,
                    )
                observations = bounded_list(raw_observations, "observations", 100)
                if not observations:
                    raise InputError("submission requires at least one measurement")
                identifiers = [identifier(value, "observation") for value in observations]
                if len(set(identifiers)) != len(identifiers):
                    raise InputError("duplicate observation in submission")
                rule = laboratory["replicates"]
                if not rule["minimum"] <= len(identifiers) <= rule["maximum"]:
                    raise InputError(
                        "replicate count outside the registered rule",
                        details={
                            "measurand": measurand_id,
                            "count": len(identifiers),
                            "rule": rule,
                        },
                    )
                target = laboratory["channels"][measurand_id]
                unit = specifications[measurand_id]["unit"]
                pins = []
                for observation_id in identifiers:
                    if observation_id in used or observation_id in seen:
                        raise Conflict(
                            "observation already submitted in this round",
                            field=observation_id,
                        )
                    row = con.execute(
                        """SELECT o.instrument,o.channel,r.revision,r.payload,r.error
                        FROM observations o JOIN results r ON r.observation=o.id AND r.active=1
                        WHERE o.id=?""",
                        (observation_id,),
                    ).fetchone()
                    if row is None:
                        raise Missing("observation not found", field=observation_id)
                    if row["error"] or row["payload"] is None:
                        raise InputError(
                            "observation has no usable measurement",
                            field=observation_id,
                        )
                    measurement = strict_json(row["payload"])
                    if not measurement["accepted"]:
                        raise InputError(
                            "rejected measurement cannot be submitted",
                            field=observation_id,
                        )
                    if (
                        row["instrument"] != target["instrument"]
                        or row["channel"] != target["channel"]
                    ):
                        raise InputError(
                            "observation channel does not match laboratory registration",
                            field=observation_id,
                        )
                    registry.validate_pair(measurement["unit"], unit)
                    certificate = con.execute(
                        "SELECT fingerprint FROM certificates WHERE id=?",
                        (measurement["certificate"],),
                    ).fetchone()
                    if certificate is None:
                        raise Missing("certificate not found")
                    pins.append(
                        {
                            "observation": observation_id,
                            "revision": row["revision"],
                            "certificate": measurement["certificate"],
                            "certificate_fingerprint": certificate["fingerprint"],
                        }
                    )
                    seen.add(observation_id)
                pins.sort(key=lambda pin: pin["observation"])
                items[measurand_id] = pins
            payload = {
                "id": submission_id,
                "round": round_id,
                "laboratory": laboratory_id,
                "submitted_at": time_text(submitted_at),
                "deadline": plan["deadline"],
                "items": items,
            }
            con.execute(
                "INSERT INTO pt_submissions VALUES(?,?,?,?,?)",
                (submission_id, round_id, laboratory_id, status, canonical(payload)),
            )
            append(
                con,
                "pt_submission_received",
                submission_id,
                {
                    "round": round_id,
                    "laboratory": laboratory_id,
                    "status": status,
                    "measurands": sorted(items),
                },
            )
        return {"id": submission_id, "status": status, "payload": payload}

    def withdraw(self, round_id, submission_id, reason):
        reason = label(reason, "reason")
        with self.db.transaction() as con:
            round_row = self.get(round_id)
            if round_row["state"] != "open":
                raise StateError("submissions can be withdrawn only while open")
            row = con.execute(
                "SELECT status FROM pt_submissions WHERE id=? AND round=?",
                (submission_id, round_id),
            ).fetchone()
            if row is None:
                raise Missing("submission not found", field=submission_id)
            if row["status"] not in ("submitted", "late"):
                raise StateError("submission is not active")
            con.execute(
                "UPDATE pt_submissions SET status='withdrawn' WHERE id=?",
                (submission_id,),
            )
            append(
                con,
                "pt_submission_withdrawn",
                submission_id,
                {"round": round_id, "reason": reason},
            )
        return {"id": submission_id, "status": "withdrawn"}

    def submissions(self, round_id, laboratory=None):
        self.get(round_id)
        if laboratory is not None:
            laboratory = identifier(laboratory, "laboratory")
            rows = self.db.rows(
                "SELECT * FROM pt_submissions WHERE round=? AND laboratory=? ORDER BY id",
                (round_id, laboratory),
            )
        else:
            rows = self.db.rows(
                "SELECT * FROM pt_submissions WHERE round=? ORDER BY id", (round_id,)
            )
        return [
            {"id": row["id"], "status": row["status"], **strict_json(row["payload"])}
            for row in rows
        ]
