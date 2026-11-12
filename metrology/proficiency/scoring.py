"""Pure scoring computations for interlaboratory proficiency rounds.

All arithmetic uses decimal context precision 40 and returns text decimals so
stored payloads stay deterministic.  Nothing in this module touches the
database; callers supply plain values and receive plain results.
"""
from decimal import Decimal, localcontext

from ..errors import InputError
from ..processing.diagnostics import percentile
from ..values import decimal_text, number

ROBUST_FACTOR = Decimal("1.483")
MINIMUM_CONSENSUS_PARTICIPANTS = 2


def per_laboratory_summary(items):
    """Summarize one laboratory's replicate measurements.

    items is a list of (value, standard_uncertainty) decimal pairs already
    expressed in the round unit.  The laboratory is represented by the
    arithmetic mean of its replicates; the standard uncertainty of that mean
    follows from the stated measurement uncertainties, so a laboratory with
    more replicates does not gain extra weight beyond its own plan.
    """
    if not items:
        raise InputError("laboratory summary requires at least one measurement")
    with localcontext() as ctx:
        ctx.prec = 40
        values = [item[0] for item in items]
        count = Decimal(len(values))
        mean = sum(values, Decimal(0)) / count
        uncertainty = (
            sum((item[1] ** 2 for item in items), Decimal(0)).sqrt() / count
        )
        if len(values) > 1:
            variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (
                count - 1
            )
            stddev = decimal_text(variance.sqrt())
        else:
            stddev = None
        return {
            "count": len(values),
            "mean": decimal_text(mean),
            "standard_uncertainty": decimal_text(uncertainty),
            "replicate_stddev": stddev,
        }


def consensus(means):
    """Robust assigned value from per-laboratory means.

    means is a list of {"laboratory", "mean"} mappings.  The assigned value
    is the median of the laboratory means and its standard uncertainty is
    1.483*MAD/sqrt(n).  Leave-one-out medians expose each participant's
    influence on the reference.
    """
    if len(means) < MINIMUM_CONSENSUS_PARTICIPANTS:
        raise InputError("consensus requires at least two participants")
    with localcontext() as ctx:
        ctx.prec = 40
        entries = [
            (entry["laboratory"], number(entry["mean"], "mean")) for entry in means
        ]
        values = [value for _, value in entries]
        median = percentile(values, ".5")
        mad = percentile([abs(value - median) for value in values], ".5")
        robust_sd = ROBUST_FACTOR * mad
        uncertainty = robust_sd / Decimal(len(values)).sqrt()
        leave_one_out = []
        for index, (laboratory, _) in enumerate(entries):
            remaining = [value for i, value in enumerate(values) if i != index]
            reduced = percentile(remaining, ".5")
            leave_one_out.append(
                {
                    "laboratory": laboratory,
                    "median_without": decimal_text(reduced),
                    "shift": decimal_text(reduced - median),
                }
            )
        leave_one_out.sort(key=lambda item: item["laboratory"])
        return {
            "participants": len(values),
            "median": decimal_text(median),
            "mad": decimal_text(mad),
            "robust_standard_deviation": decimal_text(robust_sd),
            "standard_uncertainty": decimal_text(uncertainty),
            "leave_one_out": leave_one_out,
        }


def reference_uncertainty(specification):
    """Standard uncertainty of a declared reference, in the reference unit."""
    uncertainty = specification["uncertainty"]
    value = number(uncertainty["value"], "uncertainty", nonnegative=True)
    if uncertainty["kind"] == "expanded":
        with localcontext() as ctx:
            ctx.prec = 40
            value = value / number(
                uncertainty["coverage_factor"], "coverage_factor", positive=True
            )
    return value


def classify(z_score, evaluation):
    """Map an absolute z score to the fixed round boundaries."""
    magnitude = abs(number(z_score, "z"))
    satisfactory = number(evaluation["satisfactory"], "satisfactory", positive=True)
    warning = number(evaluation["warning"], "warning", positive=True)
    if magnitude <= satisfactory:
        return "satisfactory"
    if magnitude < warning:
        return "warning"
    return "unsatisfactory"


def z_scores(difference, sigma):
    with localcontext() as ctx:
        ctx.prec = 40
        sigma = number(sigma, "sigma", positive=True)
        return number(difference, "difference") / sigma


def zeta(difference, laboratory_uncertainty, reference_uncertainty_value, covariance=None):
    """Zeta score combining both standard uncertainties.

    Returns (value, notes).  A zero or nonpositive combined variance is not
    forced into a number: the caller receives None plus the reason.
    """
    with localcontext() as ctx:
        ctx.prec = 40
        u_lab = number(laboratory_uncertainty, "uncertainty", nonnegative=True)
        u_ref = number(
            reference_uncertainty_value, "reference_uncertainty", nonnegative=True
        )
        variance = u_lab**2 + u_ref**2
        if covariance is not None:
            variance -= 2 * number(covariance, "covariance")
        if variance == 0:
            return None, "zero_scale"
        if variance < 0:
            return None, "nonpositive_variance"
        return number(difference, "difference") / variance.sqrt(), None
