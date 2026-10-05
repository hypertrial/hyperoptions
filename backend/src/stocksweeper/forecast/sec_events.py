"""Conservative point-in-time SEC earnings schedule evidence.

An 8-K announcing a *future* date is useful. An earnings release or a filing
accepted on the event date is not proof that its date was known beforehand.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time as clock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from options_api.models import normalize_ticker
from stocksweeper.storage.db import connect, rows

_NY = ZoneInfo("America/New_York")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DATE = (
    r"(?P<date>(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|"
    r"Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2},?\s+20\d{2})"
)
_SCHEDULE = re.compile(
    r"(?:will|plans to|expects to|to)\s+(?:report|release|announce)\b.{0,150}?"
    r"(?:earnings|financial results|quarterly results)\b.{0,180}?\bon\s+" + _DATE,
    re.IGNORECASE,
)
_RESULTS_OBJECT = (
    r"(?:earnings|financial results|quarterly results)\b"
    r"(?!\s+(?:release\s+)?(?:date|schedule|conference call|webcast)\b)"
)
_ACTUAL_OBJECT = (
    r"\b(?:reported|announced|released)\b"
    r"(?:(?!\b(?:date|schedule|conference call|webcast|upcoming|forthcoming)\b).){0,150}?"
    r"\b" + _RESULTS_OBJECT
)
_ACTUAL_PREFIX = re.compile(
    r"\bOn\s+" + _DATE + r"\b.{0,120}?" + _ACTUAL_OBJECT,
    re.IGNORECASE,
)
_ACTUAL_SUFFIX = re.compile(
    _ACTUAL_OBJECT + r".{0,100}?\bon\s+" + _DATE,
    re.IGNORECASE,
)
# Keep supported date and corporate abbreviations within their sentence.
_SENTENCES = re.compile(
    r"(?:\b(?:Inc|Corp|Co|Ltd|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\."
    r"|[^.!?])+", re.IGNORECASE,
)
# Only a separate call-hosting clause may ignore its future auxiliary.
# Keep its remaining text so "to release results" still fails closed.
_FUTURE_CALL = re.compile(
    r"(?:;|\band|\bbut)\s+(?:it\s+)?"
    r"(?:will|would|shall|plans? to|expects? to|intends? to)\s+"
    r"(?:host|hold|conduct)\s+(?:(?:a|an|the|its)\s+)?"
    r"(?:earnings\s+)?(?:conference call|webcast)\b", re.IGNORECASE,
)
_FUTURE_RELEASE = re.compile(
    r"\b(?:will|would|shall|plans? to|expects? to|expected to|intends? to|"
    r"scheduled (?:to|for)|planned (?:to|for)|going to)\b"
    r"|\bto\s+(?:be\s+)?(?:report(?:ed)?|release(?:d)?|announce(?:d)?)\b", re.IGNORECASE,
)
_TIME = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*(?:ET|EST|EDT|Eastern Time)\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(20\d{2})\b", re.IGNORECASE
)
_MAX_BYTES = 1_000_000
_MAX_FILINGS = 5
_rate_lock = threading.Lock()
_last_request = 0.0


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


@dataclass(frozen=True)
class SecSchedule:
    ticker: str
    cik: int
    accession: str
    accepted_at: datetime
    retrieved_at: datetime
    event_date: date
    event_at: datetime | None
    series_key: str | None
    document_url: str
    document_hash: str
    evidence: str

    @property
    def knowledge_at(self) -> datetime:
        return max(self.accepted_at, self.retrieved_at)


@dataclass(frozen=True)
class SecActual:
    ticker: str
    cik: int
    accession: str
    accepted_at: datetime
    retrieved_at: datetime
    event_date: date
    document_url: str
    document_hash: str
    evidence: str


def _filing_url(cik: int, accession: str, document: str) -> str:
    if (
        not 0 < cik <= 9_999_999_999
        or not _ACCESSION.fullmatch(accession)
        or not _FILENAME.fullmatch(document)
    ):
        raise ValueError("invalid SEC filing identity")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"


def _fetch(client: httpx.Client, url: str, user_agent: str) -> bytes:
    global _last_request
    if not (
        url.startswith("https://www.sec.gov/Archives/edgar/data/")
        or url.startswith("https://data.sec.gov/submissions/CIK")
    ):
        raise ValueError("SEC fetch host or path is not allowed")
    if "@" not in user_agent or len(user_agent) > 200:
        raise ValueError("SEC requests need a declared contact User-Agent")
    with _rate_lock:
        delay = 0.25 - (clock.monotonic() - _last_request)
        if delay > 0:
            clock.sleep(delay)
        _last_request = clock.monotonic()
    with client.stream(
        "GET",
        url,
        headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
        follow_redirects=False,
    ) as response:
        if response.status_code != 200 or response.is_redirect:
            raise ValueError(f"SEC document unavailable (HTTP {response.status_code})")
        if int(response.headers.get("Content-Length", "0")) > _MAX_BYTES:
            raise ValueError("SEC response exceeds byte limit")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_BYTES:
                raise ValueError("SEC response exceeds byte limit")
        return bytes(content)


def _document_text(content: bytes) -> str:
    parser = _Text()
    parser.feed(content.decode("utf-8", errors="replace"))
    return " ".join(" ".join(parser.parts).split())


def _event_date(text: str) -> date | None:
    normalized = text.replace(".", "").replace(",", "")
    normalized = re.sub(r"^sept(?=\s)", "Sep", normalized, flags=re.IGNORECASE)
    for layout in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(normalized, layout).date()
        except ValueError:
            pass
    return None


def parse_forward_schedule(
    ticker: str,
    cik: int,
    accession: str,
    accepted_at: datetime,
    retrieved_at: datetime,
    document: str,
    content: bytes,
) -> SecSchedule | None:
    """Accept an explicit future date in a dated 8-K document, never infer one."""
    url = _filing_url(cik, accession, document)
    if (
        normalize_ticker(ticker) != ticker
        or accepted_at.tzinfo is None
        or retrieved_at.tzinfo is None
    ):
        raise ValueError("SEC schedule identity or time is invalid")
    if len(content) > _MAX_BYTES:
        raise ValueError("SEC document exceeds byte limit")
    text = _document_text(content)
    matches = list(_SCHEDULE.finditer(text))
    if len(matches) != 1:
        return None  # Ambiguous or absent announcement.
    match = matches[0]
    event_date = _event_date(match.group("date"))
    if event_date is None:
        return None
    if event_date <= accepted_at.astimezone(_NY).date() or retrieved_at < accepted_at:
        return None
    context = text[max(0, match.start() - 100) : min(len(text), match.end() + 100)]
    # A nearby filing timestamp must not become the announced event time.
    after_date = text[match.end() : min(len(text), match.end() + 100)]
    local_time = _TIME.search(after_date)
    if local_time and "." in after_date[: local_time.start()]:
        local_time = None
    event_at = None
    if local_time:
        hour = int(local_time.group(1)) % 12 + (
            12 if local_time.group(3).lower().startswith("p") else 0
        )
        minute = int(local_time.group(2) or 0)
        if hour < 24 and minute < 60:
            event_at = datetime(
                event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=_NY
            ).astimezone(UTC)
    period = _PERIOD.search(context)
    quarter = {
        "first": 1,
        "1st": 1,
        "second": 2,
        "2nd": 2,
        "third": 3,
        "3rd": 3,
        "fourth": 4,
        "4th": 4,
    }
    series_key = f"{period.group(2)}Q{quarter[period.group(1).lower()]}" if period else None
    return SecSchedule(
        ticker,
        cik,
        accession,
        accepted_at.astimezone(UTC),
        retrieved_at.astimezone(UTC),
        event_date,
        event_at,
        series_key,
        url,
        hashlib.sha256(content).hexdigest(),
        text[match.start() : match.end()][:300],
    )


def parse_actual_results(
    ticker: str,
    cik: int,
    accession: str,
    accepted_at: datetime,
    retrieved_at: datetime,
    document: str,
    content: bytes,
) -> SecActual | None:
    """Require an explicitly dated results announcement near filing acceptance."""
    url = _filing_url(cik, accession, document)
    if (
        normalize_ticker(ticker) != ticker
        or accepted_at.tzinfo is None
        or retrieved_at.tzinfo is None
    ):
        raise ValueError("SEC actual-results identity or time is invalid")
    if len(content) > _MAX_BYTES or retrieved_at < accepted_at:
        raise ValueError("SEC actual-results content or retrieval time is invalid")
    text = _document_text(content)
    matches = []
    for sentence in _SENTENCES.finditer(text):
        evidence = sentence.group()
        if _FUTURE_RELEASE.search(_FUTURE_CALL.sub("", evidence)):
            continue
        future_call = _FUTURE_CALL.search(evidence)
        if future_call is not None:
            # The call's purpose cannot supply a results object for a past verb.
            evidence = evidence[:future_call.start()]
        for pattern in (_ACTUAL_PREFIX, _ACTUAL_SUFFIX):
            matches.extend(pattern.finditer(evidence))
    if len(matches) != 1:
        return None
    match = matches[0]
    event_date = _event_date(match.group("date"))
    accepted_day = accepted_at.astimezone(_NY).date()
    if event_date is None or not 0 <= (accepted_day - event_date).days <= 3:
        return None
    return SecActual(
        ticker,
        cik,
        accession,
        accepted_at.astimezone(UTC),
        retrieved_at.astimezone(UTC),
        event_date,
        url,
        hashlib.sha256(content).hexdigest(),
        match.group()[:300],
    )


def record_actual(data_dir: Path, actual: SecActual) -> bool:
    identity = (
        actual.ticker,
        actual.cik,
        actual.accession,
        actual.document_hash,
        actual.event_date.isoformat(),
    )
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    with connect(data_dir / "results.duckdb") as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sec_earnings_actuals (
                event_key VARCHAR PRIMARY KEY, ticker VARCHAR NOT NULL, cik BIGINT NOT NULL,
                accession VARCHAR NOT NULL, accepted_at TIMESTAMPTZ NOT NULL,
                retrieved_at TIMESTAMPTZ NOT NULL, event_date DATE NOT NULL,
                document_url VARCHAR NOT NULL, document_hash VARCHAR NOT NULL,
                evidence VARCHAR NOT NULL
            )"""
        )
        before = connection.execute(
            "SELECT count(*) FROM sec_earnings_actuals WHERE event_key = ?", [key]
        ).fetchone()[0]
        connection.execute(
            """INSERT OR IGNORE INTO sec_earnings_actuals
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                key,
                actual.ticker,
                actual.cik,
                actual.accession,
                actual.accepted_at,
                actual.retrieved_at,
                actual.event_date,
                actual.document_url,
                actual.document_hash,
                actual.evidence,
            ],
        )
        return before == 0


def record_schedule(data_dir: Path, schedule: SecSchedule) -> bool:
    """Append one source revision; return whether it was newly recorded."""
    if schedule.knowledge_at.date() > schedule.event_date:
        raise ValueError("earnings schedule was not known before event")
    identity = (
        schedule.ticker,
        schedule.cik,
        schedule.accession,
        schedule.document_hash,
        schedule.event_date.isoformat(),
        schedule.event_at.isoformat() if schedule.event_at else None,
    )
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    with connect(data_dir / "results.duckdb") as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sec_earnings_schedules (
                event_key VARCHAR PRIMARY KEY, ticker VARCHAR NOT NULL, cik BIGINT NOT NULL,
                accession VARCHAR NOT NULL, accepted_at TIMESTAMPTZ NOT NULL,
                retrieved_at TIMESTAMPTZ NOT NULL, event_date DATE NOT NULL,
                event_at TIMESTAMPTZ, series_key VARCHAR, document_url VARCHAR NOT NULL,
                document_hash VARCHAR NOT NULL, evidence VARCHAR NOT NULL
            )"""
        )
        before = connection.execute(
            "SELECT count(*) FROM sec_earnings_schedules WHERE event_key = ?", [key]
        ).fetchone()[0]
        connection.execute(
            """INSERT OR IGNORE INTO sec_earnings_schedules
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                key,
                schedule.ticker,
                schedule.cik,
                schedule.accession,
                schedule.accepted_at,
                schedule.retrieved_at,
                schedule.event_date,
                schedule.event_at,
                schedule.series_key,
                schedule.document_url,
                schedule.document_hash,
                schedule.evidence,
            ],
        )
        return before == 0


def known_forward_schedules(
    data_dir: Path, ticker: str, as_of: datetime, through: date
) -> tuple[dict[str, object], ...]:
    """Return only schedules retrieved by the origin, preserving revision rows."""
    known = _schedules_as_of(data_dir, ticker, as_of)
    current_day = as_of.astimezone(_NY).date()
    return tuple(
        item
        for item in known
        if current_day < item["event_date"] <= through
    )


def _schedules_as_of(
    data_dir: Path, ticker: str, as_of: datetime
) -> tuple[dict[str, object], ...]:
    if normalize_ticker(ticker) != ticker or as_of.tzinfo is None:
        raise ValueError("invalid SEC event lookup")
    with connect(data_dir / "results.duckdb") as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sec_earnings_schedules (
                event_key VARCHAR PRIMARY KEY, ticker VARCHAR NOT NULL, cik BIGINT NOT NULL,
                accession VARCHAR NOT NULL, accepted_at TIMESTAMPTZ NOT NULL,
                retrieved_at TIMESTAMPTZ NOT NULL, event_date DATE NOT NULL,
                event_at TIMESTAMPTZ, series_key VARCHAR, document_url VARCHAR NOT NULL,
                document_hash VARCHAR NOT NULL, evidence VARCHAR NOT NULL
            )"""
        )
        found = rows(
            connection,
            """SELECT * FROM sec_earnings_schedules WHERE ticker = ?
               AND accepted_at <= ? AND retrieved_at <= ?
               ORDER BY accepted_at, retrieved_at, event_key""",
            [ticker, as_of, as_of],
        )
    return tuple(found)


