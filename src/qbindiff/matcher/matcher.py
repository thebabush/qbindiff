# built-in imports
import logging

# Third-party imports
import numpy as np
from lapjv import lapjv
from scipy.sparse import csr_matrix, coo_matrix
from collection.abc import Generator
from typing import Tuple, List

# Local imports
from qbindiff.matcher.squares import find_squares
from qbindiff.matcher.belief_propagation import BeliefMWM, BeliefQAP
from qbindiff.types import (
    Positive,
    Ratio,
    RawMapping,
    AdjacencyMatrix,
    Matrix,
    SimMatrix,
    SparseMatrix,
)


def iter_csr_matrix(matrix: SparseMatrix) -> Generator[Tuple[np.ndarray, np.ndarray]]:
    """
    Iter over non-null items in a CSR (Compressed Sparse Row) matrix.
    It returns a generator that, at each iteration, returns the tuple (row_index, column_index, value)

    :param matrix: CSR matrix
    :return: generator (row_idx, column_idx, val)
    """

    coo_matrix = matrix.tocoo()
    for x, y, v in zip(coo_matrix.row, coo_matrix.col, coo_matrix.data):
        yield (x, y, v)


def solve_linear_assignment(cost_matrix: Matrix) -> RawMapping:
    """
    Solve the linear assignment problem given the cost_matrix

    :param: cost matrix
    :return: raw mapping
    """

    n, m = cost_matrix.shape
    transposed = n > m
    if transposed:
        n, m = m, n
        cost_matrix = cost_matrix.T
    full_cost_matrix = np.zeros((m, m), dtype=cost_matrix.dtype)
    full_cost_matrix[:n, :m] = cost_matrix
    col_indices = lapjv(full_cost_matrix)[0][:n]
    if transposed:
        return col_indices, np.arange(n)
    return np.arange(n), col_indices


