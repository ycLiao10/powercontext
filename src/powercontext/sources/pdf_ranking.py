# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline ranking of captured pages for a natural-language question.

Retrieval over filings cannot rely on plain lexical overlap alone. A question
such as *"what is the FY2022 unadjusted EBITDA less capex"* shares almost no
vocabulary with the page that answers it, while words like ``company`` or
``million`` appear on every page and would happily outvote the two terms that do
point at the evidence. Financial documents do, however, carry a strong and
stable structure:

* the primary statements announce themselves in a page heading, and questions
  frequently name the statement whose numbers they need;
* sections are introduced by short heading lines, so a question phrase found in
  a heading is worth far more than the same phrase inside running text;
* metric questions are answered from tables, which are dense with numbers.

This module combines that structure with BM25-style term weighting and a small
finance vocabulary, so a page is scored by how much of the *distinctive* part of
the question it covers rather than by how many common words it happens to repeat.
Everything here is pure and depends only on page text, which keeps it testable
without a runtime.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise

__all__ = [
    "Concept",
    "Page",
    "concepts",
    "normalize",
    "prepare",
    "query_terms",
    "rank",
    "statement_hints",
    "stem",
]

_WORD = re.compile(r"[a-z]+|\d+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# Filings print four-digit years, questions often write ``FY22``.
_SHORT_YEAR = re.compile(r"\bfy\s?'?(\d{2})\b", re.IGNORECASE)

# FinanceBench writes many line items as the abbreviation the filing itself uses
# only in tables. Expanding the acronym before tokenizing lets a question such as
# "did Pfizer grow its PPNE" reach the balance sheet line it names.
_ABBREVIATIONS = {
    "pp&e": "property plant and equipment",
    "ppe": "property plant and equipment",
    "ppne": "property plant and equipment",
    "cogs": "cost of revenue",
    "sg&a": "selling general and administrative",
    "sga": "selling general and administrative",
    "r&d": "research and development",
    "fcf": "free cash flow",
    "ocf": "operating cash flow",
    "eps": "earnings per share",
    "dso": "days sales outstanding",
    "dpo": "days payable outstanding",
    "asp": "average selling price",
    "ytd": "year to date",
}


def _expand(question: str) -> str:
    """Return ``question`` with finance abbreviations spelled out."""

    expanded = question
    for abbreviation, phrase in _ABBREVIATIONS.items():
        expanded = re.sub(rf"(?<![\w&]){re.escape(abbreviation)}(?![\w&])", phrase, expanded, flags=re.IGNORECASE)
    return expanded


def _years(text: str) -> set[str]:
    """Return the four-digit years a text mentions, including ``FY22`` forms."""

    found = {match.group() for match in _YEAR.finditer(text)}
    found.update(f"20{match.group(1)}" for match in _SHORT_YEAR.finditer(text))
    return found

# English function words, question phrasing, and the instruction boilerplate that
# FinanceBench questions carry ("assume you are a public equities analyst",
# "answer in USD billions"). Those words describe the request instead of the
# evidence, and dropping them is what stops a generic note page from winning.
_STOP_WORDS = frozenset({
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "also",
    "an",
    "and",
    "any",
    "approximately",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "calculate",
    "can",
    "company",
    "companys",
    "compute",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "dollar",
    "dollars",
    "down",
    "during",
    "each",
    "either",
    "else",
    "exactly",
    "explain",
    "few",
    "figure",
    "figures",
    "fiscal",
    "following",
    "for",
    "from",
    "further",
    "give",
    "given",
    "had",
    "has",
    "have",
    "having",
    "here",
    "how",
    "however",
    "i",
    "if",
    "in",
    "inc",
    "include",
    "included",
    "includes",
    "including",
    "information",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "less",
    "like",
    "made",
    "make",
    "many",
    "may",
    "me",
    "might",
    "million",
    "more",
    "most",
    "much",
    "must",
    "my",
    "no",
    "none",
    "nor",
    "not",
    "note",
    "notes",
    "now",
    "of",
    "off",
    "on",
    "once",
    "one",
    "only",
    "or",
    "other",
    "otherwise",
    "our",
    "out",
    "over",
    "own",
    "per",
    "percent",
    "percentage",
    "please",
    "provide",
    "question",
    "quite",
    "rather",
    "rely",
    "relying",
    "report",
    "reported",
    "reports",
    "respectively",
    "respond",
    "responding",
    "round",
    "rounded",
    "same",
    "say",
    "shall",
    "she",
    "should",
    "show",
    "shown",
    "since",
    "so",
    "some",
    "state",
    "such",
    "take",
    "taken",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "though",
    "thousand",
    "through",
    "thus",
    "to",
    "too",
    "under",
    "until",
    "up",
    "upon",
    "us",
    "usd",
    "use",
    "used",
    "uses",
    "using",
    "value",
    "values",
    "very",
    "was",
    "we",
    "well",
    "were",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "while",
    "who",
    "whole",
    "why",
    "will",
    "with",
    "within",
    "would",
    "year",
    "years",
    "you",
    "your",
})

# A page is classified by the statement heading it carries near its top.
_STATEMENT_HEADINGS = {
    "income": (
        "consolidated statement of income",
        "consolidated statements of income",
        "consolidated statement of operations",
        "consolidated statements of operations",
        "consolidated statement of earnings",
        "consolidated statements of earnings",
        "statement of operations",
        "statement of income",
        "statement of earnings",
    ),
    "balance": (
        "consolidated balance sheet",
        "consolidated balance sheets",
        "statement of financial position",
        "balance sheet",
    ),
    "cash": (
        "consolidated statement of cash flow",
        "consolidated statements of cash flow",
        "statement of cash flow",
        "statement of cash flows",
    ),
    "equity": (
        "consolidated statement of equity",
        "consolidated statements of equity",
        "statement of stockholders equity",
        "statement of shareholders equity",
    ),
}

# Phrases in a question that name the statement its evidence lives on.
_STATEMENT_HINTS = {
    "income": (
        "income statement",
        "statement of income",
        "statement of operations",
        "statements of operations",
        "p&l",
        "p & l",
        "profit and loss",
        "profit & loss",
        "statement of earnings",
    ),
    "balance": ("balance sheet", "statement of financial position", "financial position"),
    "cash": ("cash flow statement", "statement of cash flows", "statement of cash flow"),
}

# Finance vocabulary where a question and a filing name the same thing differently.
_SYNONYMS = {
    "acquisition": ("acquisition", "business combination", "merger"),
    "acquisitions": ("acquisitions", "acquisition", "business combination"),
    "asset": ("total assets", "assets", "property plant and equipment"),
    "backlog": ("backlog", "remaining performance obligations"),
    "capex": (
        "capital expenditures",
        "capital expenditure",
        "capital spending",
        "purchases of property plant and equipment",
        "property plant and equipment",
    ),
    "capital": ("capital expenditures", "capital expenditure", "property plant and equipment", "fixed assets"),
    "debt": ("long-term debt", "total debt", "borrowings", "notes payable"),
    "dividend": ("dividend", "dividends", "dividend per share"),
    "dividends": ("dividends", "dividend"),
    "ebitda": ("ebitda", "operating income", "operating profit", "depreciation and amortization"),
    "employees": ("employees", "employee", "headcount"),
    "geographies": ("geographic", "geographies", "region", "regions"),
    "geography": ("geographic", "geographies", "region", "regions"),
    "goodwill": ("goodwill", "impairment"),
    "intangible": ("intangible", "intangible assets", "amortization"),
    "inventories": ("inventories", "inventory"),
    "inventory": ("inventory", "inventories"),
    "legal": ("legal proceedings", "litigation", "claims"),
    "liabilities": ("total liabilities", "current liabilities", "liabilities"),
    "litigation": ("litigation", "legal proceedings", "claims"),
    "liquidity": ("liquidity", "current assets", "current liabilities", "working capital"),
    "margin": ("operating margin", "gross margin", "operating profit", "gross profit"),
    "pension": ("pension", "pension benefits", "retirement"),
    "ppe": ("property plant and equipment", "property and equipment", "fixed assets"),
    "profit": ("net income", "net earnings", "operating profit", "operating income", "gross profit"),
    "profitability": ("net income", "operating profit", "gross margin", "operating margin"),
    "receivable": ("receivable", "accounts receivable"),
    "receivables": ("receivables", "accounts receivable"),
    "restructuring": ("restructuring", "restructuring charges", "severance"),
    "revenue": ("revenue", "net sales", "net revenues", "total net sales"),
    "revenues": ("revenue", "net sales", "net revenues", "total net sales"),
    "sales": ("net sales", "revenue", "net revenues"),
    "segment": ("business segment", "segment", "segments"),
    "segments": ("business segment", "segments", "segment"),
    "turnover": ("turnover", "net sales", "revenue"),
}

# Questions that ask for a quantity are answered from table pages and from the
# primary statements. Narrative questions ("who are the customers", "is the
# business cyclical") are not, so the metric vocabulary below is what decides
# whether the statement and table priors apply at all.
_NUMERIC_INTENT = re.compile(
    r"how much|how many|amount|ratio|percentage|percent|%|value|total|growth|margin|cost|expense"
    r"|\brate\b|\btax\b|ebitda|\beps\b|per share|debt|liabilit|equity|dividend|asset|goodwill"
    r"|depreciation|amortization|inventor|receivable|backlog|obligation|provision|impairment"
    r"|return|yield|profit|loss|sales|revenue|income|cash|capital|increase|decrease|balance"
    r"|spend|spending|compensation|headcount|employees|share count|weighted average|grow|grew|growing",
    re.IGNORECASE,
)

_HEADING_MAX_WORDS = 9
_HEADING_MAX_CHARS = 72
# Questions and filings word the same metric differently ("cyclicality" against
# "cyclical"), so a term that is absent verbatim still counts when a page carries
# a term with the same opening letters.
PREFIX_WIDTH = 6
_PREFIX_WEIGHT = 0.55


def normalize(text: str) -> str:
    """Return ``text`` lower-cased with punctuation collapsed to single spaces."""

    lowered = text.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9.%]+", " ", lowered).strip()


