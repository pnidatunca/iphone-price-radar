"""URL allowlists: an adapter requests only URLs of a few fixed forms and refuses everything else in code.

robots.txt is not a sufficient guard on its own. Easycep's rule ``*?orderby*`` does not match ``&orderby``, and a path
copied from page data could carry sort or filter parameters. So each source declares the exact path patterns it uses
and the only query parameters they may carry (pagination, or the seller of an offer link), and ``check`` raises
``UrlNotAllowed`` for any other URL before a request is made.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
POSITIVE_INT = re.compile(r"[1-9][0-9]{0,5}")


class UrlNotAllowed(ValueError):
    """The URL is not one of the forms this source may request."""


@dataclass(frozen=True)
class UrlForm:
    """One allowed URL form: a path pattern plus the query parameters it may carry (each a positive integer)."""

    name: str
    path: re.Pattern[str]
    params: tuple[str, ...] = ()


def check(url: str, host: str, forms: Sequence[UrlForm]) -> str:
    """Return the name of the form ``url`` matches, or raise ``UrlNotAllowed``.

    A matching URL is https on ``host`` with no port, credentials or fragment, and has at most one query parameter,
    which must be allowed for its form and have a positive integer value.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != host or parts.fragment:
        raise UrlNotAllowed(f"{url}: not an https URL on {host}")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError as err:
        raise UrlNotAllowed(f"{url}: malformed query") from err
    for form in forms:
        if not form.path.fullmatch(parts.path):
            continue
        if len(pairs) > 1:
            raise UrlNotAllowed(f"{url}: only one query parameter is allowed")
        for key, value in pairs:
            if key not in form.params or not POSITIVE_INT.fullmatch(value) or parts.query != f"{key}={value}":
                raise UrlNotAllowed(f"{url}: query parameter {key!r} is not allowed here")
        return form.name
    raise UrlNotAllowed(f"{url}: not a known page form")
