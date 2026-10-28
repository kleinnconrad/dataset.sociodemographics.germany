"""
Labels news articles and press releases on sociodemographic topics with Google Gemini.

One request covers a batch of up to BATCH_SIZE items (title, teaser and lead
text). The module paces the requests, tells per-day from per-minute quota
errors, splits a batch in halves when the response is blocked or invalid,
and cleans the validated labels. The returned labels are aligned with the
input items; an item without usable labels gets None.

A ``GeminiAnnotator`` may be called from several threads at once (see
GEMINI_CONCURRENCY); the call budget, the pacing and the usage counters are
shared between them.
"""
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, get_args

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from schema import (
    ANNOTATION_MODELS,
    AnnotationBatch,
    EventType,
    Institution,
    ItemAnnotation,
    PopulationGroup,
    SocialIndicator,
    SocialTopic,
    StateCode,
    describe_models,
)
from settings import Settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
TEMPERATURE = 0.2
# 0 turns thinking off: the labels are a classification of short texts, and thinking tokens are billed as output
THINKING_BUDGET: Optional[int] = 0
# A batch of ten items takes 10 to 20 seconds; the timeout bounds the time a run needs after its deadline
REQUEST_TIMEOUT_MS = 60_000
SDK_RETRY_STATUS_CODES = [408, 500, 502, 503, 504]
SDK_RETRY_ATTEMPTS = 3
# Minimum time between the starts of two requests, also with several requests at once
MIN_CALL_INTERVAL_S = 1.0
PER_MINUTE_QUOTA_RETRIES = 3
DEFAULT_QUOTA_WAIT_S = 60.0
MAX_QUOTA_WAIT_S = 120.0
MAX_TITLE_CHARS = 300
MAX_TEASER_CHARS = 600
MAX_LEAD_CHARS = 1500
MAX_ISSUE_CHARS = 60
MAX_GROUPS = 5
MAX_COUNTRIES = 10
UPCOMING_EVENT_MAX_DAYS = 730
# Codes that the model may use instead of ISO 3166-1 alpha-2
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR"}

SYSTEM_INSTRUCTION = """\
You label German news articles and press releases about the population of Germany and its living conditions for a research dataset.

Rules:
1. Label only what the given text states or clearly implies. Do not add knowledge about events, figures or groups that the text does not mention.
2. Population groups are groups of people, never single persons. List a group only if the item is mainly about it, at most five groups.
3. Do not list persons. Names of politicians, experts, victims, suspects or other persons are not labelled.
4. Tone describes how the text presents a group or the subject (positive, neutral, negative or mixed), not whether you consider the event good or bad.
5. Indicators and their direction: use the development the text reports for the current period (e.g. 'Rising' for 'Die Zahl der Asylanträge stieg um 20 Prozent'). Use 'Not stated' if the text names the indicator without a development.
6. Crime counts as sociodemographic when the item is about crime as a social phenomenon (statistics, trends, policies). Reports on a single crime, accident or court case about individuals are not sociodemographic.
7. For press releases, the issuing actor is the author.
8. Fill communication_type only for press releases; for news articles use 'Not applicable'.
9. Return exactly one entry per item, with the item number of the request.
"""

ITEM_TEMPLATE = """\
Item {number}
Kind: {kind}
Source: {source}
Published: {published}
Title: {title}
Teaser: {teaser}
Lead text: {lead}
"""

PROMPT_TEMPLATE = """\
Label the following {count} items. Most are German texts, some English; answer in English except for issue_detail, which may stay in the original language.

{items}
Fill the response schema with one entry per item."""


class AnnotationError(Exception):
    """Base class for annotation failures of a batch."""


class TransientAnnotationError(AnnotationError):
    """A server error or timeout persisted after the SDK retries."""


class RunStopError(Exception):
    """Base class for conditions that end the annotation for the whole run."""


