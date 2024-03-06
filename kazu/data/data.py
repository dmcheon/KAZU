"""This module contains the core aspects of the :doc:`/datamodel`.

See the page linked above for a quick introduction to the key concepts.

.. |metadata_s11n_warn| replace::
   Note that storing objects here that don't straightforwardly
   convert to and from json may cause problems for (de)serialization.
   See :ref:`data-serialization` for more details.

.. |from_dict_note| replace:: See :ref:`data-serialization`.
"""

import dataclasses
import json
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, auto, IntEnum
from math import inf
from typing import Any, Optional, Union
from collections.abc import Iterable

import bson
from bson import json_util
import cattrs.preconf.json
import cattrs.strategies
from numpy import ndarray, float32, float16

from kazu.utils.string_normalizer import StringNormalizer

IS_SUBSPAN = "is_subspan"
# BIO schema
ENTITY_START_SYMBOL = "B"
ENTITY_INSIDE_SYMBOL = "I"
ENTITY_OUTSIDE_SYMBOL = "O"

# key for Document Processing Failed
PROCESSING_EXCEPTION = "PROCESSING_EXCEPTION"


JsonEncodable = Optional[
    Union[dict[str, "JsonEncodable"], list["JsonEncodable"], int, float, bool, str]
]
"""Represents a json-encodable object.

Note that because :class:`dict` is invariant, there can be issues with
using types like ``dict[str, str]`` (see further
`here <https://github.com/python/typing/issues/182#issuecomment-1320974824>`_).
"""


class AutoNameEnum(Enum):
    """Subclass to create an Enum where values are the names when using
    :class:`enum.auto`\\ .

    Taken from the `Python Enum Docs <https://docs.python.org/3/howto/enum.html#using-automatic-values>`_.

    This is `licensed under Zero-Clause BSD. <https://docs.python.org/3/license.html#zero-clause-bsd-license-for-code-in-the-python-release-documentation>`_

    .. raw:: html

        <details>
        <summary>Full License</summary>

    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted.

    THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
    REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
    INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
    LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
    OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
    PERFORMANCE OF THIS SOFTWARE.

    .. raw:: html

        </details>
    """

    def _generate_next_value_(name, start, count, last_values):
        return name


class MentionConfidence(IntEnum):
    HIGHLY_LIKELY = 100  # almost certain to be correct
    PROBABLE = 50
    POSSIBLE = 10  # high degree of uncertainty
    IGNORE = 0  # do not use this mention for NER


class StringMatchConfidence(AutoNameEnum):
    HIGHLY_LIKELY = auto()  # almost certain to be correct
    PROBABLE = auto()  # on the balance of probabilities, will be correct
    POSSIBLE = auto()  # high degree of uncertainty


class DisambiguationConfidence(AutoNameEnum):
    HIGHLY_LIKELY = auto()  # almost certain to be correct
    PROBABLE = auto()  # on the balance of probabilities, will be correct
    POSSIBLE = auto()  # high degree of uncertainty
    AMBIGUOUS = auto()  # could not disambiguate


@dataclass(frozen=True)
class CharSpan:
    """A concept similar to a spaCy Span, except is character index based rather than
    token based."""

    start: int
    end: int

    def is_completely_overlapped(self, other: "CharSpan") -> bool:
        """True if other completely overlaps this span.

        :param other:
        :return:
        """
        return self.start >= other.start and self.end <= other.end

    def is_partially_overlapped(self, other: "CharSpan") -> bool:
        """True if other partially overlaps this span.

        :param other:
        :return:
        """
        return (other.start <= self.start <= other.end) or (other.start <= self.end <= other.end)

    def __lt__(self, other):
        return self.start < other.start

    def __gt__(self, other):
        return self.end > other.end


class EquivalentIdAggregationStrategy(AutoNameEnum):
    NO_STRATEGY = auto()  # no strategy. should be used for debugging/testing only
    RESOLVED_BY_SIMILARITY = auto()  # synonym linked to ID via similarity to default ID label
    SYNONYM_IS_AMBIGUOUS = auto()  # synonym has no unambiguous meaning
    CUSTOM = auto()  # a place holder for any strategy that
    UNAMBIGUOUS = auto()
    MERGED_AS_NON_SYMBOLIC = auto()  # used when non-symbolic synonyms are merged
    MODIFIED_BY_CURATION = auto()
    RESOLVED_BY_XREF = auto()


