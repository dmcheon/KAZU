import itertools
import traceback
from typing import List, Tuple, Optional, Protocol, Callable, Iterable, TypeVar
from functools import reduce
from kazu.data.data import Document, Entity, PROCESSING_EXCEPTION
from kazu.steps import BaseStep

EntityFilterFn = Callable[[Entity], bool]
T = TypeVar("T")


class CleanupAction(Protocol):
    def cleanup(self, doc: Document):
        raise NotImplementedError


class EntityFilterFnProvider(Protocol):
    def filter_fns(self) -> List[EntityFilterFn]:
        raise NotImplementedError


class EntityFilterCleanupAction:
    def __init__(self, filter_fns: List[EntityFilterFn]):
        self.filter_fns = filter_fns

    @staticmethod
    def from_filter_fn_providers(
        filter_providers: List[EntityFilterFnProvider],
    ) -> "EntityFilterCleanupAction":
        filter_fns = list(
            itertools.chain.from_iterable(
                (filter_provider.filter_fns() for filter_provider in filter_providers)
            )
        )
        return EntityFilterCleanupAction(filter_fns)

    def cleanup(self, doc: Document):
        for section in doc.sections:
            section_ents = set(section.entities)

            filter_combiner: Callable[
                [Iterable[Entity], EntityFilterFn], Iterable[Entity]
            ] = lambda _filtered_ents, filter_fn: filter(filter_fn, _filtered_ents)
            filtered_ents = set(reduce(filter_combiner, self.filter_fns, section_ents))

            section_ents.difference_update(filtered_ents)
            section.entities = list(section_ents)


class DropUnmappedEntsEntityFilters:
    def __init__(self, from_ent_namespaces: List[str]):
        self.from_ent_namespaces = set(from_ent_namespaces)

    def filter_fns(self) -> List[EntityFilterFn]:
        return [
            (lambda ent: ent.namespace == ns and len(ent.mappings) == 0)
            for ns in self.from_ent_namespaces
        ]


class CleanupStep(BaseStep):
    def __init__(self, depends_on: Optional[List[str]], cleanup_actions: List[CleanupAction]):
        super().__init__(depends_on=depends_on)
        self.cleanup_actions = cleanup_actions

    def _run(self, docs: List[Document]) -> Tuple[List[Document], List[Document]]:
        failed_docs = []
        for doc in docs:
            try:
                for cleanup_action in self.cleanup_actions:
                    cleanup_action.cleanup(doc)
            except Exception:
                doc.metadata[PROCESSING_EXCEPTION] = traceback.format_exc()
                failed_docs.append(doc)

        return docs, failed_docs
