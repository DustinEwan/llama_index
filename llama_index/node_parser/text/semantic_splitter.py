from typing import Any, Callable, List, Optional, Sequence, TypedDict

import numpy as np

from llama_index.bridge.pydantic import Field
from llama_index.callbacks.base import CallbackManager
from llama_index.embeddings.base import BaseEmbedding
from llama_index.node_parser import NodeParser
from llama_index.node_parser.interface import NodeParser
from llama_index.node_parser.node_utils import build_nodes_from_splits
from llama_index.node_parser.text.utils import split_by_sentence_tokenizer
from llama_index.schema import BaseNode, Document
from llama_index.utils import get_tqdm_iterable

DEFAULT_OG_TEXT_METADATA_KEY = "original_text"


class SentenceCombination(TypedDict):
    sentence: str
    index: int
    combined_sentence: str
    combined_sentence_embedding: List[float]
    distance_to_next: float


class SemanticSplitterNodeParser(NodeParser):
    """Semantic node parser.

    Splits a document into Nodes, with each node being a group of semantically related sentences.

    Args:
        embedding: (BaseEmbedding): embedding model to use
        sentence_splitter (Optional[Callable]): splits text into sentences
        include_metadata (bool): whether to include metadata in nodes
        include_prev_next_rel (bool): whether to include prev/next relationships
    """

    sentence_splitter: Callable[[str], List[str]] = Field(
        default_factory=split_by_sentence_tokenizer,
        description="The text splitter to use when splitting documents.",
        exclude=True,
    )

    embedding: BaseEmbedding = Field(
        description="The embedding model to use to for semantic comparison",
    )

    window_size: int = Field(
        description="The number of sentences to group together when evaluating semantic similarity.  Set to 1 to consider each sentence individually.  Set to >1 to group sentences together."
    )

    breakpoint_percentile_threshold = Field(
        default=95,
        description="The percentile of cosine dissimilarity that must be exceeded between a group of sentences and the next to form a node.  The smaller this number is, the more nodes will be generated",
    )

    @classmethod
    def class_name(cls) -> str:
        return "SemanticSplitterNodeParser"

    @classmethod
    def from_defaults(
        cls,
        embedding: BaseEmbedding,
        breakpoint_percentile_threshold: int = 95,
        window_size: int = 1,
        sentence_splitter: Optional[Callable[[str], List[str]]] = None,
        original_text_metadata_key: str = DEFAULT_OG_TEXT_METADATA_KEY,
        include_metadata: bool = True,
        include_prev_next_rel: bool = True,
        callback_manager: Optional[CallbackManager] = None,
    ) -> "SemanticSplitterNodeParser":
        callback_manager = callback_manager or CallbackManager([])

        sentence_splitter = sentence_splitter or split_by_sentence_tokenizer()

        return cls(
            embedding=embedding,
            breakpoint_percentile_threshold=breakpoint_percentile_threshold,
            window_size=window_size,
            sentence_splitter=sentence_splitter,
            original_text_metadata_key=original_text_metadata_key,
            include_metadata=include_metadata,
            include_prev_next_rel=include_prev_next_rel,
            callback_manager=callback_manager,
        )

    def _parse_nodes(
        self,
        nodes: Sequence[BaseNode],
        show_progress: bool = False,
        **kwargs: Any,
    ) -> List[BaseNode]:
        """Parse document into nodes."""
        all_nodes: List[BaseNode] = []
        nodes_with_progress = get_tqdm_iterable(nodes, show_progress, "Parsing nodes")

        for node in nodes_with_progress:
            nodes = self.build_semantic_nodes_from_documents([node], show_progress)
            all_nodes.extend(nodes)

        return all_nodes

    def build_semantic_nodes_from_documents(
        self,
        documents: Sequence[Document],
        show_progress: bool = False,
    ) -> List[BaseNode]:
        """Build window nodes from documents."""
        all_nodes: List[BaseNode] = []
        for doc in documents:
            text = doc.text
            text_splits = self.sentence_splitter(text)

            sentences: List[SentenceCombination] = [
                {
                    "sentence": x,
                    "index": i,
                    "combined_sentence": "",
                    "combined_sentence_embedding": [],
                    "distance_to_next": 0,
                }
                for i, x in enumerate(text_splits)
            ]

            # Group sentences and calculate embeddings for sentence groups
            for i in range(len(sentences)):
                combined_sentence = ""

                for j in range(i - self.window_size, i):
                    if j >= 0:
                        combined_sentence += sentences[j]["sentence"] + " "

                combined_sentence += sentences[i]["sentence"]

                for j in range(i + 1, i + 1 + self.window_size):
                    if j < len(sentences):
                        combined_sentence += " " + sentences[j]["sentence"]

                sentences[i]["combined_sentence"] = combined_sentence

            combined_sentence_embeddings = self.embedding.get_text_embedding_batch(
                [s["combined_sentence"] for s in sentences],
                show_progress=show_progress,
            )

            for i, embedding in enumerate(combined_sentence_embeddings):
                sentences[i]["combined_sentence_embedding"] = embedding

            # Calculate similarity between sentence groups
            distances = []
            for i in range(len(sentences) - 1):
                embedding_current = sentences[i]["combined_sentence_embedding"]
                embedding_next = sentences[i + 1]["combined_sentence_embedding"]

                similarity = self.embedding.similarity(
                    embedding_current, embedding_next
                )

                distance = 1 - similarity

                distances.append(distance)
                sentences[i]["distance_to_next"] = distance

            chunks = []
            if len(distances) > 0:
                breakpoint_distance_threshold = np.percentile(
                    distances, self.breakpoint_percentile_threshold
                )

                indices_above_threshold = [
                    i
                    for i, x in enumerate(distances)
                    if x > breakpoint_distance_threshold
                ]

                # Chunk sentences into semantic groups based on percentile breakpoints
                start_index = 0

                for index in indices_above_threshold:
                    end_index = index - 1

                    group = sentences[start_index : end_index + 1]
                    combined_text = "  ".join([d["sentence"] for d in group])
                    chunks.append(combined_text)

                    start_index = index

                if start_index < len(sentences):
                    combined_text = "  ".join(
                        [d["sentence"] for d in sentences[start_index:]]
                    )
                    chunks.append(combined_text)
            else:
                chunks = [" ".join(text_splits)]

            nodes = build_nodes_from_splits(
                chunks,
                doc,
                id_func=self.id_func,
            )

            all_nodes.extend(nodes)

        return all_nodes