# this is frozen below, but for use in typehints elsewhere the mutable version is
# more useful, as a function written for an immutable frozenset would often behave
# correctly when passed a mutable set, and sometimes our functions actually produce
# the mutable version, as all we wish to do is compare with an immutable version,
# and the mutable version is easier to produce when constructed iteratively.
IdsAndSource = set[tuple[str, str]]


@dataclass(frozen=True, eq=True, order=True)
class EquivalentIdSet:
    """A representation of a set of kb ID's that map to the same synonym and mean the
    same thing."""

    # other ID's mapping to this syn, from different KBs
    ids_and_source: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    @property
    def sources(self) -> set[str]:
        return set(x[1] for x in self.ids_and_source)

    @property
    def ids(self) -> set[str]:
        return set(x[0] for x in self.ids_and_source)


@dataclass(frozen=True)
class Mapping:
    """A mapping is a fully mapped and disambiguated kb concept."""

    #: default label from knowledgebase
    default_label: str
    #: the knowledgebase/database/ontology name
    source: str
    #: the origin of this mapping
    parser_name: str
    #: the identifier within the KB
    idx: str
    string_match_strategy: str
    string_match_confidence: StringMatchConfidence
    disambiguation_confidence: Optional[DisambiguationConfidence] = None
    disambiguation_strategy: Optional[str] = None
    #: source parser name if mapping is an XREF
    xref_source_parser_name: Optional[str] = None
    #: | generic metadata
    #: |
    #: | |metadata_s11n_warn|
    metadata: dict = field(default_factory=dict, hash=False)

    @staticmethod
    def from_dict(mapping_dict: dict) -> "Mapping":
        """|from_dict_note|"""
        return _json_converter.structure(mapping_dict, Mapping)


NumericMetric = Union[bool, int, float]

AssociatedIdSets = frozenset[EquivalentIdSet]
"""A frozen set of :class:`.EquivalentIdSet`"""


@dataclass(frozen=True, eq=True)
class SynonymTerm:
    """A SynonymTerm is a container for a single normalised synonym, and is produced by
    an :class:`~.OntologyParser` implementation.

    It may be composed of multiple terms that normalise to the same
    unique string (e.g. "breast cancer" and "Breast Cancer"). The number
    of ``associated_id_sets`` that this synonym maps to is determined by the
    :meth:`~.OntologyParser.score_and_group_ids` method of the associated OntologyParser.
    """

    #: unnormalised synonym strings
    terms: frozenset[str]
    #: normalised form
    term_norm: str
    #: ontology parser name
    parser_name: str
    #: is the term symbolic? Determined by the OntologyParser
    is_symbolic: bool
    associated_id_sets: AssociatedIdSets
    #: aggregation strategy, determined by the ontology parser
    aggregated_by: EquivalentIdAggregationStrategy = field(hash=False, compare=False)
    #: mapping type metadata
    mapping_types: frozenset[str] = field(hash=False, default_factory=frozenset)

    @property
    def is_ambiguous(self) -> bool:
        return len(self.associated_id_sets) > 1

    @staticmethod
    def from_dict(term_dict: dict) -> "SynonymTerm":
        """|from_dict_note|"""
        return _json_converter.structure(term_dict, SynonymTerm)


@dataclass(frozen=True, eq=True)
class SynonymTermWithMetrics(SynonymTerm):
    """Similar to SynonymTerm, but also allows metrics to be scored.

    As these metrics are not used in the hash function, care should be taken when
    hashing of this object is required.
    """

    search_score: Optional[float] = field(compare=False, default=None)
    embed_score: Optional[float] = field(compare=False, default=None)
    bool_score: Optional[float] = field(compare=False, default=None)
    exact_match: Optional[bool] = field(compare=False, default=None)

    @staticmethod
    def from_synonym_term(
        term: SynonymTerm,
        search_score: Optional[float] = None,
        embed_score: Optional[float] = None,
        bool_score: Optional[float] = None,
        exact_match: Optional[bool] = None,
    ) -> "SynonymTermWithMetrics":

        return SynonymTermWithMetrics(
            search_score=search_score,
            embed_score=embed_score,
            bool_score=bool_score,
            exact_match=exact_match,
            **term.__dict__,
        )

    def merge_metrics(self, term: "SynonymTermWithMetrics") -> "SynonymTermWithMetrics":
        new_values = {
            k: v
            for k, v in term.__dict__.items()
            if k in {"search_score", "embed_score", "bool_score", "exact_match"} and v is not None
        }
        new_term = dataclasses.replace(self, **new_values)
        return new_term

    @staticmethod
    def from_dict(term_dict: dict) -> "SynonymTermWithMetrics":
        """|from_dict_note|"""
        return _json_converter.structure(term_dict, SynonymTermWithMetrics)


