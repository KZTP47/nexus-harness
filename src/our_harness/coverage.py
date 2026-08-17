"""Which pages of your site nobody checks.

A list of checks tells you what is being watched. It does not tell you what is
not, and that is the thing that bites. A site grows a settings page, a checkout
page, a help page, and no one writes a check for them, because nobody notices
they are missing.

So this walks the site the way a visitor would, follows the links, and then
lines the pages up against the checks in your suite. Every page comes back in
one of three groups: checked, only walked over, or nobody looks at it at all.
For the last group it also writes out the check you would have written, so
adding one is a single click.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import qa as qalab
from . import starters
from .config import LoadedConfig
from .models import HarnessError

# Kinds of check that open an address. A command check that happens to hold a
# web address in its arguments is not looking at that page, so it is not
# counted.
ADDRESS_KINDS = ("browser", "http", "visual")

# How many pages a walk opens unless you say otherwise.
DEFAULT_MAX_PAGES = 40


class CoverageError(HarnessError):
    """A problem working out what is checked and what is not."""


def tidy(address: str) -> str:
    """The same page written the same way twice.

    Addresses that mean one page are written half a dozen ways: with and
    without a trailing slash, with the host in capitals, with a piece after a
    hash. They are made the same here so that a page is not called unchecked
    just because a check spelled it differently.
    """

    text = str(address or "").strip()
    if not text:
        return ""
    split = urllib.parse.urlsplit(text)
    scheme = (split.scheme or "http").lower()
    host = (split.netloc or "").lower()
    # A default port means the same place as no port at all.
    for default, port in (("http", ":80"), ("https", ":443")):
        if scheme == default and host.endswith(port):
            host = host[: -len(port)]
    path = split.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    # The part after a hash is a place on the page, not another page.
    return urllib.parse.urlunsplit((scheme, host, path, split.query, ""))


def _under(address: str, boundary: str) -> bool:
    """Is this page inside the part of the site a walk stays in?"""

    if not boundary:
        return False
    tidied = tidy(address)
    edge = tidy(boundary)
    if not tidied or not edge:
        return False
    if tidied == edge:
        return True
    return tidied.startswith(edge if edge.endswith("/") else edge + "/")


@dataclass
class PageCoverage:
    """One page of the site, and who looks at it."""

    address: str
    status: int = 0
    checked_by: tuple[str, ...] = ()
    walked_by: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if self.checked_by:
            return "checked"
        if self.walked_by:
            return "only walked over"
        return "nobody looks at it"

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "status": self.status,
            "state": self.state,
            "checked_by": list(self.checked_by),
            "walked_by": list(self.walked_by),
        }


@dataclass
class Coverage:
    """What the walk found, and how much of it is watched."""

    start: str
    pages: list[PageCoverage] = field(default_factory=list)
    more_pages: int = 0
    note: str = ""

    @property
    def checked(self) -> list[PageCoverage]:
        return [page for page in self.pages if page.checked_by]

    @property
    def walked_only(self) -> list[PageCoverage]:
        return [page for page in self.pages if not page.checked_by and page.walked_by]

    @property
    def missing(self) -> list[PageCoverage]:
        return [page for page in self.pages if not page.checked_by and not page.walked_by]

    @property
    def percent(self) -> int:
        """How much of the site has a check of its own, nearest whole number."""

        if not self.pages:
            return 0
        return round(len(self.checked) * 100 / len(self.pages))

    def suggestions(self, limit: int = 20) -> list[dict[str, Any]]:
        """A ready-made check for each page nobody looks at."""

        made: list[dict[str, Any]] = []
        for page in self.missing[: max(0, limit)]:
            try:
                case = starters.build("page-opens", url=page.address, case_id=name_for(page.address))
            except HarnessError:
                continue
            made.append({"address": page.address, "starter": "page-opens", "case": case})
        return made

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "percent": self.percent,
            "more_pages": self.more_pages,
            "note": self.note,
            "pages": [page.to_dict() for page in self.pages],
            "checked": [page.address for page in self.checked],
            "walked_only": [page.address for page in self.walked_only],
            "missing": [page.address for page in self.missing],
            "suggestions": self.suggestions(),
        }

    def lines(self, offer_help: bool = True) -> list[str]:
        """Plain lines for printing, the gap first.

        With offer_help off, the line telling you how to add a check is left
        out, because by then the checks have already been written for you.
        """

        out = [
            f"Walked {len(self.pages)} page{'' if len(self.pages) == 1 else 's'} from {self.start}."
        ]
        if self.note:
            out.append(self.note)
        if not self.pages:
            return out
        out.append(
            f"{len(self.checked)} of {len(self.pages)} have a check of their own ({self.percent}%)."
        )
        if self.missing:
            out.append("Nobody looks at these:")
            out.extend(f"  {page.address}" for page in self.missing)
            if offer_help:
                out.append(
                    "Write a check for all of them with: "
                    "harness qa coverage --url <address> --write-missing"
                )
        if self.walked_only:
            out.append("Only walked over, so only the obvious breakages would show:")
            out.extend(
                f"  {page.address} (walked by {', '.join(page.walked_by)})"
                for page in self.walked_only
            )
        if self.more_pages:
            out.append(
                f"{self.more_pages} more page{'' if self.more_pages == 1 else 's'} were still "
                "waiting when the walk stopped. Raise --max-pages to reach them."
            )
        return out


def name_for(address: str) -> str:
    """A short check name made from an address, safe to write in a suite."""

    path = urllib.parse.urlsplit(tidy(address)).path or "/"
    pieces = [piece for piece in path.split("/") if piece]
    stem = "-".join(pieces[-2:]) if pieces else "home-page"
    # A check name may hold a to z and 0 to 9 and nothing else. "é" and "中"
    # are letters as far as Python is concerned, so asking whether a character
    # is a letter is not the same question as asking whether a suite will take
    # it, and a page called /café had its check refused for a name the person
    # never chose and could not change.
    kept = [
        letter.lower() if letter.isascii() and letter.isalnum() else "-" for letter in stem
    ]
    cleaned = "".join(kept).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned[:40].strip("-")
    return f"{cleaned or 'home'}-opens"


def free_name(wanted: str, taken: Iterable[str]) -> str:
    """The name asked for, or the same with a number, so nothing is dropped."""

    used = {str(item) for item in taken}
    if wanted not in used:
        return wanted
    for number in range(2, 1000):
        tried = f"{wanted}-{number}"
        if tried not in used:
            return tried
    raise CoverageError(f"There are already too many checks called {wanted}")


def watched(suite: qalab.QaSuite) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Two things: pages a check opens by name, and the walks over the site."""

    by_address: dict[str, list[str]] = {}
    walks: list[tuple[str, str]] = []
    for case in suite.cases:
        if case.kind == "crawl":
            boundary = case.stay_under or (qalab.folder_of(case.url) if case.url else "")
            if boundary:
                walks.append((boundary, case.id))
            continue
        if case.kind not in ADDRESS_KINDS or not case.url:
            continue
        by_address.setdefault(tidy(case.url), []).append(case.id)
        # A browser check that visits other addresses on the way covers them too.
        for route in case.routes:
            joined = urllib.parse.urljoin(case.url, str(route))
            by_address.setdefault(tidy(joined), []).append(case.id)
    return by_address, walks