class QuotaExhaustedError(RunStopError):
    """The per-day quota is exhausted, or the per-minute quota did not recover after several waits."""


class CallBudgetExceededError(RunStopError):
    """The run reached MAX_GEMINI_CALLS_PER_RUN."""


class FatalRequestError(RunStopError):
    """The API rejected a request with a client error other than 429 (key, model or schema problem)."""


class _UnusableResponse(Exception):
    """Internal signal that a response cannot be used; triggers the split retry."""


@dataclass(frozen=True)
class AnnotationInput:
    """One item to label.

    Attributes:
        kind (str): "news article" or "press release".
        source (str): Outlet or issuing actor with its type.
        published (Optional[str]): Publication date (YYYY-MM-DD).
        title (str): Title.
        teaser (str): Teaser or summary; may be empty.
        lead_text (str): First paragraphs of the text; may be empty.
    """
    kind: str
    source: str
    published: Optional[str]
    title: str
    teaser: str = ""
    lead_text: str = ""


@dataclass
class UsageTotals:
    """Number of calls and tokens used in a run.

    Attributes:
        calls (int): Requests sent, including retries.
        prompt_tokens (int): Input tokens.
        output_tokens (int): Tokens of the visible response.
        thinking_tokens (int): Tokens the model used for thinking.
        total_tokens (int): All tokens as reported by the API.
    """
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage_metadata: Any) -> None:
        """Adds the token counts of one response.

        Args:
            usage_metadata: The ``usage_metadata`` of a response; may be None.
        """
        if usage_metadata is None:
            return
        self.prompt_tokens += getattr(usage_metadata, "prompt_token_count", None) or 0
        self.output_tokens += getattr(usage_metadata, "candidates_token_count", None) or 0
        self.thinking_tokens += getattr(usage_metadata, "thoughts_token_count", None) or 0
        self.total_tokens += getattr(usage_metadata, "total_token_count", None) or 0


def create_client(api_key: Optional[str] = None, timeout_ms: int = REQUEST_TIMEOUT_MS) -> genai.Client:
    """Creates the Gemini client with timeout and retry options.

    Args:
        api_key: API key; defaults to the GEMINI_API_KEY environment variable.
        timeout_ms: Timeout per request attempt in milliseconds.

    Returns:
        genai.Client: The configured client.

    Raises:
        ValueError: If no API key is available.
    """
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY is not set")
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=SDK_RETRY_ATTEMPTS, http_status_codes=SDK_RETRY_STATUS_CODES),
        ),
    )


def extraction_hash(settings: Settings) -> str:
    """Returns a hash of everything that influences the labels.

    Args:
        settings: The run settings.

    Returns:
        str: 16 hexadecimal digits.
    """
    parts = {
        "system_instruction": SYSTEM_INSTRUCTION,
        "item_template": ITEM_TEMPLATE,
        "prompt_template": PROMPT_TEMPLATE,
        "schema": describe_models(ANNOTATION_MODELS, include_descriptions=True),
        "model": settings.gemini_model,
        "temperature": TEMPERATURE,
        "thinking_budget": THINKING_BUDGET,
        "batch_size": BATCH_SIZE,
        "max_chars": [MAX_TITLE_CHARS, MAX_TEASER_CHARS, MAX_LEAD_CHARS],
        "google_genai": importlib.metadata.version("google-genai"),
    }
    payload = json.dumps(parts, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _shorten(text: Optional[str], limit: int) -> str:
    """Collapses whitespace and cuts a text to a maximum length."""
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3].rstrip() + "..."