@dataclass
class Entity:
    """A :class:`kazu.data.data.Entity` is a container for information about a
    single entity detected within a :class:`kazu.data.data.Section`\\ .

    Within an :class:`kazu.data.data.Entity`, the most important fields are :attr:`.Entity.match` (the actual string detected),
    :attr:`.Entity.syn_term_to_synonym_terms`, a dict of :class:`kazu.data.data.SynonymTermWithMetrics` (candidates for knowledgebase hits)
    and :attr:`.Entity.mappings`, the final product of linked references to the underlying entity.
    """

    #: exact text representation
    match: str
    entity_class: str
    spans: frozenset[CharSpan]
    #: # namespace of the :class:`~.Step` that produced this instance
    namespace: str
    mention_confidence: MentionConfidence = MentionConfidence.HIGHLY_LIKELY
    mappings: set[Mapping] = field(default_factory=set)
    #: | generic metadata
    #: |
    #: | |metadata_s11n_warn|
    metadata: dict = field(default_factory=dict)
    start: int = field(init=False)
    end: int = field(init=False)
    match_norm: str = field(init=False)
    syn_term_to_synonym_terms: dict[SynonymTermWithMetrics, SynonymTermWithMetrics] = field(
        default_factory=dict
    )

    def update_terms(self, terms: Iterable[SynonymTermWithMetrics]) -> None:
        for term in terms:
            existing_term: Optional[SynonymTermWithMetrics] = self.syn_term_to_synonym_terms.get(
                term
            )
            if existing_term is not None:
                new_term = existing_term.merge_metrics(term)
                self.syn_term_to_synonym_terms[new_term] = new_term
            else:
                self.syn_term_to_synonym_terms[term] = term

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return id(self) == id(other)

    def calc_starts_and_ends(self) -> tuple[int, int]:
        earliest_start = inf
        latest_end = 0
        for span in self.spans:
            if span.start < earliest_start:
                earliest_start = span.start
            if span.end > latest_end:
                latest_end = span.end
        if earliest_start is inf:
            raise ValueError("spans are not valid")
        return int(earliest_start), latest_end

    def __post_init__(self):
        self.start, self.end = self.calc_starts_and_ends()
        self.match_norm = StringNormalizer.normalize(self.match, self.entity_class)

    def is_completely_overlapped(self, other: "Entity") -> bool:
        """True if all CharSpan instances are completely encompassed by all other
        CharSpan instances.

        :param other:
        :return:
        """
        for charspan in self.spans:
            for other_charspan in other.spans:
                if charspan.is_completely_overlapped(other_charspan):
                    break
            else:
                return False
        return True

    def is_partially_overlapped(self, other: "Entity") -> bool:
        """True if only one CharSpan instance is defined in both self and other, and
        they are partially overlapped.

        If multiple CharSpan are defined in both self and other, this becomes pathological, as while they may overlap
        in the technical sense, they may have distinct semantic meaning. For instance, consider the case where
        we may want to use is_partially_overlapped to select the longest annotation span suggested
        by some NER system.

        case 1:
        text: the patient has metastatic liver cancers
        entity1:  metastatic liver cancer -> [CharSpan(16,39]
        entity2: liver cancers -> [CharSpan(27,40]

        result: is_partially_overlapped -> True (entities are part of same concept)


        case 2: non-contiguous entities

        text: lung and liver cancer
        lung cancer -> [CharSpan(0,4), CharSpan(1521
        liver cancer -> [CharSpan(9,21)]

        result: is_partially_overlapped -> False (entities are distinct)

        :param other:
        :return:
        """
        if len(self.spans) == 1 and len(other.spans) == 1:
            (charspan,) = self.spans
            (other_charspan,) = other.spans
            return charspan.is_partially_overlapped(other_charspan)
        else:
            return False

    def __len__(self) -> int:
        """Span length.

        :return: number of characters enclosed by span
        """
        return self.end - self.start

    def __repr__(self) -> str:
        """Describe the tag.

        :return: tag match description
        """
        return f"{self.match}:{self.entity_class}:{self.namespace}:{self.start}:{self.end}"

    def as_brat(self) -> str:
        """
        :return: this entity in the third party biomedical nlp Brat format
            (see the `docs <https://brat.nlplab.org/introduction.html>`_\\ ,
            `paper <https://aclanthology.org/E12-2021.pdf>`_\\ , and
            `codebase <https://github.com/nlplab/brat>`_\\ )
        """
        # TODO: update this to make use of non-contiguous entities
        return f"{hash(self)}\t{self.entity_class}\t{self.start}\t{self.end}\t{self.match}\n"

    def add_mapping(self, mapping: Mapping) -> None:
        """Deprecated.

        :param mapping:
        :return:
        """
        self.mappings.add(mapping)

    @classmethod
    def from_spans(
        cls, spans: list[tuple[int, int]], text: str, join_str: str = "", **kwargs: Any
    ) -> "Entity":
        """Create an instance of Entity from a list of character indices. A text string
        of underlying doc is also required to produce a representative match.

        :param spans:
        :param text:
        :param join_str: a string used to join the spans together
        :param kwargs:
        :return:
        """
        char_spans = []
        text_pieces = []
        for start, end in spans:
            text_pieces.append(text[start:end])
            char_spans.append(CharSpan(start=start, end=end))
        return cls(spans=frozenset(char_spans), match=join_str.join(text_pieces), **kwargs)

    @classmethod
    def load_contiguous_entity(cls, start: int, end: int, **kwargs: Any) -> "Entity":
        single_span = frozenset([CharSpan(start=start, end=end)])
        return cls(spans=single_span, **kwargs)

    @staticmethod
    def from_dict(entity_dict: dict) -> "Entity":
        """|from_dict_note|"""
        return _json_converter.structure(entity_dict, Entity)