def stem(token: str) -> str:
    """Return a light suffix-stripped form so plurals and tenses agree.

    A full Porter stemmer is unnecessary here: the only forms that matter for
    filing vocabulary are plurals (``inventories``), progressives
    (``operating``) and past tense (``consolidated``).
    """

    if len(token) <= 4:
        return token
    for suffix, replacement in (
        ("ies", "y"),
        ("sses", "ss"),
        ("ches", "ch"),
        ("shes", "sh"),
        ("xes", "x"),
        ("ing", ""),
        ("ed", ""),
        ("s", ""),
    ):
        if not token.endswith(suffix):
            continue
        if suffix == "s" and token.endswith(("ss", "us", "is")):
            continue
        if len(token) - len(suffix) < 3:
            continue
        return token[: -len(suffix)] + replacement
    return token


def _stems(text: str) -> list[str]:
    return [stem(token) for token in _WORD.findall(normalize(text)) if len(token) > 1 and token not in _STOP_WORDS]


def _phrase(text: str) -> str:
    """Stem a phrase into the form used inside a page's flattened token string."""

    return " ".join(_stems(text))


def _squash(text: str) -> str:
    """Collapse a phrase to letters and digits only.

    Extraction regularly breaks a heading across a line, so ``Consolidated
    Balance Sheet`` arrives as ``Consolidated Balance Shee t``. Comparing the
    squashed forms keeps such headings recognizable.
    """

    return re.sub(r"[^a-z0-9]+", "", text.casefold())