def measure(report: Mapping[str, Any], suite: qalab.QaSuite, start: str = "") -> Coverage:
    """Line the pages a walk found up against the checks in a suite."""

    if not isinstance(report, Mapping):
        raise CoverageError("That is not a report of a walk over a site")
    pages = report.get("pages")
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
        raise CoverageError("That report holds no list of pages")
    by_address, walks = watched(suite)
    found = Coverage(start=tidy(start) or str(report.get("start") or ""))
    seen: set[str] = set()
    for item in pages:
        if not isinstance(item, Mapping):
            continue
        address = tidy(str(item.get("url") or ""))
        if not address or address in seen:
            continue
        seen.add(address)
        found.pages.append(
            PageCoverage(
                address=address,
                status=int(item.get("status") or 0),
                checked_by=tuple(by_address.get(address, ())),
                walked_by=tuple(
                    case_id for boundary, case_id in walks if _under(address, boundary)
                ),
            )
        )
    more = report.get("morePages")
    # True is a whole number as far as Python is concerned, and it is not a count.
    if isinstance(more, bool) or not isinstance(more, int) or more < 0:
        more = 0
    found.more_pages = int(more)
    fatal = str(report.get("fatal") or "")
    if fatal:
        found.note = f"The walk stopped early: {fatal}"
    return found


