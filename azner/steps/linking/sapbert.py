import logging
from typing import List, Tuple

import numpy as np
import pandas as pd
import torch
from azner.data.data import Document, Entity, Mapping
from azner.data.pytorch import HFDataset
from azner.modelling.hf_lightning_wrappers import PLAutoModel
from azner.steps import BaseStep
from cachetools import LFUCache
from pytorch_lightning import Trainer
from scipy.spatial.distance import cdist
from steps.utils.utils import (
    documents_to_entity_list,
    filter_entities_with_kb_mappings,
    get_match_entity_class_hash,
    update_mappings,
)
from torch.utils.data import DataLoader
from transformers import (
    AutoConfig,
    AutoTokenizer,
    DataCollatorWithPadding,
    AutoModel,
)
from transformers.file_utils import PaddingStrategy

logger = logging.getLogger(__name__)


class SapBertForEntityLinkingStep(BaseStep):
    def __init__(
        self,
        depends_on: List[str],
        model_path: str,
        knowledgebase_path: str,
        batch_size: int,
        process_all_entities: bool = False,
        rebuild_kb_cache: bool = False,
        lookup_cache_size: int = 5000,
    ):
        """
        This step wraps Sapbert: Self Alignment pretraining for biomedical entity representation.

        Note, the knowledgebase to link against is held in memory as an ndarray. For very large KB's we should use
        faiss (see Nemo example via SapBert github reference)

        Original paper
        https://aclanthology.org/2021.naacl-main.334.pdf

        :param model_path: path to HF SAPBERT model, config and tokenizer. A good pretrained default is available at
                            https://huggingface.co/cambridgeltl/SapBERT-from-PubMedBERT-fulltext
                            This is passed to HF Automodel.from_pretrained()
        :param depends_on: namespaces of dependency stes
        :param knowledgebase_path: path to parquet of labels to map to. This should have two columns: 'iri' and
                            'default_label'. The default_label will be used to create an embedding, and the iri will
                            be used to create a Mapping for the entity. See SapBert paper for further info
        :param batch_size: batch size for dataloader
        :param process_all_entities: bool flag. Since SapBert involves expensive bert calls, this flag controls whether
                                        it should be used on all entities, or only entities that have no mappings (i.e.
                                        entites that have already been linked via a less expensive method, such as
                                        dictionary lookup). This flag check for the presence of at least one entry in
                                        Entity.metadata.mappings
        :param rebuild_kb_cache: should the kb embedding cache be rebuilt?
        :param lookup_cache_size: this step maintains a cache of {hash(Entity.match,Entity.entity_class):Mapping}, to reduce bert calls. This dictates the size
        """
        super().__init__(depends_on=depends_on)
        self.rebuild_cache = rebuild_kb_cache
        self.process_all_entities = process_all_entities
        self.batch_size = batch_size
        self.config = AutoConfig.from_pretrained(model_path)
        self.tokeniser = AutoTokenizer.from_pretrained(model_path, config=self.config)
        self.knowledgebase_path = knowledgebase_path
        self.model = AutoModel.from_pretrained(model_path, config=self.config)
        self.model = PLAutoModel(self.model)
        self.trainer = Trainer()
        self.kb_ids, self.kb_embeddings = self.get_or_create_kb_embedding_cache()
        self.lookup_cache = LFUCache(lookup_cache_size)

    def get_or_create_kb_embedding_cache(self):
        if self.rebuild_cache:
            logger.info("forcing a rebuild of the kb cache")
            return self.predict_kb_embeddings()
        else:
            self.load_kb_cache()

    def load_kb_cache(self):
        raise NotImplementedError("kb caching currently not implemented")

    def predict_kb_embeddings(self) -> Tuple[List[str], np.ndarray]:
        """
        based on the value of self.knowledgebase_path, this returns a Tuple[List[str],np.ndarray]. The strings are the
        iri's, and the ndarray are the embeddings to be queried against
        :return:
        """

        df = pd.read_parquet(self.knowledgebase_path)
        logger.info(f"read {df.shape[0]} rows from kb")
        df.columns = ["iri", "default_label"]

        default_labels = df["default_label"].tolist()
        batch_encodings = self.tokeniser(default_labels)
        dataset = HFDataset(batch_encodings)
        collate_func = DataCollatorWithPadding(
            tokenizer=self.tokeniser, padding=PaddingStrategy.LONGEST
        )
        loader = DataLoader(dataset=dataset, batch_size=self.batch_size, collate_fn=collate_func)

        results = self.trainer.predict(
            model=self.model, dataloaders=loader, return_predictions=True
        )
        results = torch.cat([x.pooler_output for x in results]).cpu().detach().numpy()
        logger.info("knowledgebase embedding generation successful")
        return df["iri"].tolist(), results

    def get_dataloader_for_entities(self, entities: List[Entity]) -> DataLoader:
        """
        get a dataloader and entity list from a List of Document. Collation is handled via DataCollatorWithPadding
        :param docs:
        :return:
        """

        batch_encoding = self.tokeniser([x.match for x in entities])
        dataset = HFDataset(batch_encoding)
        collate_func = DataCollatorWithPadding(
            tokenizer=self.tokeniser, padding=PaddingStrategy.LONGEST
        )
        loader = DataLoader(dataset=dataset, batch_size=self.batch_size, collate_fn=collate_func)
        return loader

    def _run(self, docs: List[Document]) -> Tuple[List[Document], List[Document]]:
        """
        logic of entity linker:

        1) first obtain a dataloader and entity list from all docs
        2) generate embeddings for the entities based on the value of Entity.match
        3) query this embedding against self.kb_embeddings to detest the best match based on cosine distance
        4) generate a new Mapping with the queried iri, and update the entity information
        :param docs:
        :return:
        """
        entities = documents_to_entity_list(docs)
        if not self.process_all_entities:
            entities = filter_entities_with_kb_mappings(entities)

        entities = self.check_lookup_cache(entities)
        if len(entities) > 0:
            loader = self.get_dataloader_for_entities(entities)
            results = self.trainer.predict(
                model=self.model, dataloaders=loader, return_predictions=True
            )
            results = (
                torch.unsqueeze(torch.cat([x.pooler_output for x in results]), 1)
                .cpu()
                .detach()
                .numpy()
            )

            for i, result in enumerate(results):
                dist = cdist(result, self.kb_embeddings)
                nn_index = np.argmin(dist)
                entity = entities[i]
                new_mapping = Mapping(source="x", idx=self.kb_ids[nn_index], mapping_type="direct")
                update_mappings(entity, new_mapping)
                self.update_lookup_cache(entity, new_mapping)
        return docs, []

    def update_lookup_cache(self, entity: Entity, mapping: Mapping):
        hash_val = get_match_entity_class_hash(entity)
        if hash_val not in self.lookup_cache:
            self.lookup_cache[hash_val] = mapping

    def check_lookup_cache(self, entities: List[Entity]) -> List[Entity]:
        cache_misses = []
        for ent in entities:
            hash_val = get_match_entity_class_hash(ent)
            maybe_mapping = self.lookup_cache.get(hash_val, None)
            if maybe_mapping is None:
                cache_misses.append(ent)
            else:
                update_mappings(ent, maybe_mapping)
        return cache_misses