def effective_forward_schedules(
    data_dir: Path, ticker: str, as_of: datetime, through: date
) -> tuple[dict[str, object], ...]:
    """Latest observed revision per explicitly identified fiscal period."""
    # Choose the latest known revision before applying the date window. An old
    # future date must not survive a later revision moving it into the past.
    known = _schedules_as_of(data_dir, ticker, as_of)
    latest: dict[str, dict[str, object]] = {}
    for item in known:
        series = item["series_key"]
        if not isinstance(series, str):
            continue
        previous = latest.get(series)
        order = (item["accepted_at"], item["retrieved_at"], item["event_key"])
        if previous is None or order > (
            previous["accepted_at"],
            previous["retrieved_at"],
            previous["event_key"],
        ):
            latest[series] = item
    current_day = as_of.astimezone(_NY).date()
    return tuple(
        latest[key]
        for key in sorted(latest)
        if current_day < latest[key]["event_date"] <= through
    )


def verified_past_earnings_events(
    data_dir: Path, ticker: str, as_of: datetime
) -> tuple[dict[str, object], ...]:
    """Match an actual results release to a schedule known before that event.

    Only explicit dates in SEC documents qualify; no event is inferred from an
    8-K filing date. Schedule revisions are kept, then the last prior revision
    for the exact realized date is selected without rewriting source records.
    """
    if normalize_ticker(ticker) != ticker or as_of.tzinfo is None:
        raise ValueError("invalid SEC event lookup")
    with connect(data_dir / "results.duckdb") as connection:
        actual_tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if not {"sec_earnings_actuals", "sec_earnings_schedules"} <= actual_tables:
            return ()
        actuals = rows(
            connection,
            """SELECT * FROM sec_earnings_actuals
               WHERE ticker = ? AND accepted_at <= ? AND retrieved_at <= ?
               ORDER BY event_date, accepted_at, retrieved_at, event_key""",
            [ticker, as_of, as_of],
        )
        schedules = rows(
            connection,
            """SELECT * FROM sec_earnings_schedules WHERE ticker = ?
               ORDER BY event_date, accepted_at, retrieved_at, event_key""",
            [ticker],
        )
    result = []
    seen_dates: set[date] = set()
    for actual in actuals:
        day = actual["event_date"]
        if day in seen_dates or day >= as_of.astimezone(_NY).date():
            continue
        prior_by_series: dict[str, dict[str, object]] = {}
        for schedule in schedules:
            series = schedule["series_key"]
            if not isinstance(series, str) or schedule["cik"] != actual["cik"]:
                continue
            if (
                schedule["accepted_at"].astimezone(_NY).date() >= day
                or schedule["retrieved_at"].astimezone(_NY).date() >= day
            ):
                continue
            previous = prior_by_series.get(series)
            order = (schedule["accepted_at"], schedule["retrieved_at"], schedule["event_key"])
            if previous is None or order > (
                previous["accepted_at"],
                previous["retrieved_at"],
                previous["event_key"],
            ):
                prior_by_series[series] = schedule
        matching = [item for item in prior_by_series.values() if item["event_date"] == day]
        if len(matching) != 1:
            continue
        latest = matching[0]
        result.append(
            {
                "ticker": ticker,
                "cik": actual["cik"],
                "event_date": day,
                "actual_accession": actual["accession"],
                "actual_accepted_at": actual["accepted_at"],
                "actual_retrieved_at": actual["retrieved_at"],
                "actual_document_hash": actual["document_hash"],
                "schedule_accession": latest["accession"],
                "schedule_accepted_at": latest["accepted_at"],
                "schedule_retrieved_at": latest["retrieved_at"],
                "schedule_document_hash": latest["document_hash"],
            }
        )
        seen_dates.add(day)
    return tuple(result)