@dataclass(unsafe_hash=True)
class Section:
    """A container for text and entities.

    One or more make up a :class:`kazu.data.data.Document`.
    """

    #: the text to be processed
    text: str
    #: the name of the section (e.g. abstract, body, header, footer etc)
    name: str
    #: | generic metadata
    #: |
    #: | |metadata_s11n_warn|
    metadata: dict = field(default_factory=dict, hash=False)
    #: entities detected in this section
    entities: list[Entity] = field(default_factory=list, hash=False)
    _sentence_spans: Optional[tuple[CharSpan, ...]] = field(
        default=None, hash=False, init=False
    )  # hidden implem. to prevent overwriting existing sentence spans

    @property
    def sentence_spans(self) -> Iterable[CharSpan]:
        if self._sentence_spans is not None:
            return self._sentence_spans
        else:
            return ()

    @sentence_spans.setter
    def sentence_spans(self, sent_spans: Iterable[CharSpan]) -> None:
        """Setter for sentence_spans. sentence_spans are stored in the order provided by
        the iterable sent_spans param, which may not necessarily be in sorted order.

        :param sent_spans:
        :return:
        """
        if self._sentence_spans is None:
            sent_spans_tuple = tuple(sent_spans)
            assert len(sent_spans_tuple) == len(
                set(sent_spans_tuple)
            ), "There are duplicate sentence spans"
            self._sentence_spans = sent_spans_tuple
        else:
            raise AttributeError("Immutable sentence_spans is already set")

    def __str__(self):
        return f"name: {self.name}, text: {self.text[:100]}"

    @staticmethod
    def from_dict(section_dict: dict) -> "Section":
        """|from_dict_note|"""
        return _json_converter.structure(section_dict, Section)


