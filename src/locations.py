"""India location detection.

A job is kept only when India is explicitly one of its locations. Text comes
from ATS location fields, schema.org JSON-LD and job cards, e.g.
"Hyderabad, Telangana, India", "Bengaluru Millenia", "IN-KA",
"Singapore | Hyderabad", "India or US", "Kolkata (AC) - Bangalore - RMZ Hebbal".

City names alone are trusted unless the same location names a foreign
country or state right after them ("Hyderabad, Pakistan", "Delhi, Ontario,
Canada", "Kochi, Japan", "Salem, MA").
"""

from __future__ import annotations

import re

INDIA_CITIES = [
    "bengaluru", "bangalore", "hyderabad", "secunderabad", "pune", "chennai", "mumbai", "navi mumbai",
    "thane", "gurgaon", "gurugram", "noida", "greater noida", "delhi", "new delhi", "delhi ncr", "ncr",
    "kolkata", "ahmedabad", "gandhinagar", "vadodara", "surat", "rajkot", "kochi", "cochin",
    "thiruvananthapuram", "trivandrum", "kozhikode", "calicut", "thrissur", "coimbatore", "madurai",
    "tiruchirappalli", "trichy", "salem", "vellore", "hosur", "chengalpattu", "sriperumbudur",
    "pondicherry", "puducherry", "mysuru", "mysore", "mangaluru", "mangalore", "manipal", "udupi",
    "hubli", "hubballi", "belagavi", "belgaum", "indore", "bhopal", "jaipur", "jodhpur", "udaipur",
    "chandigarh", "mohali", "panchkula", "ludhiana", "amritsar", "dehradun", "lucknow", "kanpur",
    "varanasi", "nagpur", "nashik", "aurangabad", "kolhapur", "visakhapatnam", "vizag", "vijayawada",
    "guntur", "tirupati", "warangal", "bhubaneswar", "cuttack", "raipur", "ranchi", "jamshedpur",
    "patna", "guwahati", "siliguri", "durgapur", "faridabad", "ghaziabad", "sonipat", "manesar",
    "goa", "panaji", "whitefield", "electronic city", "gachibowli", "hitec city", "hitech city",
]
INDIA_STATES = [
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "gujarat", "haryana",
    "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur",
    "meghalaya", "mizoram", "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "jammu", "srinagar", "ladakh",
]
# Foreign places that share a name with an Indian city/state, or commonly
# follow one: "Hyderabad, Pakistan", "Delhi, Ontario", "Punjab, Pakistan".
_FOREIGN_NAMES = [
    "pakistan", "bangladesh", "sri lanka", "nepal", "japan", "china", "singapore", "malaysia",
    "philippines", "indonesia", "thailand", "vietnam", "australia", "new zealand", "canada",
    "united states", "united states of america", "usa", "america", "united kingdom", "england",
    "scotland", "ireland", "germany", "france", "spain", "italy", "netherlands", "poland",
    "brazil", "mexico", "argentina", "south africa", "nigeria", "kenya", "egypt", "uae",
    "united arab emirates", "saudi arabia", "qatar",
    # Canadian provinces / US states that follow an ambiguous city name
    "ontario", "quebec", "british columbia", "alberta", "new york", "california", "texas",
    "massachusetts", "oregon", "ohio", "illinois", "virginia", "new jersey", "florida",
    "north carolina", "georgia", "pennsylvania", "washington", "indiana", "louisiana", "missouri",
    "sindh", "punjab province", "lahore", "karachi", "islamabad", "rawalpindi", "faisalabad", "multan",
]
# US/Canadian abbreviations after a comma ("Salem, MA"). Codes that are also
# Indian state codes (GA, MN, AR, OR, TN, UT, AS, ...) are deliberately left out.
_FOREIGN_ABBR = {
    "AL", "AK", "AZ", "CA", "CO", "DE", "FL", "HI", "ID", "IL", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "PA", "RI", "SC", "SD", "TX", "VT", "VA", "WA", "WV", "WI", "WY", "ON", "QC", "BC", "AB",
    "US", "USA", "UK", "GB", "JP", "PK", "SG", "CN", "AU",
}

_PLACE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in sorted(INDIA_CITIES + INDIA_STATES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_INDIA_WORD_RE = re.compile(r"\bindia\b|\bbharat\b", re.IGNORECASE)
# ISO codes: "IN", "IND" as a whole field, or subdivision codes "IN-KA", "IN-TG"
_INDIA_CODE_RE = re.compile(r"(?<![A-Za-z])IN-[A-Z]{2,3}(?![A-Za-z])")
_INDIA_BARE_CODE_RE = re.compile(r"^\s*(?:IN|IND)\s*$")
_FOREIGN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in sorted(_FOREIGN_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# separate locations of a multi-location posting
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\||;|\n|·|/|\s\+\s|\bor\b|\band\b)\s*", re.IGNORECASE)


def _part_is_foreign(part: str) -> bool:
    part = part.strip(" .()-")
    return bool(_FOREIGN_RE.search(part)) or part in _FOREIGN_ABBR


def _segment_is_india(segment: str) -> bool:
    if _INDIA_WORD_RE.search(segment) or _INDIA_CODE_RE.search(segment) or _INDIA_BARE_CODE_RE.match(segment):
        return True
    parts = [p for p in re.split(r"\s*,\s*", segment) if p.strip()]
    for i, part in enumerate(parts):
        if not _PLACE_RE.search(part):
            continue
        # a foreign country/state next to the city decides it
        # ("Hyderabad, Pakistan", "Lahore, Punjab", "Delhi, Ontario, Canada")
        neighbours = parts[max(0, i - 1):i] + parts[i + 1:i + 3]
        if _part_is_foreign(part.replace(_PLACE_RE.search(part).group(0), "")) or any(
            _part_is_foreign(p) for p in neighbours
        ):
            continue
        # "Pune, IN" (India code after an Indian city) or plain "Pune"
        return True
    return False


def india_segments(text: str) -> list[str]:
    """The parts of a (possibly multi-location) location string that are in India."""
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(text or "") if s and s.strip() and _segment_is_india(s)]


def is_india(text: str) -> bool:
    """True when India is explicitly one of the locations in ``text``."""
    return bool(india_segments(text))


_LOCATION_FILLER_RE = re.compile(
    r"\b(?:remote|hybrid|on[\s-]?site|office|offices|hub|campus|location|locations|city|cities|region|"
    r"state|country|multiple|various|all|india)\b|[,|/()\-–—:·&]|\band\b|\bor\b",
    re.IGNORECASE,
)


def is_location_only(text: str) -> bool:
    """"Hyderabad, India", "India" — a link or title that is just a place
    (a location page or country picker), not a job title."""
    if not is_india(text):
        return False
    rest = _PLACE_RE.sub(" ", text or "")
    rest = _INDIA_CODE_RE.sub(" ", rest)
    rest = _LOCATION_FILLER_RE.sub(" ", rest)
    return not re.search(r"[^\W\d_]", rest)