def _one_exhibit_991(index: bytes) -> str | None:
    """SEC archive index has filenames, not reliable exhibit type metadata."""
    try:
        listing = json.loads(index)["directory"]["item"]
        matches = []
        for item in listing:
            name = item["name"]
            if not isinstance(name, str) or not _FILENAME.fullmatch(name):
                continue
            if not name.lower().endswith((".htm", ".html", ".txt")):
                continue
            compact = re.sub(r"[^a-z0-9]", "", name.lower())
            if re.search(r"(?:exhibit|ex)991(?!\d)", compact):
                matches.append(name)
        return matches[0] if len(matches) == 1 else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def capture_recent_sec_schedules(
    data_dir: Path,
    ticker: str,
    cik: int,
    user_agent: str,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    observed_time: Callable[[], datetime] | None = None,
) -> int:
    """Inspect at most five recent 8-Ks and one exhibit each, offline-safe.

    This intentionally does not infer a calendar from SEC filing history. Only
    explicit forward dates and actual result dates become separate records.
    """
    if normalize_ticker(ticker) != ticker or not 0 < cik <= 9_999_999_999:
        raise ValueError("invalid SEC issuer")
    if now is not None and now.tzinfo is None:
        raise ValueError("SEC capture time must be timezone-aware")

    def capture_time() -> datetime:
        value = observed_time() if observed_time is not None else now or datetime.now(UTC)
        if value.tzinfo is None:
            raise ValueError("SEC capture time must be timezone-aware")
        return value.astimezone(UTC)

    owned_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False
    )
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        response = json.loads(_fetch(client, url, user_agent))
        observed_at = capture_time()
        if response.get("cik") != cik or ticker not in response.get("tickers", []):
            raise ValueError("SEC CIK does not match ticker")
        recent = response["filings"]["recent"]
        selected = [
            (form, accession, document, accepted)
            for form, accession, document, accepted in zip(
                recent["form"],
                recent["accessionNumber"],
                recent["primaryDocument"],
                recent["acceptanceDateTime"],
                strict=True,
            )
            if form in ("8-K", "8-K/A")
        ][:_MAX_FILINGS]
        inserted = 0
        for _, accession, document, accepted in selected:
            accepted_at = datetime.fromisoformat(accepted)
            if accepted_at.tzinfo is None:
                accepted_at = accepted_at.replace(tzinfo=_NY)
            if accepted_at > observed_at:
                continue
            content = _fetch(client, _filing_url(cik, accession, document), user_agent)
            primary_retrieved_at = capture_time()
            schedule = parse_forward_schedule(
                ticker, cik, accession, accepted_at, primary_retrieved_at, document, content
            )
            actual = parse_actual_results(
                ticker, cik, accession, accepted_at, primary_retrieved_at, document, content
            )
            if schedule is None or actual is None:
                index = _fetch(client, _filing_url(cik, accession, "index.json"), user_agent)
                exhibit = _one_exhibit_991(index)
                if exhibit is not None:
                    exhibit_content = _fetch(
                        client, _filing_url(cik, accession, exhibit), user_agent
                    )
                    exhibit_retrieved_at = capture_time()
                    if schedule is None:
                        schedule = parse_forward_schedule(
                            ticker,
                            cik,
                            accession,
                            accepted_at,
                            exhibit_retrieved_at,
                            exhibit,
                            exhibit_content,
                        )
                    if actual is None:
                        actual = parse_actual_results(
                            ticker,
                            cik,
                            accession,
                            accepted_at,
                            exhibit_retrieved_at,
                            exhibit,
                            exhibit_content,
                        )
            # A first-time scan may discover an old announcement only after
            # its event. Keep the record_schedule invariant and continue.
            if schedule is not None and schedule.knowledge_at.date() <= schedule.event_date:
                inserted += record_schedule(data_dir, schedule)
            if actual is not None:
                record_actual(data_dir, actual)
        return inserted
    finally:
        if owned_client:
            client.close()