_STATEMENT_SQUASHED = {
    name: tuple(_squash(heading) for heading in headings) for name, headings in _STATEMENT_HEADINGS.items()
}
_STATEMENT_PHRASES = frozenset(_phrase(heading) for headings in _STATEMENT_HEADINGS.values() for heading in headings)
# Every page of a filing repeats its running head; it is not a section title.
_TOC_PHRASE = _phrase("Table of Contents")


def _synonym_index() -> dict[str, tuple[str, ...]]:
    """Key the finance vocabulary by stem, merging entries that stem alike."""

    index: dict[str, list[str]] = {}
    for key, forms in _SYNONYMS.items():
        index.setdefault(stem(key), []).extend(forms)
    return {key: tuple(dict.fromkeys(forms)) for key, forms in index.items()}


_SYNONYMS_BY_STEM = _synonym_index()


def statement_hints(question: str) -> frozenset[str]:
    """Return the statement families a question explicitly names."""

    flat = f" {normalize(question)} "
    stems_flat = f" {' '.join(_stems(question))} "
    hints = set()
    for name, phrases in _STATEMENT_HINTS.items():
        if any(normalize(phrase) in flat for phrase in phrases) or any(
            f" {_phrase(phrase)} " in stems_flat for phrase in phrases
        ):
            hints.add(name)
    return frozenset(hints)


def query_terms(question: str, limit: int = 12) -> tuple[str, ...]:
    """Return the distinctive words of a question, in order, for lexical search.

    The words are returned unstemmed because the Memory backend runs its own
    normalization; the stem is used only to drop repeats of the same word.
    """

    terms: list[str] = []
    seen: set[str] = set()
    for token in _WORD.findall(normalize(_expand(question))):
        if len(token) < 2 or token in _STOP_WORDS:
            continue
        key = stem(token)
        if key in seen:
            continue
        seen.add(key)
        terms.append(token)
        if len(terms) >= limit:
            break
    return tuple(terms)


