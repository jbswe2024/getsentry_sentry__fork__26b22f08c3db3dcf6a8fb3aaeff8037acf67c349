import logging
from dataclasses import asdict
from typing import Any

import sentry_sdk
from django.conf import settings

from sentry import options
from sentry import ratelimits as ratelimiter
from sentry.conf.server import SEER_SIMILARITY_MODEL_VERSION
from sentry.eventstore.models import Event
from sentry.grouping.grouping_info import get_grouping_info_from_variants
from sentry.grouping.variants import BaseVariant
from sentry.models.grouphash import GroupHash
from sentry.models.project import Project
from sentry.seer.similarity.similar_issues import get_similarity_data_from_seer
from sentry.seer.similarity.types import SimilarIssuesEmbeddingsRequest
from sentry.seer.similarity.utils import (
    SEER_INELIGIBLE_EVENT_PLATFORMS,
    ReferrerOptions,
    event_content_has_stacktrace,
    filter_null_from_string,
    get_stacktrace_string_with_metrics,
    has_too_many_contributing_frames,
    killswitch_enabled,
    record_did_call_seer_metric,
)
from sentry.utils import metrics
from sentry.utils.circuit_breaker2 import CircuitBreaker
from sentry.utils.safe import get_path

logger = logging.getLogger("sentry.events.grouping")


def should_call_seer_for_grouping(event: Event, variants: dict[str, BaseVariant]) -> bool:
    """
    Use event content, feature flags, rate limits, killswitches, seer health, etc. to determine
    whether a call to Seer should be made.
    """

    project = event.project

    # Check both of these before returning based on either so we can gather metrics on their results
    content_is_eligible = _event_content_is_seer_eligible(event)
    seer_enabled_for_project = _project_has_similarity_grouping_enabled(project)
    if not (content_is_eligible and seer_enabled_for_project):
        return False

    if (
        _has_customized_fingerprint(event, variants)
        or _has_too_many_contributing_frames(event, variants)
        or killswitch_enabled(project.id, ReferrerOptions.INGEST, event)
        or _circuit_breaker_broken(event, project)
        # The rate limit check has to be last (see below) but rate-limiting aside, call this after other checks
        # because it calculates the stacktrace string, which we only want to spend the time to do if we already
        # know the other checks have passed.
        or _has_empty_stacktrace_string(event, variants)
        # **Do not add any new checks after this.** The rate limit check MUST remain the last of all
        # the checks.
        #
        # (Checking the rate limit for calling Seer also increments the counter of how many times
        # we've tried to call it, and if we fail any of the other checks, it shouldn't count as an
        # attempt. Thus we only want to run the rate limit check if every other check has already
        # succeeded.)
        or _ratelimiting_enabled(event, project)
    ):
        return False

    return True


def _event_content_is_seer_eligible(event: Event) -> bool:
    """
    Determine if an event's contents makes it fit for using with Seer's similar issues model.
    """
    if not event_content_has_stacktrace(event):
        metrics.incr(
            "grouping.similarity.event_content_seer_eligible",
            sample_rate=options.get("seer.similarity.metrics_sample_rate"),
            tags={"eligible": False, "blocker": "no-stacktrace"},
        )
        return False

    if event.platform in SEER_INELIGIBLE_EVENT_PLATFORMS:
        metrics.incr(
            "grouping.similarity.event_content_seer_eligible",
            sample_rate=options.get("seer.similarity.metrics_sample_rate"),
            tags={"eligible": False, "blocker": "unsupported-platform"},
        )
        return False

    metrics.incr(
        "grouping.similarity.event_content_seer_eligible",
        sample_rate=options.get("seer.similarity.metrics_sample_rate"),
        tags={"eligible": True, "blocker": "none"},
    )
    return True


def _has_too_many_contributing_frames(event: Event, variants: dict[str, BaseVariant]) -> bool:
    if has_too_many_contributing_frames(event, variants, ReferrerOptions.INGEST):
        record_did_call_seer_metric(call_made=False, blocker="excess-frames")
        return True

    return False


def _project_has_similarity_grouping_enabled(project: Project) -> bool:
    # TODO: This is a hack to get ingest to turn on for projects as soon as they're backfilled. When
    # the backfill script completes, we turn on this option, enabling ingest immediately rather than
    # forcing the project to wait until it's been manually added to a feature handler. Once all
    # projects have been backfilled, the option (and this check) can go away.
    has_been_backfilled = bool(project.get_option("sentry:similarity_backfill_completed"))

    metrics.incr(
        "grouping.similarity.event_project_backfill_status",
        sample_rate=options.get("seer.similarity.metrics_sample_rate"),
        tags={"backfilled": has_been_backfilled},
    )

    return has_been_backfilled


