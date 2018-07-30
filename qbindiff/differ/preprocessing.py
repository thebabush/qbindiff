# coding: utf-8

import logging
import numpy as np
from pandas import DataFrame
from scipy.spatial.distance import cdist
from scipy.sparse import csr_matrix
from functools import reduce

from typing import List, Dict, Tuple, Optional, Union
from ml_analysis.features.visitor import ProgramVisitor
from ml_analysis.loader.program import Program
from pandas import DataFrame, Index
Addr = int
Idx = int
Vector = np.array  # 1-Dimensional array
InputMatrix = Union[DataFrame, csr_matrix, np.array]
CallGraph = List[List[Idx]]
AddrIndex = Index  # panda index of addresses


def load_features(program1:Program, program2:Program, visitor:ProgramVisitor) -> Tuple[DataFrame, DataFrame]:
    program_features1, program_features2 = (visitor.visit_program(p) for p in (program1, program2))

    features_idx = _build_feature_idx(program_features1, program_features2)

    features1 = _vectorize_features(program_features1, features_idx)
    features2 = _vectorize_features(program_features2, features_idx)
    return features1, features2

def _build_feature_idx(program_features1:dict, program_features2:dict) -> dict:
    '''
    Builds a set a all existing features in both programs
    '''
    features_names = set()
    for p_features in [program_features1, program_features2]:
        for function in p_features.values():
            features_names.update(function.keys())
    return dict(zip(features_names, range(len(features_names))))


def _vectorize_features(program_features:dict, features_idx:dict) -> DataFrame:
    '''
    Converts function features into vector forms (DataFrame)
    '''
    features = np.zeros((len(program_features), len(features_idx)), np.float32) # Check floating size according to program size
    for funid, pfeatures in enumerate(program_features.values()):
        opid, count = zip(*((features_idx[opc], count) for opc, count in pfeatures.items() if opc in features_idx))
        features[funid, opid] = count
    features = DataFrame(features, index=program_features.keys())
    return features


def build_weight_matrix(features1: DataFrame, features2: DataFrame, distance: str="correlation", threshold: float=.75) -> Tuple[AddrIndex, AddrIndex, csr_matrix]:
    '''
    Processes features, then builds the weight matrix and applies the specified threshold
    Recall : the weights are to be MAXIMISED so they should computed according to a SIMILARITY measure (not a distance)
    '''
    features1, features2 = process_features(features1, features2)
    weight_matrix = 1 - cdist(features1, features2, distance) # Distance to similarity
    threshmask = weight_matrix > threshold
    weight_matrix *= threshmask
    _compute_sparsity(threshmask)
    rowmask = threshmask.any(1) # Keep vertex with at least
    colmask = threshmask.any(0) # one possible matching
    adds1 = features1.index[rowmask]
    adds2 = features2.index[colmask]
    weight_matrix = csr_matrix(weight_matrix[rowmask][:, colmask])
    return adds1, adds2, weight_matrix

def process_features(features1:DataFrame, features2:DataFrame) -> Tuple[DataFrame, DataFrame]:
    opcmask = features1.astype(bool).sum(0) > 1  # remove features that only appears
    opcmask &= features2.astype(bool).sum(0) > 1 # in one function or in one graph

    features1 = features1.loc[:,opcmask].drop_duplicates() # remove duplicated rows
    features2 = features2.loc[:,opcmask].drop_duplicates() # (near-duplicate functions)

    opcsum = features1.sum(0) + features2.sum(0)
    features1 /= opcsum # feature ponderation via total
    features2 /= opcsum # number of appearance per features

    return features1, features2

def build_callgraphs(program1: Program, program2: Program, adds1: AddrIndex, adds2: AddrIndex) -> Tuple[CallGraph, CallGraph]:
    '''
    Builds call-graph of functions selected for the matchings (subgraph)
    Converts address -> index
    '''
    def _build_callgraph(program, adds):
        addindex = dict(zip(adds, range(len(adds))))
        return [list({addindex[nadd] for nadd in program[funadd].children if nadd in adds}) for funadd in adds]
    callgraph_p1 = _build_callgraph(program1, adds1)
    callgraph_p2 = _build_callgraph(program2, adds2)
    return callgraph_p1, callgraph_p2


def _compute_sparsity(mask:DataFrame) -> None:
    nnz = np.count_nonzero(mask)
    size = np.prod(mask.shape)
    sparse = 100 * nnz / size
    logging.debug("[+] items number : %d/%d (sparsity: %.2f%%)" % (nnz, size, sparse))
