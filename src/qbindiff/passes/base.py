import numpy as np
import scipy.spatial
from abc import ABCMeta, abstractmethod
from typing import Any, Optional, Iterable

from qbindiff.loader import Program
from qbindiff.features.extractor import FeatureExtractor, FeatureCollector
from qbindiff.visitor import ProgramVisitor
from qbindiff.types import SimMatrix


class GenericPass(metaclass=ABCMeta):
    """Class to define a interface to Passes"""

    @abstractmethod
    def __call__(
        self,
        sim_matrix: SimMatrix,
        primary: Program,
        secondary: Program,
        primary_mapping: dict,
        secondary_mapping: dict,
    ) -> None:
        """Execute the pass that operates on the similarity matrix inplace"""
        raise NotImplementedError()


class FeaturePass(GenericPass):
    """
    Run all the feature extractors previously registered and compute the similarity
    matrix
    """

    def __init__(self, distance: str):
        self.distance = distance
        self._visitor = ProgramVisitor()

    def register_extractor(self, extractor: FeatureExtractor):
        """
        Register a feature extractor.
        The class will be called when the visitor will traverse the graph.
        """
        self._visitor.register_feature_extractor(extractor)

    def _create_feature_matrix(
        self,
        features: dict[Any, FeatureCollector],
        features_keys: dict[str, Iterable[str]],
        node_to_index: dict[Any, int],
        dim: int,
        dtype: type,
    ):
        """
        Utility function to generate the feature matrix.
        It returns a tuple with (feature_matrix, mapping, nonempty_set) where
          feature_matrix: is the actual feature matrix, each row corresponds to a
                          node and each column to a feature
          mapping: a dict representing the mapping between the nodes index in the
                   adjacency matrix and in the similarity matrix.
                   {adj_index : sim_index}
          nonempty_set: set with all the node index (index in the adjacency matrix)
                        that have been added to the feature matrix (aka nodes with
                        non empty feature vector)

        :param features: Dict of features {node : feature_collector}
        :param features_keys: List of all the features keys
        :param node_to_index: Dict representing the mapping between nodes to indexes in
                              the similarity matrix. {node : sim_index}
        :param dim: Size of the feature matrix
        :param dtype: dtype of the feature matrix
        """

        feature_matrix = np.zeros((0, dim), dtype=dtype)
        mapping = {}
        nonempty_set = set()
        for i, (node_label, feature) in enumerate(features.items()):
            node_index = node_to_index[node_label]
            mapping[node_index] = i
            vec = feature.to_vector(features_keys, False)
            if vec:
                feature_matrix = np.vstack((feature_matrix, vec))
                nonempty_set.add(node_index)
        return (feature_matrix, mapping, nonempty_set)

    def __call__(
        self,
        sim_matrix: SimMatrix,
        primary: Program,
        secondary: Program,
        primary_mapping: dict,
        secondary_mapping: dict,
        fill: Optional[bool] = False,
    ) -> None:
        """
        Generate the similarity matrix by calculating the distance between the feature
        vectors.

        :param fill: if True the whole matrix will be erased before writing in it
        """

        # fill the matrix with zeros
        if fill:
            sim_matrix[:] = 0

        # Extract the features
        key_fun = lambda *args: args[0][0]  # ((label, node) iteration)
        primary_features = self._visitor.visit(primary, key_fun=key_fun)
        secondary_features = self._visitor.visit(secondary, key_fun=key_fun)
        primary_dim = len(primary_features)
        secondary_dim = len(secondary_features)

        # Get the weights of each feature
        f_weights = {}
        for extractor in self._visitor.feature_extractors:
            f_weights[extractor.key] = extractor.weight

        # Get all the keys and subkeys of the features
        # features_keys is a dict: {main_key: set(subkeys), ...}
        features_keys = {}
        for features in (primary_features, secondary_features):
            for f_collector in features.values():
                for main_key, subkey_list in f_collector.full_keys().items():
                    features_keys.setdefault(main_key, set())
                    if subkey_list:
                        features_keys[main_key].update(subkey_list)

        # Build the weights vector
        weights = []
        for main_key, subkey_list in features_keys.items():
            if subkey_list:
                dim = len(subkey_list)
                weights.extend(f_weights[main_key] / dim for _ in range(dim))
            else:
                weights.append(f_weights[main_key])

        # Build the feature matrix
        dim = len(weights)
        dtype = sim_matrix.dtype
        (
            primary_feature_matrix,  # the feature matrix
            temp_map_primary,  # temporary mappings between the nodes index in the adjacency matrix and in the similarity matrix
            nonempty_rows,  # non empty rows that will be kept after the distance is calculated
        ) = self._create_feature_matrix(
            primary_features, features_keys, primary_mapping, dim, dtype
        )
        (
            secondary_feature_matrix,
            temp_map_secondary,
            nonempty_cols,
        ) = self._create_feature_matrix(
            secondary_features, features_keys, secondary_mapping, dim, dtype
        )

        # Generate the partial similarity matrix (only non empty rows and cols)
        tmp_sim_matrix = scipy.spatial.distance.cdist(
            primary_feature_matrix, secondary_feature_matrix, self.distance, w=weights
        ).astype(sim_matrix.dtype)

        # Normalize
        if len(tmp_sim_matrix) > 0 and tmp_sim_matrix.max() != 0:
            tmp_sim_matrix /= tmp_sim_matrix.max()
        tmp_sim_matrix[:] = 1 - tmp_sim_matrix

        # Fill the entire similarity matrix
        for idx in nonempty_rows:  # Rows insertion
            sim_matrix[idx, : tmp_sim_matrix.shape[1]] = tmp_sim_matrix[
                temp_map_primary[idx]
            ]
        # Cols permutation
        mapping = np.full(secondary_dim, secondary_dim - 1, dtype=int)
        for idx in nonempty_cols:
            mapping[idx] = temp_map_secondary[idx]
        sim_matrix[:] = sim_matrix[:, mapping]