# TODO: Here we're including events with hybrid fingerprints (ones which are `{{ default }}`
# combined with some other value). To the extent to which we're then using this function to decide
# whether or not to call Seer, this means that the calculations giving rise to the default part of
# the value never involve Seer input. In the long run, we probably want to change that.
def _has_customized_fingerprint(event: Event, variants: dict[str, BaseVariant]) -> bool:
    fingerprint = event.data.get("fingerprint", [])

    if "{{ default }}" in fingerprint:
        # No custom fingerprinting at all
        if len(fingerprint) == 1:
            return False

        # Hybrid fingerprinting ({{ default }} + some other value(s))
        else:
            record_did_call_seer_metric(call_made=False, blocker="hybrid-fingerprint")
            return True

    # Fully customized fingerprint (from either us or the user)
    fingerprint_variant = variants.get("custom_fingerprint") or variants.get("built_in_fingerprint")

    if fingerprint_variant:
        record_did_call_seer_metric(call_made=False, blocker=fingerprint_variant.type)
        return True

    return False


def _ratelimiting_enabled(event: Event, project: Project) -> bool:
    """
    Check both the global and project-based Seer similarity ratelimits.
    """

    global_ratelimit = options.get("seer.similarity.global-rate-limit")
    per_project_ratelimit = options.get("seer.similarity.per-project-rate-limit")

    global_limit_per_sec = global_ratelimit["limit"] / global_ratelimit["window"]
    project_limit_per_sec = per_project_ratelimit["limit"] / per_project_ratelimit["window"]

    logger_extra = {"event_id": event.event_id, "project_id": project.id}

    if ratelimiter.backend.is_limited("seer:similarity:global-limit", **global_ratelimit):
        logger_extra["limit_per_sec"] = global_limit_per_sec
        logger.warning("should_call_seer_for_grouping.global_ratelimit_hit", extra=logger_extra)
        record_did_call_seer_metric(call_made=False, blocker="global-rate-limit")

        return True

    if ratelimiter.backend.is_limited(
        f"seer:similarity:project-{project.id}-limit", **per_project_ratelimit
    ):
        logger_extra["limit_per_sec"] = project_limit_per_sec
        logger.warning("should_call_seer_for_grouping.project_ratelimit_hit", extra=logger_extra)
        record_did_call_seer_metric(call_made=False, blocker="project-rate-limit")

        return True

    return False


def _circuit_breaker_broken(event: Event, project: Project) -> bool:
    breaker_config = options.get("seer.similarity.circuit-breaker-config")
    circuit_breaker = CircuitBreaker(settings.SEER_SIMILARITY_CIRCUIT_BREAKER_KEY, breaker_config)
    circuit_broken = not circuit_breaker.should_allow_request()

    if circuit_broken:
        logger.warning(
            "should_call_seer_for_grouping.broken_circuit_breaker",
            extra={
                "event_id": event.event_id,
                "project_id": project.id,
                **breaker_config,
            },
        )
        record_did_call_seer_metric(call_made=False, blocker="circuit-breaker")

    return circuit_broken


def _has_empty_stacktrace_string(event: Event, variants: dict[str, BaseVariant]) -> bool:
    stacktrace_string = get_stacktrace_string_with_metrics(
        get_grouping_info_from_variants(variants), event.platform, ReferrerOptions.INGEST
    )
    if not stacktrace_string:
        if stacktrace_string == "":
            record_did_call_seer_metric(call_made=False, blocker="empty-stacktrace-string")
        return True
    # Store the stacktrace string in the event so we only calculate it once. We need to pop it
    # later so it isn't stored in the database.
    event.data["stacktrace_string"] = stacktrace_string
    return False