@dataclass(unsafe_hash=True)
class Document:
    """A container that is the primary input into a
    :class:`kazu.pipeline.pipeline.Pipeline`."""

    #: a document identifier
    idx: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: sections comprising this document
    sections: list[Section] = field(default_factory=list, hash=False)
    #: | generic metadata
    #: |
    #: | |metadata_s11n_warn|
    metadata: dict = field(default_factory=dict, hash=False)

    def __str__(self):
        return f"idx: {self.idx}"

    def get_entities(self) -> list[Entity]:
        """Get all entities in this document."""
        entities = []
        for section in self.sections:
            entities.extend(section.entities)
        return entities

    def to_json(
        self,
        drop_unmapped_ents: bool = False,
        drop_terms: bool = False,
        **kwargs: Any,
    ) -> str:
        """Convert to json string.

        The two specified arguments are passed through to :meth:`~.as_minified_dict`,
        kwargs is passed through to :func:`json.dumps`.

        :param drop_unmapped_ents: drop any entities that have no mappings
        :param drop_terms: drop the synonym term dict field
        :param kwargs: additional kwargs passed to :func:`json.dumps`.
        :return:
        """
        as_dict = self.as_minified_dict(
            drop_unmapped_ents=drop_unmapped_ents, drop_terms=drop_terms
        )
        return json.dumps(as_dict, **kwargs)

    def as_minified_dict(self, drop_unmapped_ents: bool = False, drop_terms: bool = False) -> dict:
        """Convert the Document to a ``dict`` and 'minify'.

        This function 'minifies' in a couple of ways:

        1. Providing arguments that allow you to drop some parts of the Document that may be irrelevant to you.
        2. General 'minification' by removing 'empty' parts of the ``dict``.

        :param drop_unmapped_ents: drop any entities that have no mappings
        :param drop_terms: drop the synonym term dict field
        :return:
        """
        as_dict: dict = _json_converter.unstructure(self)
        return DocumentJsonUtils.minify_json_dict(
            as_dict, drop_unmapped_ents=drop_unmapped_ents, drop_terms=drop_terms
        )

    @classmethod
    def create_simple_document(cls, text: str) -> "Document":
        """Create an instance of :class:`.Document` from a text string.

        :param text:
        :return:
        """
        return cls(sections=[Section(text=text, name="na")])

    @classmethod
    def simple_document_from_sents(cls, sents: list[str]) -> "Document":
        section = Section(text=" ".join(sents), name="na")
        sent_spans = []
        curr_start = 0
        for sent in sents:
            sent_spans.append(CharSpan(start=curr_start, end=curr_start + len(sent)))
            curr_start += len(sent) + 1  # + 1 is for the joining space
        section.sentence_spans = sent_spans
        return cls(sections=[section])

    @classmethod
    def from_named_section_texts(cls, named_sections: dict[str, str]) -> "Document":
        sections = [Section(text=text, name=name) for name, text in named_sections.items()]
        return cls(sections=sections)

    def __len__(self):
        return sum(len(section.text) for section in self.sections)

    @staticmethod
    def from_dict(document_dict: dict) -> "Document":
        """|from_dict_note|"""
        return _json_converter.structure(document_dict, Document)

    @staticmethod
    def from_json(json_str: str) -> "Document":
        return Document.from_dict(json.loads(json_str))


_json_converter = cattrs.preconf.json.make_converter(omit_if_default=True)

# 'external' to kazu datatypes
_json_converter.register_unstructure_hook(float16, lambda v: v.item())
_json_converter.register_structure_hook(float16, lambda v, _: float16(v))
_json_converter.register_unstructure_hook(float32, lambda v: v.item())
_json_converter.register_structure_hook(float32, lambda v, _: float16(v))
_json_converter.register_unstructure_hook(ndarray, lambda v: v.tolist())
# note: this could result in a change in the dtype (e.g. float vs int and precision)
# from the 'original' if roundtripped. This doesn't seem like a deal breaker for
# what we're using it for.
_json_converter.register_structure_hook(ndarray, lambda v, _: ndarray(v))
_json_converter.register_unstructure_hook(bson.ObjectId, lambda v: json_util.default(v))
_json_converter.register_structure_hook(bson.ObjectId, lambda v, _: json_util.object_hook(v))

_json_converter.register_structure_hook(MentionConfidence, lambda v, _: MentionConfidence[v])
_json_converter.register_unstructure_hook(MentionConfidence, lambda v: v.name)

