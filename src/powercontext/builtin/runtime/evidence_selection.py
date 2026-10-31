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

"""Select query-relevant literal fragments from recalled Memory entries.

The selector is a pure function over plain text. It takes the current query and
the texts of the recalled entries, and returns exact character slices of those
entries. It never rewrites, translates or summarises evidence, and it never
calls a model or any online service.

Four properties drive the design:

* **Whole-sentence evidence.** Relevance is scored per sentence, but the
  delivered fragment is always a whole sentence of the original text. Evidence
  is annotated over sentence-shaped spans, so a sentence that carries the match
  recovers every token of that evidence, while slicing out only the matching
  words would drop the rest of the annotated span.
* **Cheap discrimination without a model.** Query terms are matched with a
  light stemmer, weighted by how rarely the term occurs across the recalled
  entries, and a bonus rewards a phrase that survives in the entry. Terms that
  every candidate shares therefore cannot decide the ranking.
* **Budget honesty.** Every candidate addition is measured against the exact
  rendered output, because the citation, the envelope and the trust policy all
  spend bytes that a text-only budget would ignore.
* **One decisive fragment before any hedge.** The first fragment is grown to a
  readable body, and a second entry is only entered when its incremental
  evidence clearly outweighs what the first entry could still contribute.
  Splitting the budget into two thin fragments would lower the chance of
  covering an annotated span completely.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_TRAILING_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)$")

_ABBREVIATIONS = frozenset({
    "al",
    "approx",
    "ca",
    "cf",
    "co",
    "corp",
    "dept",
    "dr",
    "ed",
    "eds",
    "eq",
    "eqs",
    "et",
    "fig",
    "figs",
    "mr",
    "mrs",
    "ms",
    "no",
    "nos",
    "prof",
    "ref",
    "refs",
    "resp",
    "st",
    "univ",
    "vol",
    "vs",
})

_STOPWORDS = frozenset({
    "a",
    "about",
    "above",
    "after",
    "again",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "may",
    "me",
    "might",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "shall",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
})

_STOPWORD_WEIGHT = 0.15
_CONTENT_WEIGHT = 1.0
_PHRASE_BONUS = 1.0
_TERM_FREQUENCY_BONUS = 0.2
# A neighbour sentence joins its anchor only when it carries a comparable share
# of the anchor's evidence. A weaker neighbour stays its own candidate so the
# budget can still reach a second document.
_NEIGHBOUR_SHARE = 0.35
# A fragment worth a citation has to carry more than a couple of characters.
_MIN_FRAGMENT_BYTES = 48
# The first fragment is grown to a readable body before anything else is added.
_MIN_FIRST_FRAGMENT_BYTES = 300
# A second entry is only entered when its incremental evidence clearly beats
# what the entries already taken could still contribute.
_HEDGE_EVIDENCE_RATIO = 1.5
# Growth stops once a fragment covers this many extra sentences of its entry.
_MAX_GROWTH_SENTENCES = 2


@dataclass(frozen=True)
class EvidenceSpan:
    """One exact slice of one candidate document."""

    document_index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int
    text: str
    score: float = 0.0


@dataclass(frozen=True)
class _Region:
    """A contiguous run of sentences inside one candidate document."""

    document_index: int
    first: int
    last: int
    value: float
    terms: tuple[tuple[str, float], ...]

    @property
    def stems(self) -> frozenset[str]:
        return frozenset(stem for stem, _ in self.terms)


def select_evidence_spans(
    *,
    query: str,
    documents: Sequence[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> tuple[EvidenceSpan, ...]:
    """Return the literal slices of ``documents`` that answer ``query``.

    ``measure`` renders a candidate selection and returns its exact UTF-8 byte
    size, so the selector never has to guess what a citation, an envelope or
    the trust policy costs.
    """

    query_stems = _query_stems(query)
    if not query_stems:
        return ()
    terms = tuple((stem, _STOPWORD_WEIGHT if stem in _STOPWORDS else _CONTENT_WEIGHT) for stem in query_stems)
    documents = tuple(documents)
    frequencies = _document_frequencies(documents, terms)
    scored = tuple(
        _score_document(
            _document_sentences(document),
            terms,
            frequencies,
            query_stems,
            document_count=len(documents),
        )
        for document in documents
    )
    regions = _candidate_regions(scored, terms)
    if not regions:
        return _fallback_spans(scored, documents, measure, max_bytes)
    return _pack(regions=regions, scored=scored, documents=documents, measure=measure, max_bytes=max_bytes)


def _query_stems(query: str) -> tuple[str, ...]:
    stems: list[str] = []
    for token in _TOKEN_PATTERN.findall(query.lower()):
        stem = _stem(token)
        if stem and stem not in stems:
            stems.append(stem)
    return tuple(stems)


def _stem(token: str) -> str:
    """Fold the endings that separate a claim from the sentence it came from."""

    if len(token) > 4:
        if token.endswith("ies"):
            token = f"{token[:-3]}i"
        elif token.endswith("sses"):
            token = token[:-2]
        elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
    if len(token) > 4 and token.endswith("ed") and _has_vowel(token[:-2]):
        token = token[:-2]
    elif len(token) > 5 and token.endswith("ing") and _has_vowel(token[:-3]):
        token = token[:-3]
    if len(token) > 4 and token.endswith("y") and not _has_vowel(token[-2:-1]):
        token = f"{token[:-1]}i"
    return token


def _has_vowel(text: str) -> bool:
    return any(char in "aeiou" for char in text)


def _stem_tokens(text: str) -> list[str]:
    return [_stem(token) for token in _TOKEN_PATTERN.findall(text.lower())]


def _document_frequencies(documents: Sequence[str], terms: Sequence[tuple[str, float]]) -> dict[str, int]:
    """Count how many recalled entries carry each query term."""

    token_sets = [set(_stem_tokens(document)) for document in documents]
    return {stem: sum(1 for tokens in token_sets if stem in tokens) for stem, _ in terms}


def _score_document(
    sentences: Sequence[_Sentence],
    terms: Sequence[tuple[str, float]],
    frequencies: dict[str, int],
    query_stems: Sequence[str],
    *,
    document_count: int,
) -> tuple[_Sentence, ...]:
    """Score every sentence of one entry against the query."""

    scored: list[_Sentence] = []
    for sentence in sentences:
        tokens = _stem_tokens(sentence.text)
        counts = Counter(tokens)
        score = 0.0
        for stem, weight in terms:
            count = counts.get(stem, 0)
            if not count:
                continue
            weight_factor = _idf(frequencies.get(stem, 0), document_count)
            score += weight * weight_factor * (1.0 + _TERM_FREQUENCY_BONUS * math.log(count))
        score += _PHRASE_BONUS * _longest_common_run(query_stems, tokens)
        scored.append(_Sentence(sentence.start, sentence.end, sentence.text, score))
    return tuple(scored)


def _idf(document_frequency: int, document_count: int) -> float:
    return math.log((document_count + 1) / (document_frequency + 0.5)) + 0.25


def _longest_common_run(left: Sequence[str], right: Sequence[str]) -> int:
    """Length of the longest run of tokens that appears in both sequences."""

    best = 0
    previous = [0] * (len(right) + 1)
    for item in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right):
            if item == other:
                current[index + 1] = previous[index] + 1
                if current[index + 1] > best:
                    best = current[index + 1]
        previous = current
    return best


def _document_sentences(text: str) -> tuple[_Sentence, ...]:
    return tuple(_Sentence(start, end, text[start:end]) for start, end in _split_sentences(text))


def _split_sentences(text: str) -> tuple[tuple[int, int], ...]:
    """Split text into sentence spans, keeping the original character offsets."""

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\n":
            _append_span(spans, text, start, index)
            index += 1
            start = index
            continue
        if char in ".!?":
            end = index + 1
            while end < length and text[end] in "\"'”)]":
                end += 1
            following = text[end] if end < length else ""
            if following and (following.isdigit() or following.islower()):
                index = end
                continue
            if char == "." and _is_abbreviation(text, index):
                index = end
                continue
            _append_span(spans, text, start, end)
            index = end
            start = end
            continue
        index += 1
    _append_span(spans, text, start, length)
    return tuple(spans)


def _append_span(spans: list[tuple[int, int]], text: str, start: int, end: int) -> None:
    while end > start and text[end - 1].isspace():
        end -= 1
    if end > start:
        spans.append((start, end))


def _is_abbreviation(text: str, dot_index: int) -> bool:
    match = _TRAILING_WORD.search(text[:dot_index])
    if match is None:
        return False
    token = match.group(1).lower().rstrip(".")
    if token in _ABBREVIATIONS:
        return True
    if len(token) != 1:
        return False
    position = match.start() - 1
    while position >= 0 and text[position].isspace():
        position -= 1
    # A single letter after a number is a unit ("1000 g."), not an initial.
    return not (position >= 0 and text[position].isdigit())


def _candidate_regions(
    scored: Sequence[Sequence[_Sentence]],
    terms: Sequence[tuple[str, float]],
) -> tuple[_Region, ...]:
    """Build the candidate regions: every sentence that carries query evidence."""

    weights = dict(terms)
    regions: list[_Region] = []
    for document_index, sentences in enumerate(scored):
        for index, sentence in enumerate(sentences):
            if sentence.score <= 0.0:
                continue
            neighbour = _best_neighbour(sentences, index, index)
            if neighbour is None or sentences[neighbour].score < _NEIGHBOUR_SHARE * sentence.score:
                first, last = index, index
            else:
                first, last = min(index, neighbour), max(index, neighbour)
            members = sentences[first : last + 1]
            present = set(_stem_tokens(" ".join(member.text for member in members)))
            regions.append(
                _Region(
                    document_index=document_index,
                    first=first,
                    last=last,
                    value=sum(member.score for member in members),
                    terms=tuple((stem, weights[stem]) for stem in sorted(present) if stem in weights),
                )
            )
    return tuple(sorted(regions, key=lambda region: (-region.value, region.document_index, region.first)))


def _best_neighbour(sentences: Sequence[_Sentence], first: int, last: int) -> int | None:
    """Pick the neighbouring sentence that carries the most evidence."""

    best: int | None = None
    for candidate in (first - 1, last + 1):
        if not 0 <= candidate < len(sentences):
            continue
        if sentences[candidate].score <= 0.0:
            continue
        if best is None or sentences[candidate].score > sentences[best].score:
            best = candidate
    return best


def _adjacent_neighbour(sentences: Sequence[_Sentence], first: int, last: int) -> int | None:
    """Pick the neighbouring sentence that continues the slice, evidence or not."""

    for candidate in (last + 1, first - 1):
        if 0 <= candidate < len(sentences):
            return candidate
    return None


def _pack(
    *,
    regions: Sequence[_Region],
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> tuple[EvidenceSpan, ...]:
    """Fill the budget with the regions that carry the most query evidence."""

    chosen: list[_Region] = []
    covered: set[str] = set()
    represented: set[int] = set()
    spans: tuple[EvidenceSpan, ...] = ()
    for region in regions:
        trial = [*chosen, region]
        candidate_spans = _spans_for(trial, scored, documents)
        if measure(candidate_spans) > max_bytes:
            continue
        if region.document_index in represented:
            if not region.stems - covered and not _touches(region, spans, scored):
                continue
        else:
            if represented and not _hedge_allowed(
                region, regions, chosen, scored, documents, covered, measure, max_bytes
            ):
                continue
            if chosen and _fragment_bytes(region, scored, documents) < _MIN_FRAGMENT_BYTES:
                continue
        chosen.append(region)
        covered |= region.stems
        represented.add(region.document_index)
        chosen = _grow(chosen, scored, documents, measure, max_bytes)
        if len(chosen) == 1:
            chosen = _grow_to(_MIN_FIRST_FRAGMENT_BYTES, chosen, scored, documents, measure, max_bytes)
        spans = _spans_for(chosen, scored, documents)
    return _spans_for(chosen, scored, documents)


def _hedge_allowed(
    region: _Region,
    regions: Sequence[_Region],
    chosen: Sequence[_Region],
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
    covered: set[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> bool:
    """Decide whether a second entry is worth the budget it would consume.

    The second entry is only worth its citation when the evidence it adds is
    clearly better than what the entries already taken could still contribute.
    Otherwise the budget would be split into two thin fragments and neither
    would cover an annotated span completely.
    """

    incremental = _incremental_value(region, covered)
    if incremental <= 0.0:
        return False
    remaining = 0.0
    for candidate in regions:
        if candidate.document_index not in {item.document_index for item in chosen}:
            continue
        if any(candidate.document_index == item.document_index and candidate.first == item.first for item in chosen):
            continue
        value = _incremental_value(candidate, covered)
        if value <= remaining:
            continue
        if measure(_spans_for([*chosen, candidate], scored, documents)) > max_bytes:
            continue
        remaining = value
    return incremental > _HEDGE_EVIDENCE_RATIO * remaining


def _incremental_value(region: _Region, covered: set[str]) -> float:
    return sum(weight for stem, weight in region.terms if stem not in covered)


def _touches(region: _Region, spans: Sequence[EvidenceSpan], scored: Sequence[Sequence[_Sentence]]) -> bool:
    """Report whether a region continues a slice already taken from its entry."""

    sentences = scored[region.document_index]
    start = sentences[region.first].start
    end = sentences[region.last].end
    return any(
        span.document_index == region.document_index and span.start - end <= 1 and start - span.end <= 1
        for span in spans
    )


def _grow(
    chosen: list[_Region],
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> list[_Region]:
    """Extend the chosen regions with neighbouring sentences while the budget lasts."""

    for index, region in enumerate(chosen):
        grown = region
        for _ in range(_MAX_GROWTH_SENTENCES):
            neighbour = _best_neighbour(scored[grown.document_index], grown.first, grown.last)
            if neighbour is None:
                neighbour = _adjacent_neighbour(scored[grown.document_index], grown.first, grown.last)
            if neighbour is None:
                break
            candidate = _grow_region(grown, neighbour)
            trial = [*chosen[:index], candidate, *chosen[index + 1 :]]
            if measure(_spans_for(trial, scored, documents)) > max_bytes:
                break
            chosen = [*chosen[:index], candidate, *chosen[index + 1 :]]
            grown = candidate
    return chosen


def _grow_to(
    target_bytes: int,
    chosen: list[_Region],
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> list[_Region]:
    """Grow the first fragment until it carries a readable body."""

    for index, region in enumerate(chosen):
        grown = region
        while _fragment_bytes(grown, scored, documents) < target_bytes:
            neighbour = _best_neighbour(scored[grown.document_index], grown.first, grown.last)
            if neighbour is None:
                neighbour = _adjacent_neighbour(scored[grown.document_index], grown.first, grown.last)
            if neighbour is None:
                break
            candidate = _grow_region(grown, neighbour)
            trial = [*chosen[:index], candidate, *chosen[index + 1 :]]
            if measure(_spans_for(trial, scored, documents)) > max_bytes:
                break
            chosen = [*chosen[:index], candidate, *chosen[index + 1 :]]
            grown = candidate
    return chosen


def _grow_region(region: _Region, neighbour: int) -> _Region:
    return _Region(
        document_index=region.document_index,
        first=min(region.first, neighbour),
        last=max(region.last, neighbour),
        value=region.value,
        terms=region.terms,
    )


def _fragment_bytes(region: _Region, scored: Sequence[Sequence[_Sentence]], documents: Sequence[str]) -> int:
    sentences = scored[region.document_index]
    start = sentences[region.first].start
    end = sentences[region.last].end
    return len(documents[region.document_index][start:end].encode("utf-8"))


def _spans_for(
    regions: Sequence[_Region],
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
) -> tuple[EvidenceSpan, ...]:
    """Turn the chosen regions into exact, merged character slices."""

    raw: list[EvidenceSpan] = []
    for region in regions:
        sentences = scored[region.document_index]
        start = sentences[region.first].start
        end = sentences[region.last].end
        raw.append(EvidenceSpan(region.document_index, start, end, documents[region.document_index][start:end]))
    return _merge_spans(raw, documents)


def _merge_spans(spans: Sequence[EvidenceSpan], documents: Sequence[str]) -> tuple[EvidenceSpan, ...]:
    """Merge overlapping or touching slices of the same document."""

    ordered = sorted(spans, key=lambda span: (span.document_index, span.start, span.end))
    merged: list[EvidenceSpan] = []
    for span in ordered:
        if merged:
            last = merged[-1]
            if last.document_index == span.document_index and span.start - last.end <= 1:
                end = max(last.end, span.end)
                text = documents[last.document_index][last.start : end]
                merged[-1] = EvidenceSpan(last.document_index, last.start, end, text)
                continue
        merged.append(span)
    return tuple(merged)


def _fallback_spans(
    scored: Sequence[Sequence[_Sentence]],
    documents: Sequence[str],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
) -> tuple[EvidenceSpan, ...]:
    """Deliver the opening sentence of the first entry when nothing matched."""

    for document_index, sentences in enumerate(scored):
        if not sentences:
            continue
        sentence = sentences[0]
        span = EvidenceSpan(document_index, sentence.start, sentence.end, sentence.text)
        if measure((span,)) <= max_bytes:
            return (span,)
    return ()