def build_prompt(items: Sequence[AnnotationInput]) -> str:
    """Builds the prompt for a batch of items.

    Args:
        items: The items, numbered from 1 in this order.

    Returns:
        str: The prompt.
    """
    blocks = [
        ITEM_TEMPLATE.format(
            number=number, kind=item.kind, source=item.source, published=item.published or "(unknown)",
            title=_shorten(item.title, MAX_TITLE_CHARS), teaser=_shorten(item.teaser, MAX_TEASER_CHARS) or "(none)",
            lead=_shorten(item.lead_text, MAX_LEAD_CHARS) or "(none)",
        )
        for number, item in enumerate(items, start=1)
    ]
    return PROMPT_TEMPLATE.format(count=len(items), items="\n".join(blocks))


def _parse_duration_s(value: Any) -> Optional[float]:
    """Parses a protobuf duration such as ``"43s"``; returns None if it cannot be parsed."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", value)
    return float(match.group(1)) if match else None


def classify_quota_error(error: errors.APIError) -> tuple[bool, Optional[float]]:
    """Reads the details of a 429 error.

    Args:
        error: The API error.

    Returns:
        tuple[bool, Optional[float]]: Whether a per-day quota is exhausted, and
        the suggested wait in seconds, if the response contains one.
    """
    body = error.details if isinstance(error.details, dict) else {}
    error_body = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
    per_day = False
    retry_after = None
    for item in error_body.get("details") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                if isinstance(violation, dict) and "PerDay" in str(violation.get("quotaId", "")):
                    per_day = True
        elif kind.endswith("RetryInfo"):
            retry_after = _parse_duration_s(item.get("retryDelay"))
    return per_day, retry_after


def parse_response(response: Any) -> AnnotationBatch:
    """Validates a response and returns the labels.

    Args:
        response: The ``GenerateContentResponse``.

    Returns:
        AnnotationBatch: The validated model output.

    Raises:
        _UnusableResponse: If the prompt was blocked, generation did not finish
            normally, the text is empty, or the JSON does not match the schema.
    """
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise _UnusableResponse(f"prompt blocked ({feedback.block_reason})")
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise _UnusableResponse("no candidates")
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is not None and finish_reason != types.FinishReason.STOP:
        raise _UnusableResponse(f"finish reason {finish_reason}")
    text = getattr(response, "text", None)
    if not text:
        raise _UnusableResponse("empty response text")
    try:
        return AnnotationBatch.model_validate_json(text)
    except ValidationError as e:
        raise _UnusableResponse(f"response failed validation ({e.error_count()} errors)") from e


def _clean_optional(value: Optional[str], limit: int) -> Optional[str]:
    """Collapses whitespace, shortens and turns empty text into None."""
    cleaned = _shorten(value, limit)
    return cleaned or None


def clean_country_codes(codes: Sequence[str]) -> list[str]:
    """Keeps the valid ISO 3166-1 alpha-2 codes of a list, without duplicates.

    Args:
        codes: Codes as returned by the model.

    Returns:
        list[str]: Upper-case two-letter codes, at most MAX_COUNTRIES; UK becomes GB and EL becomes GR.
    """
    cleaned: list[str] = []
    for code in codes:
        value = COUNTRY_ALIASES.get(code.strip().upper(), code.strip().upper())
        if re.fullmatch(r"[A-Z]{2}", value) and value not in cleaned:
            cleaned.append(value)
    return cleaned[:MAX_COUNTRIES]


def keep_known(values: Sequence[str], vocabulary: object) -> list[str]:
    """Keeps the values of a vocabulary, in their canonical spelling and without duplicates.

    Args:
        values: Values as returned by the model.
        vocabulary: A ``Literal`` alias.

    Returns:
        list[str]: The values that equal a vocabulary value, ignoring case and repeated spaces.
    """
    canonical = {str(value).casefold(): str(value) for value in get_args(vocabulary)}
    kept: list[str] = []
    for value in values:
        match = canonical.get(" ".join(value.split()).casefold())
        if match is not None and match not in kept:
            kept.append(match)
    return kept


def clean_annotation(annotation: ItemAnnotation, kind: str, published: Optional[str]) -> ItemAnnotation:
    """Normalizes the labels of one item.

    Keeps only values of the vocabularies, removes duplicates, invalid country codes and invalid
    or implausible dates, keeps at most two secondary topics and five population groups, and
    resets the press-release field for news articles.

    Args:
        annotation: The labels returned by the model.
        kind: "news article" or "press release".
        published: Publication date of the item (YYYY-MM-DD), used to check upcoming events.

    Returns:
        ItemAnnotation: The cleaned labels.
    """
    groups = {}
    for mention in annotation.population_groups:
        for group in keep_known([mention.group], PopulationGroup):
            groups.setdefault(group, mention.model_copy(update={"group": group}))
    indicators = {}
    for mention in annotation.indicators:
        for indicator in keep_known([mention.indicator], SocialIndicator):
            indicators.setdefault(indicator, mention.model_copy(update={"indicator": indicator}))
    reference = date.fromisoformat(published) if published else None
    events = []
    for event in annotation.upcoming_events:
        event_types = keep_known([event.event_type], EventType)
        try:
            event_date = date.fromisoformat(event.date.strip())
        except ValueError:
            continue
        if not event_types or (reference is not None
                               and not 0 <= (event_date - reference).days <= UPCOMING_EVENT_MAX_DAYS):
            continue
        events.append(event.model_copy(update={"event_type": event_types[0], "date": event_date.isoformat()}))
    secondary = [topic for topic in keep_known(annotation.topics_secondary, SocialTopic) if topic != annotation.topic]
    is_press = kind == "press release"
    return annotation.model_copy(update={
        "topics_secondary": secondary[:2],
        "issue_detail": _clean_optional(annotation.issue_detail, MAX_ISSUE_CHARS),
        "population_groups": list(groups.values())[:MAX_GROUPS],
        "indicators": list(indicators.values()),
        "institutions": keep_known(annotation.institutions, Institution),
        "countries": clean_country_codes(annotation.countries),
        "states_concerned": keep_known(annotation.states_concerned, StateCode),
        "event_types": keep_known(annotation.event_types, EventType),
        "upcoming_events": events,
        "communication_type": annotation.communication_type if is_press else "Not applicable",
    })


class GeminiAnnotator:
    """Sends batches of items to Gemini and returns the cleaned labels.

    Attributes:
        usage (UsageTotals): Calls and tokens of the run so far.
        extraction_hash (str): Hash of the prompt, schema and model settings.
    """

    def __init__(
        self,
        settings: Settings,
        client: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initializes the annotator.

        Args:
            settings: The run settings.
            client: A ``genai.Client`` or a compatible test double; created from the
                settings' API key if omitted.
            sleep: Function used to wait; replaceable in tests.
            clock: Monotonic clock in seconds; replaceable in tests.
        """
        self._settings = settings
        self._client = client if client is not None else create_client(settings.gemini_api_key)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_call_at: Optional[float] = None
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=AnnotationBatch,
            temperature=TEMPERATURE,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=(
                types.ThinkingConfig(thinking_budget=THINKING_BUDGET) if THINKING_BUDGET is not None else None
            ),
        )
        self.usage = UsageTotals()
        self.extraction_hash = extraction_hash(settings)

    @property
    def model(self) -> str:
        """The Gemini model id."""
        return self._settings.gemini_model

    def annotate(self, items: Sequence[AnnotationInput]) -> list[Optional[ItemAnnotation]]:
        """Labels a batch of items.

        Args:
            items: At most BATCH_SIZE items.

        Returns:
            list[Optional[ItemAnnotation]]: Cleaned labels aligned with ``items``;
            None for items without usable labels.

        Raises:
            ValueError: If no items or too many items are passed.
            TransientAnnotationError: If a server error or timeout persisted.
            RunStopError: If the quota, the call budget or a client error ends the annotation.
        """
        if not items:
            raise ValueError("annotate() needs at least one item")
        if len(items) > BATCH_SIZE:
            raise ValueError(f"{len(items)} items exceed the batch size of {BATCH_SIZE}")
        try:
            batch = self._request(items)
        except _UnusableResponse as e:
            if len(items) < 2:
                logger.warning("Unusable response for a single item (%s).", e)
                return [None]
            middle = (len(items) + 1) // 2
            logger.warning("Unusable response for %d items (%s). Retrying in two halves.", len(items), e)
            return self.annotate(items[:middle]) + self.annotate(items[middle:])
        by_number: dict[int, ItemAnnotation] = {}
        for annotation in batch.items:
            by_number.setdefault(annotation.item_number, annotation)
        results: list[Optional[ItemAnnotation]] = []
        for number, item in enumerate(items, start=1):
            annotation = by_number.get(number)
            results.append(clean_annotation(annotation, item.kind, item.published) if annotation else None)
        missing = sum(1 for result in results if result is None)
        if missing:
            logger.warning("The response missed %d of %d items.", missing, len(items))
        return results

    def _request(self, items: Sequence[AnnotationInput]) -> AnnotationBatch:
        """Sends one request and validates the response."""
        return parse_response(self._generate(build_prompt(items)))

    def _wait_for_slot(self) -> None:
        """Waits until MIN_CALL_INTERVAL_S has passed since the start of the previous call."""
        if self._last_call_at is None:
            return
        remaining = MIN_CALL_INTERVAL_S - (self._clock() - self._last_call_at)
        if remaining > 0:
            self._sleep(remaining)

    def _generate(self, prompt: str) -> Any:
        """Calls the model with budget, pacing and quota handling.

        Args:
            prompt: The prompt.

        Returns:
            The ``GenerateContentResponse``.

        Raises:
            CallBudgetExceededError: If the call budget of the run is used up.
            QuotaExhaustedError: If the per-day quota is exhausted or the per-minute quota does not recover.
            FatalRequestError: For client errors other than 429.
            TransientAnnotationError: For server errors and timeouts.
        """
        quota_waits = 0
        while True:
            # The lock makes the budget check, the pacing and the call count one step across threads
            with self._lock:
                if self.usage.calls >= self._settings.max_gemini_calls_per_run:
                    raise CallBudgetExceededError(
                        f"MAX_GEMINI_CALLS_PER_RUN ({self._settings.max_gemini_calls_per_run}) reached"
                    )
                self._wait_for_slot()
                self.usage.calls += 1
                call_number = self.usage.calls
                self._last_call_at = self._clock()
            try:
                response = self._client.models.generate_content(
                    model=self._settings.gemini_model, contents=prompt, config=self._config
                )
            except errors.ClientError as e:
                if e.code != 429:
                    raise FatalRequestError(f"Gemini rejected the request: {e}") from e
                per_day, retry_after = classify_quota_error(e)
                if per_day:
                    raise QuotaExhaustedError(f"Per-day quota exhausted: {e.message}") from e
                if quota_waits >= PER_MINUTE_QUOTA_RETRIES:
                    raise QuotaExhaustedError(f"Quota still exhausted after {quota_waits} waits: {e.message}") from e
                quota_waits += 1
                wait = min(retry_after or DEFAULT_QUOTA_WAIT_S, MAX_QUOTA_WAIT_S)
                logger.warning("Per-minute quota reached. Waiting %.0f s before retry %d.", wait, quota_waits)
                self._sleep(wait)
                continue
            except errors.ServerError as e:
                raise TransientAnnotationError(f"Gemini server error after retries: {e}") from e
            except httpx.TransportError as e:
                raise TransientAnnotationError(f"Network error after retries: {e!r}") from e
            usage = getattr(response, "usage_metadata", None)
            with self._lock:
                self.usage.add(usage)
            logger.info(
                "Gemini call %d: prompt %s tokens, output %s, thinking %s.",
                call_number,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "thoughts_token_count", None),
            )
            return response