_json_converter.register_unstructure_hook(
    Entity,
    cattrs.gen.make_dict_unstructure_fn(
        Entity,
        _json_converter,
        syn_term_to_synonym_terms=cattrs.gen.override(
            rename="synonym_terms",
            unstruct_hook=lambda v: [_json_converter.unstructure(x) for x in v.values()],
        ),
        _cattrs_include_init_false=True,
        _cattrs_omit_if_default=True,
    ),
)

_json_converter.register_unstructure_hook(
    Section,
    cattrs.gen.make_dict_unstructure_fn(
        Section,
        _json_converter,
        _sentence_spans=cattrs.gen.override(rename="sentence_spans", omit_if_default=True),
        _cattrs_include_init_false=True,
        _cattrs_omit_if_default=True,
    ),
)


def _syn_term_to_synonym_terms_struct_hook(
    synonym_terms: list[dict[str, JsonEncodable]], _: type
) -> dict[SynonymTermWithMetrics, SynonymTermWithMetrics]:
    syn_terms = (_json_converter.structure(x, SynonymTermWithMetrics) for x in synonym_terms)
    return {st: st for st in syn_terms}


_json_converter.register_structure_hook(
    Entity,
    cattrs.gen.make_dict_structure_fn(
        Entity,
        _json_converter,
        syn_term_to_synonym_terms=cattrs.gen.override(
            rename="synonym_terms", struct_hook=_syn_term_to_synonym_terms_struct_hook
        ),
    ),
)

_json_converter.register_structure_hook(
    Section,
    cattrs.gen.make_dict_structure_fn(
        Section,
        _json_converter,
        _sentence_spans=cattrs.gen.override(
            rename="sentence_spans",
            # needed because cattrs by default ignores init=False fields
            omit=False,
            struct_hook=lambda v, _: (
                None if len(v) == 0 else tuple(_json_converter.structure(x, CharSpan) for x in v)
            ),
        ),
    ),
)


class DocumentJsonUtils:
    @staticmethod
    def minify_json_dict(
        doc_json_dict: dict[str, Any],
        drop_unmapped_ents: bool = False,
        drop_terms: bool = False,
        in_place: bool = True,
    ) -> dict:
        doc_json_dict = doc_json_dict if in_place else deepcopy(doc_json_dict)

        if drop_unmapped_ents or drop_terms:
            for section_dict in doc_json_dict["sections"]:
                section_entities = section_dict["entities"]
                ents_to_keep = list(
                    filter(lambda _ent: _ent["mappings"], section_entities)
                    if drop_unmapped_ents
                    else section_entities
                )
                if drop_terms:
                    for ent in ents_to_keep:
                        ent["synonym_terms"].clear()

                section_dict["entities"] = ents_to_keep

        return doc_json_dict


SimpleValue = Union[NumericMetric, str]


class CuratedTermBehaviour(AutoNameEnum):
    #: use the term for both dictionary based NER and as a linking target.
    ADD_FOR_NER_AND_LINKING = auto()
    #: use the term only as a linking target. Note, this is not required if the term is already in the
    #: underlying ontology, as all ontology terms are included as linking targets by default
    #: (Also see DROP_SYNONYM_TERM_FOR_LINKING)
    ADD_FOR_LINKING_ONLY = auto()
    #: do not use this term as a linking target. Normally, you would use this for a term you want to remove
    #: from the underlying ontology (e.g. a 'bad' synonym). If the term does not exist, has no effect
    DROP_SYNONYM_TERM_FOR_LINKING = auto()


class ParserBehaviour(AutoNameEnum):
    #: completely remove the ids from the parser - i.e. should never used anywhere
    DROP_IDS_FROM_PARSER = auto()


