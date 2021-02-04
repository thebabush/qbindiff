# coding: utf-8
import logging

import numpy as np
from lapjv import lapjv
from itertools import chain
from scipy.sparse import csr_matrix
from qbindiff.matcher.belief_propagation import BeliefMWM, BeliefQAP


def solve_linear_assignment(sim_matrix):
    n, m = sim_matrix.shape
    transposed = n > m
    if transposed:
        n, m = m, n
        sim_matrix = sim_matrix.T
    cost_matrix = np.zeros((m, m), dtype=sim_matrix.dtype)
    cost_matrix[:n, :m] = - sim_matrix
    idy = lapjv(cost_matrix)[0][:n]
    if transposed:
        return idy, np.arange(n)
    return np.arange(n), idy


class Matcher:

    def __init__(self, primary_affinity, secondary_affinity, similarity_matrix):
        self.primary_affinity = primary_affinity
        self.secondary_affinity = secondary_affinity
        self.full_sim_matrix = similarity_matrix

        self.sparse_sim_matrix = None
        self.squares_matrix = None

    def process(self, sparsity_ratio=.75, compute_squares=True):
        mask = self._compute_matrix_mask(self.full_sim_matrix, sparsity_ratio)
        self.sparse_sim_matrix = self._compute_sparse_matrix(self.full_sim_matrix, mask)
        if compute_squares:
            self.squares_matrix = self._compute_squares_matrix(self.sparse_sim_matrix, self.primary_affinity, self.secondary_affinity)

    def compute(self, tradeoff=.5, epsilon=.05, maxiter=1000):
        if tradeoff == 1:
            logging.info('[+] switching to Maximum Weight Matching (tradeoff is 1)')
            belief = BeliefMWM(self.sparse_sim_matrix, epsilon)
        else:
            belief = BeliefQAP(self.sparse_sim_matrix, self.squares_matrix, tradeoff, epsilon)

        for niter in belief.compute(maxiter):
            yield niter

        score_matrix = self.sparse_sim_matrix.copy()
        score_matrix.data[:] = belief._best_messages
        self.mapping = self._refine(belief.mapping, score_matrix)

    def display_statistics(self, mapping=None):
        similarities, common_subgraph = self._compute_statistics(mapping)
        nb_matches = len(similarities)
        similarity = similarities.sum()
        nb_squares = common_subgraph.sum()

        output = 'Score: {:.4f} | '\
                 'Similarity: {:.4f} | '\
                 'Squares: {:.0f} | '\
                 'Nb matches: {}\n'.format(similarity + nb_squares, similarity, nb_squares, nb_matches)
        output += 'Node cover:  {:.3f}% / {:.3f}% | '\
                  'Edge cover:  {:.3f}% / {:.3f}%\n'.format(100 * nb_matches / len(self.primary_affinity),
                                                            100 * nb_matches / len(self.secondary_affinity),
                                                            100 * nb_squares / self.primary_affinity.sum(),
                                                            100 * nb_squares / self.secondary_affinity.sum())
        return output

    def format_mapping(self):
        idx, idy = self.mapping
        similarities, common_subgraph = self._compute_statistics(mapping)
        nb_squares = common_subgraph.sum(0) + common_subgraph.sum(1)
        return idx, idy, similarities, nb_squares

    def _compute_statistics(self, mapping=None):
        if mapping is None:
            mapping = self.mapping
        idx, idy = mapping
        similarities = self.full_sim_matrix[idx, idy]
        common_subgraph = self.primary_affinity[np.ix_(idx, idx)]
        common_subgraph &= self.secondary_affinity[np.ix_(idy, idy)]
        return similarities, common_subgraph

    @staticmethod
    def _compute_matrix_mask(full_matrix, sparsity_ratio=.75):
        if sparsity_ratio == 0:
            return full_matrix.astype(bool)
        elif sparsity_ratio == 1:
            threshold = full_matrix.max(1, keepdims=True)
            return full_matrix >= threshold
        matrix = full_matrix.copy()
        ratio = int(sparsity_ratio * matrix.size)
        if matrix.shape[0] > matrix.shape[1]:
            matrix /= matrix.max(0)
        else:
            matrix /= matrix.max(1, keepdims=True)
        threshold = np.partition(matrix.reshape(-1), ratio)[ratio]
        if threshold==0:
            threshold += 1e-8
        mask = matrix >= threshold
        return mask

    @staticmethod
    def _compute_sparse_matrix(full_matrix, mask):
        sparse_data = full_matrix[mask]
        sparse_matrix = csr_matrix(mask, dtype=full_matrix.dtype)
        sparse_matrix.data[:] = sparse_data
        return sparse_matrix

    @staticmethod
    def _compute_squares_matrix(sparse_matrix, primary_affinity, secondary_affinity):
        size = sparse_matrix.nnz
        edgelist1 = [list(edges.nonzero()[0]) for edges in primary_affinity]
        edgelist2 = [list(edges.nonzero()[0]) for edges in secondary_affinity]
        bipartite = sparse_matrix.astype(np.uint32)
        bipartite.data[:] = np.arange(1, size+1, dtype=np.uint32)
        indices = bipartite.indices.astype(np.uint32)
        indptr = bipartite.indptr.astype(np.uint32)
        bipartite = bipartite.toarray()
        edgenum = np.fromiter(map(len, edgelist2), np.uint32)
        idxx, idxy = [], []
        for row, (begin, end) in enumerate(zip(indptr, indptr[1:])):
            cols = indices[begin:end]
            rowedges = edgelist1[row]
            coledges = list(chain(*map(edgelist2.__getitem__,  cols)))
            squares = bipartite[np.ix_(rowedges, coledges)]
            sqx, sqy = squares.nonzero()
            idx = indptr[row] + np.searchsorted(edgenum[cols].cumsum(), sqy, side="right")
            idy = squares[sqx, sqy] - 1
            idxx.extend(idx.tolist())
            idxy.extend(idy.tolist())
        ones = np.ones(len(idxx), dtype=sparse_matrix.dtype)
        squares_matrix = csr_matrix((ones, (idxx, idxy)), shape=(size, size), dtype=sparse_matrix.dtype)
        squares_matrix += squares_matrix.T
        return squares_matrix

    @staticmethod
    def _refine(self, mapping, score_matrix):
        idx, idy = mapping
        if len(idx) == min(score_matrix.shape):
            return
        idxmask = np.setdiff1d(range(score_matrix.shape[0]), idx)
        idymask = np.setdiff1d(range(score_matrix.shape[1]), idy)
        score_matrix = score_matrix[idxmask][:,idymask].toarray()
        score_matrix[score_matrix.nonzero()] += 100 - score_matrix.min()

        idxx, idyy = solve_linear_assignment(score_matrix)

        return np.hstack((idx, idxmask[idxx])), np.hstack((idy, idymask[idyy]))

        