def _section_lines(text: str) -> list[str]:
    """Return the heading-like lines of a page, stemmed and space padded.

    A filing announces its sections with short standalone lines: all-caps
    (``CUSTOMERS``), title case (``Risks Related to Our Contracts``) or an item
    prefix (``Item 1A. Risk Factors``). An evidence page is often the page where
    the relevant section *starts*, so a question term that appears in a heading
    outranks the same term buried in a paragraph.
    """

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or len(line) > _HEADING_MAX_CHARS:
            continue
        words = line.split()
        if len(words) > _HEADING_MAX_WORDS or line[-1] in ",;":
            continue
        letters = [character for character in line if character.isalpha()]
        if len(letters) < 3:
            continue
        capitals = sum(1 for character in letters if character.isupper()) / len(letters)
        title_case = sum(1 for word in words if word[:1].isupper()) / len(words)
        if (
            capitals < 0.6
            and title_case < 0.75
            and not line.endswith(":")
            and not line.startswith(("Item ", "ITEM ", "Part ", "PART "))
        ):
            continue
        phrase = _phrase(line)
        if phrase and phrase != _TOC_PHRASE:
            lines.append(f" {phrase} ")
    return lines


def _statement_strength(text: str) -> dict[str, float]:
    """Return how strongly a page announces each primary statement."""

    top = _squash(text[:200])
    scope = _squash(text[:420])
    strength: dict[str, float] = {}
    for name, squashed_headings in _STATEMENT_SQUASHED.items():
        if any(len(pattern) > 10 and pattern in top for pattern in squashed_headings):
            strength[name] = 1.0
        elif any(len(pattern) > 8 and pattern in scope for pattern in squashed_headings):
            strength[name] = 0.45
    return strength


@dataclass
class Page:
    """One candidate page prepared for ranking."""

    key: str
    text: str
    terms: Counter = field(default_factory=Counter)
    prefixes: Counter = field(default_factory=Counter)
    length: int = 0
    flat: str = ""
    heading_terms: frozenset = frozenset()
    section_lines: list = field(default_factory=list)
    statements: dict = field(default_factory=dict)
    numeric_ratio: float = 0.0
    year_counts: dict = field(default_factory=dict)


def prepare(key: str, text: str) -> Page:
    """Prepare one candidate page for repeated ranking."""

    raw = _WORD.findall(normalize(text))
    body = [stem(token) for token in raw if len(token) > 1 and token not in _STOP_WORDS]
    page = Page(key=key, text=text, terms=Counter(body), length=len(body))
    page.flat = f" {' '.join(body)} "
    page.prefixes = Counter(term[:PREFIX_WIDTH] for term in page.terms if len(term) >= PREFIX_WIDTH)
    page.section_lines = _section_lines(text)
    page.heading_terms = frozenset(term for line in page.section_lines for term in line.split())
    page.statements = _statement_strength(text)
    digits = sum(1 for token in raw if any(character.isdigit() for character in token))
    page.numeric_ratio = digits / max(len(raw), 1)
    for match in _YEAR.finditer(text):
        page.year_counts[match.group()] = page.year_counts.get(match.group(), 0) + 1
    return page


@dataclass(frozen=True)
class Concept:
    """One query concept: interchangeable surface forms and its weight."""

    weight: float
    forms: tuple[str, ...]


def concepts(question: str) -> list[Concept]:  # noqa: C901 - one explicit list of concepts
    """Return the weighted concepts a question is ranked by.

    Boilerplate is dropped, adjacent word pairs become phrase concepts (far more
    selective than either word alone), and a word that already speaks through its
    phrase is discounted so a page cannot win by repeating one generic term.
    """

    expanded = _expand(question)
    ordered: list[str] = []
    seen: set[str] = set()
    for token in _WORD.findall(normalize(expanded)):
        if len(token) < 2 or token in _STOP_WORDS or _YEAR.fullmatch(token):
            continue
        key = stem(token)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(token)

    phrases: list[str] = []
    covered: set[str] = set()
    for left, right in pairwise(ordered):
        phrase = _phrase(f"{left} {right}")
        if " " not in phrase:
            continue
        phrases.append(phrase)
        covered.update((stem(left), stem(right)))

    out: list[Concept] = []
    for word in ordered:
        forms = [word]
        forms.extend(_SYNONYMS_BY_STEM.get(stem(word), ()))
        stems_forms = tuple(dict.fromkeys(_phrase(form) for form in forms if _phrase(form)))
        single = stems_forms == (_phrase(word),)
        out.append(Concept(0.45 if single and stem(word) in covered else 1.0, stems_forms))
    for phrase in dict.fromkeys(phrases):
        out.append(Concept(1.7, (phrase,)))
    flat = normalize(expanded)
    for phrases_hint in _STATEMENT_HINTS.values():
        for phrase in phrases_hint:
            if normalize(phrase) in flat:
                stems_phrase = _phrase(phrase)
                if stems_phrase:
                    out.append(Concept(2.6, (stems_phrase,)))
                break
    return out