class Matcher:
    def __init__(
        self,
        similarity_matrix: SimMatrix,
        primary_adj_matrix: AdjacencyMatrix,
        secondary_adj_matrix: AdjacencyMatrix,
    ):
        self._mapping = None  # nodes mapping
        #: Similarity matrix used by the Matcher
        self.sim_matrix = similarity_matrix
        #: Adjacency matrix of the primary graph
        self.primary_adj_matrix = primary_adj_matrix
        #: Adjacency matrix of the secondary graph
        self.secondary_adj_matrix = secondary_adj_matrix

        self.sparse_sim_matrix = None
        self.squares_matrix = None

    def _compute_sparse_sim_matrix(self, sparsity_ratio: Ratio, sparse_row: bool) -> None:
        """
        Generate the sparse similarity matrix given the sparsity_ratio

        :param sparsity_ratio: ratio of least probable matches to ignore
        :param sparse_row: whether to use sparse rows
        :return: None
        """
        
        ratio = round(sparsity_ratio * self.sim_matrix.size)

        if ratio == 0:
            self.sparse_sim_matrix = csr_matrix(self.sim_matrix)
            return
        elif ratio == self.sim_matrix.size:
            threshold = self.sim_matrix.max(1, keepdims=True)
            self.sparse_sim_matrix = self.sim_matrix >= threshold
            return

        if sparse_row:
            ratio = round(sparsity_ratio * self.sim_matrix.shape[1])
            mask = []
            for i in range(self.sim_matrix.shape[0]):
                threshold = np.partition(self.sim_matrix[i], ratio - 1)[ratio]
                # We never want to match nodes with a similarity score of 0, even if
                # it is the right threshold
                if threshold == 0:
                    threshold += 1e-8
                mask.append(self.sim_matrix[i] >= threshold)

            self.sparse_sim_matrix = csr_matrix(mask, dtype=self.sim_matrix.dtype)
            self.sparse_sim_matrix.data[:] = self.sim_matrix[mask]
        else:
            threshold = np.partition(self.sim_matrix, ratio - 1, axis=None)[ratio]
            # We never want to match nodes with a similarity score of 0, even if it is
            # the right threshold
            if threshold == 0:
                threshold += 1e-8
            mask = self.sim_matrix >= threshold
            csr_data = self.sim_matrix[mask]

            self.sparse_sim_matrix = csr_matrix(mask, dtype=self.sim_matrix.dtype)
            self.sparse_sim_matrix.data[:] = csr_data

    def _compute_squares_matrix(self) -> None:
        """
        Generate the sparse squares matrix and store it in self._squares_matrix.
        Given two graphs G1 and G2, a square is a tuple of nodes (nodeA, nodeB, nodeC, nodeD)
        such that all of the followings statements are true:
          - nodeA and nodeD belong to G1
          - nodeB and nodeC belong to G2
          - (nodeA, nodeD) is a directed edge in G1
          - (nodeB, nodeC) is a directed edge in G2
          - (nodeA, nodeB) is a edge in the similarity matrix (non-zero score)
          - (nodeC, nodeD) is a edge in the similarity matrix (non-zero score)
        Note that the nodes are not necessarily different since (nodeX, nodeX) might be
        a valid edge.

        (A) <---sim_edge---> (B)
         |                    |
         |graph_edge          |
         |          graph_edge|
         v                    v
        (D) <---sim_edge---> (C)

        The resulting square matrix is stored as a csr_matrix of size NxN where N=#{similarity edge}
        Every similarity edge is given a unique increasing number from 0 to N and there is a square
        between two similarity edges `e1` and `e2` <=> (iff) self._squares_matrix[e1][e2] == 1

        The time complexity is O(|sparse_sim_matrix| * average_graph_degree**2)

        :return: None
        """

        squares = find_squares(
            self.primary_adj_matrix, self.secondary_adj_matrix, self.sparse_sim_matrix
        )

        size = self.sparse_sim_matrix.nnz
        # Give each similarity edge a unique number
        bipartite = self.sparse_sim_matrix.astype(np.uint32)
        bipartite.data[:] = np.arange(0, size, dtype=np.uint32)

        get_edge = {}  # Fast lookup
        C = bipartite.shape[1]
        for i, j, v in iter_csr_matrix(bipartite):
            get_edge[i * C + j] = v

        # Populate the sparse squares matrix
        squares_2n = len(squares) * 2
        rows = np.zeros(squares_2n, dtype=np.uint32)
        cols = np.zeros(squares_2n, dtype=np.uint32)
        for i, (nodeA, nodeB, nodeC, nodeD) in enumerate(squares):
            e1 = get_edge[nodeA * C + nodeB]
            e2 = get_edge[nodeD * C + nodeC]
            rows[2 * i] = e1
            rows[2 * i + 1] = e2
            cols[2 * i] = e2
            cols[2 * i + 1] = e1
        data = np.ones(squares_2n, dtype=np.uint8)

        # Build coo matrix and convert it to csr
        coo_squares_matrix = coo_matrix(
            (data, (rows, cols)), shape=(size, size), dtype=np.uint8
        )
        self.squares_matrix = coo_squares_matrix.tocsr()

        # Sometimes a square is counted twice
        # ex: (nodeA, nodeB, nodeC, nodeD) == (nodeC, nodeD, nodeA, nodeB)
        # Set the data to ones to count all the squares only once
        self.squares_matrix.data[:] = 1

    @property
    def mapping(self) -> RawMapping:
        """
        Nodes mapping between the two graphs
        """

        return self._mapping

    @property
    def confidence_score(self) -> List[float]:
        """
        Confidence score for each match in the nodes mapping
        """

        return [self._confidence[idx1, idx2] for idx1, idx2 in zip(*self.mapping)]

    def process(
        self,
        sparsity_ratio: Ratio = 0.75,
        sparse_row: bool = False,
        compute_squares: bool = True,
    ):
        """
        Initialize the matching algorithm

        :param sparsity_ratio: The ratio between null element over the entire similarity
                               matrix
        :param sparse_row: When building the sparse similarity matrix we can either
                           filter out the elements by considering all the entries in the
                           similarity matrix (sparse_row == False) or by considering
                           each vector separately (sparse_row == True)
        :param compute_squares: Whether to compute the squares matrix
        :return: None
        """

        logging.debug(
            f"Computing sparse similarity matrix (ratio {sparsity_ratio} sparse_row {sparse_row})"
        )
        self._compute_sparse_sim_matrix(sparsity_ratio, sparse_row)
        logging.debug(
            f"Sparse similarity matrix computed, shape: {self.sparse_sim_matrix.shape}"
            f", nnz elements: {self.sparse_sim_matrix.nnz}"
        )
        if compute_squares:
            logging.debug("Computing squares matrix")
            self._compute_squares_matrix()
            logging.debug(
                f"Squares matrix computed, shape: {self.squares_matrix.shape}"
                f", nnz elements: {self.squares_matrix.nnz}"
            )

    def compute(
        self, tradeoff: Ratio = 0.75, epsilon: Positive = 0.5, maxiter: int = 1000
    ) -> None:
        """
        Launch the computation for a given number of iterations, using specific QBinDiff parameters

        :param tradeoff: tradeoff between the node similarity and the structure
        :param epsilon: perturbation to add to the similarity matrix
        :param maxiter: maximum number of iterations for the belief propagation
        :return: None
        """

        if tradeoff == 1:
            logging.info("[+] switching to Maximum Weight Matching (tradeoff is 1)")
            belief = BeliefMWM(self.sparse_sim_matrix, epsilon)
        else:
            belief = BeliefQAP(
                self.sparse_sim_matrix, self.squares_matrix, tradeoff, epsilon
            )

        for niter in belief.compute(maxiter):
            yield niter

        score_matrix = self.sparse_sim_matrix.copy()
        self._confidence = belief.current_marginals
        self._mapping = self.refine(belief.current_mapping, score_matrix)

    def refine(self, mapping: RawMapping, score_matrix: SimMatrix) -> RawMapping:
        """
        Refine the mappings between the nodes of the two graphs
        by matching the unassigned nodes

        :param mapping: initial mapping
        :param score_matrix: similarity matrix
        :return: updated raw mapping
        """
        
        primary, secondary = mapping
        assert len(primary) == len(secondary)

        # All the nodes have been assigned
        if len(primary) == min(score_matrix.shape):
            return mapping

        primary_missing = np.setdiff1d(range(score_matrix.shape[0]), primary)
        secondary_missing = np.setdiff1d(range(score_matrix.shape[1]), secondary)
        score_matrix = score_matrix[primary_missing][:, secondary_missing]
        nnz_indices = score_matrix.nonzero()
        score_matrix = score_matrix.toarray()
        # Give the zero elements a high score
        lap_scores = np.full(score_matrix.shape, 1000000, dtype=score_matrix.dtype)
        # LAP solves solves for the minimum cost but high scores means good match
        lap_scores[nnz_indices] = -score_matrix[nnz_indices]

        primary_ass, secondary_ass = solve_linear_assignment(lap_scores)

        return np.hstack((primary, primary_missing[primary_ass])), np.hstack(
            (secondary, secondary_missing[secondary_ass])
        )