def get_seer_similar_issues(
    event: Event,
    variants: dict[str, BaseVariant],
    num_neighbors: int = 1,
) -> tuple[dict[str, Any], GroupHash | None]:
    """
    Ask Seer for the given event's nearest neighbor(s) and return the seer response data, sorted
    with the best matches first, along with a grouphash linked to the group Seer decided the event
    should go in (if any), or None if no neighbor was near enough.
    """
    event_hash = event.get_primary_hash()
    exception_type = get_path(event.data, "exception", "values", -1, "type")

    stacktrace_string = event.data.get(
        "stacktrace_string",
        get_stacktrace_string_with_metrics(
            get_grouping_info_from_variants(variants), event.platform, ReferrerOptions.INGEST
        ),
    )

    if not stacktrace_string:
        # TODO: remove this log once we've confirmed it isn't happening
        logger.info(
            "get_seer_similar_issues.empty_stacktrace",
            extra={
                "event_id": event.event_id,
                "project_id": event.project.id,
                "stacktrace_string": stacktrace_string,
            },
        )
        similar_issues_metadata_empty = {
            "results": [],
            "similarity_model_version": SEER_SIMILARITY_MODEL_VERSION,
        }
        return (similar_issues_metadata_empty, None)

    request_data: SimilarIssuesEmbeddingsRequest = {
        "event_id": event.event_id,
        "hash": event_hash,
        "project_id": event.project.id,
        "stacktrace": stacktrace_string,
        "exception_type": filter_null_from_string(exception_type) if exception_type else None,
        "k": num_neighbors,
        "referrer": "ingest",
        "use_reranking": options.get("seer.similarity.ingest.use_reranking"),
    }
    event.data.pop("stacktrace_string", None)

    # Similar issues are returned with the closest match first
    seer_results = get_similarity_data_from_seer(request_data)
    seer_results_json = [asdict(result) for result in seer_results]
    similar_issues_metadata = {
        "results": seer_results_json,
        "similarity_model_version": SEER_SIMILARITY_MODEL_VERSION,
    }
    parent_grouphash = (
        GroupHash.objects.filter(
            hash=seer_results[0].parent_hash, project_id=event.project.id
        ).first()
        if seer_results
        else None
    )

    logger.info(
        "get_seer_similar_issues.results",
        extra={
            "event_id": event.event_id,
            "project_id": event.project.id,
            "hash": event_hash,
            "results": seer_results_json,
            "grouphash_returned": bool(parent_grouphash),
        },
    )

    return (similar_issues_metadata, parent_grouphash)


def maybe_check_seer_for_matching_grouphash(
    event: Event, variants: dict[str, BaseVariant], all_grouphashes: list[GroupHash]
) -> GroupHash | None:
    seer_matched_grouphash = None

    if should_call_seer_for_grouping(event, variants):
        record_did_call_seer_metric(call_made=True, blocker="none")

        try:
            # If no matching group is found in Seer, we'll still get back result
            # metadata, but `seer_matched_grouphash` will be None
            seer_response_data, seer_matched_grouphash = get_seer_similar_issues(event, variants)
        except Exception as e:  # Insurance - in theory we shouldn't ever land here
            sentry_sdk.capture_exception(
                e, tags={"event": event.event_id, "project": event.project.id}
            )
            return None

        # Find the GroupHash corresponding to the hash value sent to Seer
        #
        # TODO: There shouldn't actually be more than one hash in `all_grouphashes`, but
        #   a) there's a bug in our precedence logic which leads both in-app and system stacktrace
        #      hashes being marked as contributing and making it through to this point, and
        #   b) because of how we used to compute secondary and primary hashes, we keep secondary
        #      hashes even when we don't need them.
        # Once those two problems are fixed, there will only be one hash passed to this function
        # and we won't have to do this search to find the right one to update.
        primary_hash = event.get_primary_hash()

        grouphash_sent = list(
            filter(lambda grouphash: grouphash.hash == primary_hash, all_grouphashes)
        )[0]

        # Update the relevant GroupHash with Seer results
        gh_metadata = grouphash_sent.metadata
        if gh_metadata:
            gh_metadata.update(
                # Technically the time of the metadata record creation and the time of the Seer
                # request will be some milliseconds apart, but a) the difference isn't meaningful
                # for us, and b) forcing them to be the same (rather than just close) lets us use
                # their equality as a signal that the Seer call happened during ingest rather than
                # during a backfill, without having to store that information separately.
                seer_date_sent=gh_metadata.date_added,
                seer_event_sent=event.event_id,
                seer_model=seer_response_data["similarity_model_version"],
                seer_matched_grouphash=seer_matched_grouphash,
                seer_match_distance=(
                    seer_response_data["results"][0]["stacktrace_distance"]
                    if seer_matched_grouphash
                    else None
                ),
            )

    return seer_matched_grouphash