@dataclass(frozen=True)
class ParserAction:
    """A ParserAction changes the behaviour of a
    :class:`kazu.ontology_preprocessing.base.OntologyParser` in a global sense.

    A ParserAction overrides any default behaviour of the parser, and also any conflicts that may occur with
    :class:`.CuratedTerm`\\s.

    These actions are useful for eliminating unwanted behaviour. For example, the root of the Mondo
    ontology is http://purl.obolibrary.org/obo/HP_0000001, which has a default label of 'All'. Since this is
    such a common word, and not very useful in terms of linking, we might want a global action so that this
    ID is not used anywhere in a Kazu pipeline.

    The parser_to_target_id_mappings field should specify the parser name and an affected IDs if required.
    See :class:`.ParserBehaviour` for the type of actions that are possible.
    """

    behaviour: ParserBehaviour
    parser_to_target_id_mappings: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, json_dict: dict) -> "ParserAction":
        return _json_converter.structure(json_dict, ParserAction)

    def __post_init__(self):
        if len(self.parser_to_target_id_mappings) == 0:
            raise ValueError(
                f"parser_to_target_id_mappings must be specified for global actions: {self}"
            )
        for key, values in self.parser_to_target_id_mappings.items():
            if len(values) == 0:
                raise ValueError(f"at least one ID must be specified for key: {key}, {self}")


@dataclass(frozen=True)
class GlobalParserActions:
    """Container for all :class:`.ParserAction`\\s."""

    actions: list[ParserAction]
    _parser_name_to_action: defaultdict[str, list[ParserAction]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )

    def __post_init__(self):
        for action in self.actions:
            for parser_name in action.parser_to_target_id_mappings.keys():
                self._parser_name_to_action[parser_name].append(action)

    def parser_behaviour(self, parser_name: str) -> Iterable[ParserAction]:
        """Generator that yields behaviours for a specific parser, based on the order
        they are specified in.

        :param parser_name:
        :return:
        """
        yield from self._parser_name_to_action.get(parser_name, [])

    @classmethod
    def from_dict(cls, json_dict: dict) -> "GlobalParserActions":
        return _json_converter.structure(json_dict, GlobalParserActions)


@dataclass(frozen=True)
class MentionForm:
    string: str
    case_sensitive: bool
    mention_confidence: MentionConfidence