def walk_site(
    config: LoadedConfig,
    url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    stay_under: str = "",
    runner: qalab.QaRunner | None = None,
    extra_kinds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Open the site and follow its links, and give back what was found."""

    if not str(url or "").strip():
        raise CoverageError("Say which address to start from, for example --url http://127.0.0.1:8000/")
    limit = int(max_pages)
    if limit < 1 or limit > 500:
        raise CoverageError("The number of pages to walk has to be between 1 and 500")
    case = qalab.QaCase(
        index=0,
        id="coverage-walk",
        title="A walk to see which pages are checked",
        kind="crawl",
        expect=qalab.QaExpectation(),
        url=str(url).strip(),
        max_pages=limit,
        stay_under=str(stay_under or "").strip(),
    )
    engine = runner or qalab.QaRunner(config, extra_kinds=extra_kinds)
    _reasons, _summary, full = engine.walk_over(case)
    try:
        report = json.loads(full)
    except ValueError as exc:  # pragma: no cover - the browser writes this file
        raise CoverageError(f"Could not read what the walk found: {exc}") from exc
    if not isinstance(report, Mapping):
        raise CoverageError("Could not read what the walk found")
    return dict(report)


def read_suite(
    config: LoadedConfig,
    suite_path: str | None = None,
    extra_kinds: Mapping[str, Any] | None = None,
) -> qalab.QaSuite:
    """The checks in this project, or none at all if there are none yet.

    A project with no checks written is exactly the project this tool is most
    useful in, so a missing suite file means every page is unchecked, not an
    error message telling someone to go and write checks first.

    A file that is there and cannot be read is the opposite case, and the two
    must never be confused. Treating a broken suite as an empty one, and then
    writing, replaces somebody's checks with nothing. So only a file that is
    genuinely absent means empty; anything else is handed back to the caller
    to report.
    """

    if not suite_file(config, suite_path).exists():
        return qalab.parse_suite(
            {"schema_version": 1, "name": "default", "cases": []}, extra_kinds=extra_kinds
        )
    return qalab.load_suite(config, suite_path, extra_kinds)


def suite_file(config: LoadedConfig, suite_path: str | None = None) -> Path:
    """Where this project's checks live, whether or not anything is there yet."""

    return qalab.suite_path(config, suite_path)


def look(
    config: LoadedConfig,
    url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    stay_under: str = "",
    suite: qalab.QaSuite | None = None,
    suite_path: str | None = None,
    extra_kinds: Mapping[str, Any] | None = None,
) -> Coverage:
    """Walk the site and say which pages have a check and which do not."""

    loaded = suite or read_suite(config, suite_path, extra_kinds)
    report = walk_site(
        config, url, max_pages=max_pages, stay_under=stay_under, extra_kinds=extra_kinds
    )
    return measure(report, loaded, start=url)


def add_missing(
    config: LoadedConfig,
    addresses: Iterable[str],
    *,
    extra_kinds: Mapping[str, Any] | None = None,
    suite_path: str | None = None,
) -> list[str]:
    """Write a plain "the page opens" check for each address given."""

    wanted = [str(item).strip() for item in addresses if str(item).strip()]
    if not wanted:
        raise CoverageError("Say which page to write a check for")
    suite = read_suite(config, suite_path, extra_kinds)
    cases = [item.to_dict() for item in suite.cases]
    name = suite.name
    taken = {str(item.get("id")) for item in cases}
    # Which pages already have a check, so asking twice does not write twice.
    # This has to be the same reading of "already checked" that the walk uses,
    # or a page reported as checked would still get a second check written for
    # it. That means counting the extra addresses a check visits on the way,
    # not only the one address it starts at.
    already = set(watched(suite)[0])
    added: list[str] = []
    for address in wanted:
        if tidy(address) in already:
            continue
        # Two pages can want the same short name: /a/b and /x/a/b both read as
        # "a-b". The second one gets a number rather than being dropped, because
        # a page quietly left out is the very thing this tool exists to find.
        case_id = free_name(name_for(address), taken)
        cases.append(starters.build("page-opens", url=address, case_id=case_id))
        taken.add(case_id)
        already.add(tidy(address))
        added.append(case_id)
    if not added:
        raise CoverageError("Every one of those pages already has a check")
    written = qalab.parse_suite(
        {"schema_version": 1, "name": name, "cases": cases}, extra_kinds=extra_kinds
    )
    qalab.write_suite(config, written, suite_path)
    return added
