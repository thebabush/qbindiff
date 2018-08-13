# coding: utf-8

import logging
import numpy as np
from scipy.sparse import csr_matrix, diags
from functools import reduce

from qbindiff.types import Generator, ℝ, CallGraph, InputMatrix, Vector, BeliefMatching, Iterator


class BeliefMatrixError(Exception):
    pass


class BeliefMWM(object):
    """
    Compute the **Maxmimum Weight Matching** of the matrix of weights (similarity).
    Returns the *real* maximum assignement.
    """
    def __init__(self, weights: csr_matrix):
        weights = self._checkmatrix(weights)
        self.weights = weights.data
        self.objective = []
        self._init_indices(weights)
        self._init_messages()
        self._converged_iter = 0

    def compute_matching(self, maxiter: int=100) -> Generator[int, None, None]:
        niter = 0
        for _ in range(maxiter):
            self._update_messages()
            niter += 1
            yield niter
            if self._converged():
                for _ in range(self._converged_iter):
                    self._update_messages()
                    niter += 1
                    yield niter
                yield maxiter
                logging.debug("converged after %d iterations" % niter)
                return
        logging.debug("did not converged after %d iterations" % niter)

    @property
    def matching(self) -> BeliefMatching:
        def row_match(rowmates, colidx):
            if not np.logical_xor.reduce(rowmates):
                return None
            return colidx[rowmates][0]
        matching = map(row_match, self._rowslice(self.mates), self._rowslice(self._colidx))
        return list(enumerate(matching))

    def _init_indices(self, weights: csr_matrix) -> None:
        self.dims = weights.shape
        self._colidx = weights.indices
        self._rowmap = weights.indptr
        self._colmap = np.hstack((0, np.bincount(self._colidx).cumsum()))
        self._tocol = self._colidx.argsort(kind="mergesort")
        self._torow = self._tocol.argsort(kind="mergesort")

    def _init_messages(self) -> None:
        self.x = self.weights.copy()
        self.y = self.weights.copy()

    def _update_messages(self) -> None:
        self.x = self.weights - np.maximum(0, self._other_rowmax(self.y))
        self.y = self.weights - np.maximum(0, self._other_colmax(self.x))

        self.mates = (self.x + self.y - self.weights) > 0
        self.objective.append(self._objective())

    def _rowslice(self, vector: Vector) -> Iterator[Vector]:
        def get_slice(x, y): return vector[x:y]
        return map(get_slice, self._rowmap[:-1], self._rowmap[1:])

    def _colslice(self, vector: Vector) -> Iterator[Vector]:
        def get_slice(x, y): return vector[x:y]
        vector = vector[self._tocol]
        return map(get_slice, self._colmap[:-1], self._colmap[1:])

    def _other_rowmax(self, vector: Vector) -> Vector:
        maxvec = map(self._othermax_, self._rowslice(vector))
        return np.hstack(maxvec)

    def _other_colmax(self, vector: Vector) -> Vector:
        maxvec = map(self._othermax_, self._colslice(vector))
        return np.hstack(maxvec)[self._torow]

    @staticmethod
    def _othermax_(vector: Vector) -> Vector:
        """
        Compute the maximum value for all elements except (for the maxmimum value)
        $$x_i = max_{j!=i}{x_j}$$
        """
        maxvec = np.zeros_like(vector)
        if len(vector) > 1:
            max2, max1 = vector.argsort()[-2:]
            maxvec += vector[max1]
            maxvec[max1] = vector[max2]
        return maxvec

    def _objective(self) -> float:
        return self.weights[self.mates].sum()

    def _converged(self, m: int=5, w: int=50) -> bool:
        """
        Decide whether or not the algorithm have converged

        :param m: minimum size of the pattern to match
        :param w: latest score of the w last function matching

        :return: True or False if the algorithm have converged
        :rtype: bool
        """
        def _converged_(obj, idx):
            return obj[-2*idx:-idx] == obj[-idx:]
        patterns = self.objective[-w:-m]
        actual = self.objective[-1]
        if actual in patterns:
            pivot = patterns[::-1].index(actual) + m
            if _converged_(self.objective, pivot):
                self._converged_iter = np.argmax(self.objective[-pivot:]) + 1
                return True
        return False

    @staticmethod
    def _checkmatrix(matrix: InputMatrix) -> csr_matrix:
        """
        Normalize the weight values into something homogenous

        .. todo:: check if all weights are positives
        """
        try:
            matrix = csr_matrix(matrix)
        except Exception:
            raise BeliefMatrixError("Unknown matrix type: %s" % str(type(matrix)))
        if not (matrix.getnnz(0).all() and matrix.getnnz(1).all()):
            raise BeliefMatrixError("Incomplete bipartite, (isolated nodes)")
        return matrix


class BeliefNAQP(BeliefMWM):
    """
    Compute an approximate solution to **Network Alignement Quadratic Problem**.
    """
    def __init__(self, weights: InputMatrix, edges1: CallGraph, edges2: CallGraph, alpha: ℝ=1, beta: ℝ=5):
        super().__init__(alpha * weights)
        assert(alpha >= 0)
        assert(beta >= 0)
        self.s = self.compute_squares(weights, edges1, edges2)
        self.z = self.s.astype(float)
        self.beta = beta

    def _update_messages(self) -> None:
        zclip = self.z.copy().T
        zclip.data = np.clip(zclip.data + self.beta, 0, self.beta)
        mz = self.weights + zclip.sum(1).getA1()
        self.x = mz - np.maximum(0, self._other_rowmax(self.y))
        self.y = mz - np.maximum(0, self._other_colmax(self.x))
        mxyz = self.x + self.y - mz
        self.z = diags(mxyz).dot(self.s) - zclip

        self.mates = mxyz >= 0
        self.objective.append(self._objective())

    def _objective(self) -> float:
        objective = super()._objective()
        objective += self.beta * self.numsquares
        return objective

    @property
    def numsquares(self) -> int:
        return self.s[self.mates][:, self.mates].nnz / 2

    @staticmethod
    def compute_squares(weights: csr_matrix, edge1: CallGraph, edge2: CallGraph) -> csr_matrix:
        L = weights.astype(bool).toarray()
        cumsum = L.cumsum().reshape(L.shape) * L
        colsum = np.hstack((0, cumsum.max(1)))
        edgesum = [len(e) for e in edge2]
        buildix = lambda x: [x[0]]*edgesum[x[1]]
        idxx, idxy = [], []
        for i, j in ((i, j.nonzero()[0]) for i, j in enumerate(L)):
            edgesi = edge1[i]
            edgesj = reduce(list.__add__, map(edge2.__getitem__,  j), [])
            idxj = reduce(list.__add__, map(buildix,  enumerate(j)), [])
            match = cumsum[edgesi][:, edgesj]
            matched = match.nonzero()
            idxx.extend(map(lambda idx: idxj[idx] + colsum[i], matched[1]))
            idxy.extend(list(match[matched] - 1))
        s = colsum.max()
        boolean = np.ones(len(idxx), bool)
        S = csr_matrix((boolean, (idxx, idxy)), shape=(s, s), dtype=bool)
        S += S.T
        return S