@dataclass(frozen=True)
class CuratedTerm:
    """A CuratedTerm represents the behaviour of a specific :class:`.SynonymTerm` within
    an Ontology.

    For each SynonymTerm, a default CuratedTerm is produced with its
    behaviour determined by an instance of
    :class:`kazu.ontology_preprocessing.autocuration.AutoCurator` and the
    :class:`kazu.ontology_preprocessing.curation_utils.CuratedTermConflictAnalyser`\\.

    .. note::

       This is typically handled by the internals of :class:`kazu.ontology_preprocessing.base.OntologyParser`\\.
       However, CuratedTerms can also be used to override the default behaviour of a parser. See :ref:`ontology_parser`
       for a more detailed guide.

    The configuration of a CuratedTerm will affect both NER and Linking aspects of Kazu:

    Example 1:

    The string 'ALL' is highly ambiguous. It might mean several diseases, or simply 'all'. Therefore, we want
    to add a curation as follows, so that it will only be used as a linking target and not for dictionary based NER:

    .. code-block:: python

        CuratedTerm(
            original_forms=frozenset(
                [
                    MentionForm(
                        string="ALL",
                        mention_confidence=MentionConfidence.POSSIBLE,
                        case_sensitive=True,
                    )
                ]
            ),
            behaviour=CuratedTermBehaviour.ADD_FOR_LINKING_ONLY,
        )


    Example 2:

    The string 'LH' is incorrectly identified as a synonym of the PLOD1 (ENSG00000083444) gene, whereas more often than not, it's actually an abbreviation of Lutenising Hormone.
    We therefore want to override the associated_id_sets to LHB (ENSG00000104826, or Lutenising Hormone Subunit Beta)

    The CuratedTerm we therefore want is:

    .. code-block:: python

        CuratedTerm(
            original_forms=frozenset(
                [
                    MentionForm(
                        string="LH",
                        mention_confidence=MentionConfidence.POSSIBLE,
                        case_sensitive=True,
                    )
                ]
            ),
            associated_id_sets=frozenset((EquivalentIdSet(("ENSG00000104826", "ENSEMBL")),)),
            behaviour=CuratedTermBehaviour.ADD_FOR_LINKING_ONLY,
        )

    Example 3:

    A :class:`.SynonymTerm` has an alternative synonym not referenced in the underlying ontology, and we want to add it.

    .. code-block:: python

        CuratedTerm(
            original_forms=frozenset(
                [
                    MentionForm(
                        string="breast carcinoma",
                        mention_confidence=MentionConfidence.POSSIBLE,
                        case_sensitive=True,
                    )
                ]
            ),
            associated_id_sets=frozenset((EquivalentIdSet(("ENSG00000104826", "ENSEMBL")),)),
            behaviour=CuratedTermBehaviour.ADD_FOR_NER_AND_LINKING,
        )
    """

    #: Original versions of this term, exactly as specified in the source ontology. These should all normalise to the same string.
    original_forms: frozenset[MentionForm]
    #: The intended behaviour for this term.
    behaviour: CuratedTermBehaviour
    #: Alternative forms of the original versions of this term created by :class:`kazu.ontology_preprocessing.synonym_generation.CombinatorialSynonymGenerator`\.
    alternative_forms: frozenset[MentionForm] = field(default_factory=frozenset)
    #: If specified, will override the parser defaults for the associated :class:`.SynonymTerm`\, as long as conflicts do not occur
    associated_id_sets: Optional[AssociatedIdSets] = None
    _id: bson.ObjectId = field(default_factory=bson.ObjectId, compare=False)
    #: results of any decisions by the :class:`kazu.ontology_preprocessing.autocuration.AutoCurator`
    autocuration_results: Optional[dict[str, str]] = field(default=None, compare=False)
    #: human readable comments about this curation decision
    comment: Optional[str] = field(default=None, compare=False)

    def __post_init__(self):
        cs_forms = defaultdict(set)
        ci_forms = defaultdict(set)
        if len(self.original_forms) == 0:
            raise ValueError(f"no forms for: {self}")
        for form in self.active_ner_forms():
            if form.case_sensitive:
                cs_forms[form.string].add(form.mention_confidence)
            else:
                ci_forms[form.string.lower()].add(form.mention_confidence)
        for cs_form, cs_confidences in ci_forms.items():
            ci_confidences = ci_forms.get(cs_form.lower(), {MentionConfidence.POSSIBLE})
            if min(ci_confidences) < min(cs_confidences):
                raise RuntimeError(f"case sensitive conflict: {self}")

    def term_norm_for_linking(self, entity_class: str) -> str:
        norms = set(
            StringNormalizer.normalize(form.string, entity_class) for form in self.original_forms
        )
        if len(norms) == 1:
            term_norm = next(iter(norms))
            return term_norm
        else:
            raise RuntimeError(
                f"multiple term norms produced by {self}. This curation should be seperated into two or more seperate items."
            )

    @staticmethod
    def from_json(json_str: str) -> "CuratedTerm":
        json_dict = json.loads(json_str)
        return CuratedTerm.from_dict(json_dict)

    @classmethod
    def from_dict(cls, json_dict: dict) -> "CuratedTerm":
        """|from_dict_note|"""
        return _json_converter.structure(json_dict, CuratedTerm)

    def to_dict(self, preserve_structured_object_id: bool = True) -> dict[str, Any]:
        as_dict: dict[str, Any] = _json_converter.unstructure(self)
        if preserve_structured_object_id:
            as_dict["_id"] = self._id
        return as_dict

    def to_json(self) -> str:
        as_json = self.to_dict(False)
        assert isinstance(as_json, dict)
        return json.dumps(as_json)

    @property
    def additional_to_source(self) -> bool:
        """True if this term created in addition to the source terms defined in the
        original Ontology."""
        return self.associated_id_sets is not None and self.behaviour in {
            CuratedTermBehaviour.ADD_FOR_NER_AND_LINKING,
            CuratedTermBehaviour.ADD_FOR_LINKING_ONLY,
        }

    def all_forms(self) -> Iterable[MentionForm]:
        for form in self.original_forms.union(self.alternative_forms):
            yield form

    def all_strings(self) -> Iterable[str]:
        for form in self.all_forms():
            yield form.string

    def active_ner_forms(self) -> Iterable[MentionForm]:
        if self.behaviour is CuratedTermBehaviour.ADD_FOR_NER_AND_LINKING:
            for form in self.original_forms.union(self.alternative_forms):
                if form.mention_confidence is not MentionConfidence.IGNORE:
                    yield form


class KazuConfigurationError(Exception):
    pass