def rank(  # noqa: C901 - the score is one explicit sum of independent signals
    question: str,
    pages: list[Page],
    *,
    limit: int = 20,
    k1: float = 1.2,
    b: float = 0.55,
    statement_weight: float = 12.0,
    primary_statement_weight: float = 5.0,
    section_weight: float = 4.5,
    table_weight: float = 2.0,
    year_weight: float = 2.0,
    heading_weight: float = 1.2,
) -> list[tuple[str, float]]:
    """Return ``(page key, score)`` in descending relevance, at most ``limit`` pages."""

    total = max(len(pages), 1)
    average = max(sum(page.length for page in pages) / total, 1.0)
    document_frequency: Counter = Counter()
    for page in pages:
        document_frequency.update(page.terms.keys())
        document_frequency.update({f"~{prefix}": 1 for prefix in page.prefixes})
    inverse = {
        term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
        for term, count in document_frequency.items()
    }
    unseen = math.log(1.0 + (total + 0.5) / 0.5)

    query_concepts = concepts(question)
    hints = statement_hints(question)
    wants_number = bool(_NUMERIC_INTENT.search(question))
    query_years = _years(question)
    heading_query = {term for concept in query_concepts for form in concept.forms for term in form.split()}

    scored: list[tuple[float, Page]] = []
    for page in pages:
        denominator = k1 * (1 - b + b * page.length / average)
        sections = f" {' '.join(page.section_lines)} "
        score = 0.0
        for concept in query_concepts:
            best = 0.0
            section_idf = 0.0
            for form in concept.forms:
                if " " in form:
                    occurrences = page.flat.count(f" {form} ")
                    term_idf = min(sum(inverse.get(part, unseen) for part in form.split()), unseen * 2)
                    # Statement names are scored by the statement signal instead: a
                    # note page that mentions "the balance sheet" in a sub-heading is
                    # not the balance sheet page.
                    if form not in _STATEMENT_PHRASES and f" {form} " in sections:
                        section_idf = max(section_idf, term_idf)
                else:
                    occurrences = page.terms.get(form, 0)
                    term_idf = inverse.get(form, unseen)
                    if occurrences == 0 and len(form) >= PREFIX_WIDTH:
                        # The page carries the same word with a different ending.
                        prefix = form[:PREFIX_WIDTH]
                        occurrences = page.prefixes.get(prefix, 0)
                        term_idf = _PREFIX_WEIGHT * inverse.get(f"~{prefix}", unseen)
                if occurrences <= 0:
                    continue
                best = max(best, term_idf * occurrences * (k1 + 1) / (occurrences + denominator))
            score += concept.weight * best
            if section_idf:
                # A term in a page heading earns more than the same term in running
                # text, and a rare heading term earns more than a generic one.
                score += section_weight * concept.weight * min(section_idf / 3.0, 2.0)
        if hints:
            # A named statement is a prior on the page, not a substitute for
            # matching the question: it multiplies the lexical score so a page
            # that also carries the question's terms wins, while an unrelated
            # statement page cannot vault over a better lexical candidate.
            score *= 1.0 + (statement_weight / 12.0) * max(
                (page.statements.get(name, 0.0) for name in hints), default=0.0
            )
        elif wants_number and page.statements:
            # A quantity question that never names a statement but asks how the
            # business is financed, how liquid it is, or how much it invests is
            # answered by the primary statements themselves. A narrative question
            # ("who are the customers") is not: there the statements only take up
            # slots that belong to the business description.
            score += primary_statement_weight * max(
                (strength for name, strength in page.statements.items() if name != "equity"), default=0.0
            )
        if wants_number and page.numeric_ratio >= 0.16:
            score += table_weight * min(page.numeric_ratio / 0.28, 1.8)
        if query_years:
            hits = sum(1 for year in query_years if page.year_counts.get(year, 0) >= 2)
            score += year_weight * hits / len(query_years)
        score += heading_weight * min(len(page.heading_terms & heading_query), 5)
        scored.append((score, page))

    scored.sort(key=lambda item: (-item[0], item[1].key))
    return [(page.key, score) for score, page in scored[:limit]]
