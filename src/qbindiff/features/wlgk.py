import random
from functools import cache
from collections import defaultdict, Counter
from abc import ABCMeta, abstractmethod

from qbindiff.features.extractor import FunctionFeatureExtractor, FeatureCollector
from qbindiff.loader import Program, Function, BasicBlock


class LSH(metaclass=ABCMeta):
    """
    Abstract class representing a Locality Sensitive Hashing function.
    It defines the interface to the function.
    """

    @abstractmethod
    def __init__(self, node: BasicBlock):
        raise NotImplementedError()

    @abstractmethod
    def __add__(self, lsh: "LSH") -> "LSH":
        raise NotImplementedError()

    @abstractmethod
    def __iadd__(self, lsh: "LSH") -> "LSH":
        raise NotImplementedError()

    @abstractmethod
    def prepend(self, lsh: "LSH") -> None:
        """Prepend the lsh to the current hash"""
        raise NotImplementedError()

    @property
    @abstractmethod
    def hash(self) -> bytes:
        """Return the hash assigned to the node"""
        raise NotImplementedError()

    @abstractmethod
    def add(self, lsh: "LSH") -> None:
        """Add the hash lsh to the current hash"""
        raise NotImplementedError()


class BOWLSH(LSH):
    """Extract the bag-of-words representation of a block. The hashes are 8 bytes long"""

    def __init__(self, node: BasicBlock = None):
        self.pre = b""  # Prepended hash
        self.bag = defaultdict(int)
        if node is not None:
            for instr in node:
                self.bag[instr._backend.cs_instr.id] += 1

    def __iadd__(self, lsh: "BOWLSH") -> "BOWLSH":
        for k, v in lsh.bag.items():
            self.bag[k] += v
        return self

    def __add__(self, lsh: "BOWLSH") -> "BOWLSH":
        res = self.copy()
        res += lsh
        return res

    def copy(self) -> "BOWLSH":
        res = BOWLSH()
        res.bag = self.bag.copy()
        return res

    def prepend(self, lsh: "LSH") -> None:
        """Prepend the lsh to the current hash"""
        self.pre = lsh.hash

    @property
    def hash(self) -> bytes:
        """Return the hash assigned to the node"""

        resHash = 0
        for hp in BOWLSH.hyperplanes:
            resHash <<= 1
            prod = sum(hp[k] * v for k, v in self.bag.items())
            if prod >= 0:
                resHash |= 1

        return resHash.to_bytes(4, "big")

    def add(self, lsh: "LSH") -> None:
        """Add the hash lsh to the current hash"""
        self.__iadd__(lsh)

    @classmethod
    @property
    @cache
    def hyperplanes(cls):
        """
        Generate the hyperplanes for the LSH.
        Each hyperplane is identified by its normal vector v from R^2000: v * x = 0
        the dimension 2000 should be sufficient to characterize the basic asm blocks
        """

        hyperplanes = []
        random.seed(0)
        for k in range(32):
            hyperplanes.append([2 * random.random() - 1 for i in range(2000)])
        return hyperplanes


class WeisfeilerLehman(FunctionFeatureExtractor):
    """Weisfeiler-Lehman Graph Kernel"""

    key = "wlgk"

    def __init__(self, *args, lsh: type[LSH] = None, **kwargs):
        """Extract a feature vector by using a custom defined node labeling scheme."""
        super(WeisfeilerLehman, self).__init__(*args, **kwargs)

        if lsh is None:
            self.lsh = BOWLSH
        else:
            self.lsh = lsh

    def visit_function(
        self, program: Program, function: Function, collector: FeatureCollector
    ):
        labels = []  # Labels for each node at step i
        map_node_to_index = {}
        adjacency = defaultdict(list)

        # Label each node of the graph
        for bb_addr, bb in function.items():
            labels.append(self.lsh(bb))
            # We have to get the BlockNode from the corresponding Block and map
            # it to our node index
            map_node_to_index[bb_addr] = len(labels) - 1

        # Adjacency
        for source, target in function.edges:
            adjacency[map_node_to_index[source]].append(map_node_to_index[target])

        vec = [l.hash for l in labels]
        prev_counter = 0
        for step in range(len(labels)):
            # Recalculate labels at each step
            newLabels = []
            for node, label in enumerate(labels):
                newLabels.append(label.copy())
                for n in adjacency[node]:
                    newLabels[node] += labels[n]

            labels = newLabels
            already = set()
            counter = 0
            for l in labels:
                h = l.hash
                if h in already:
                    counter += 1
                else:
                    already.add(h)
                vec.append(h)

            # Early stop
            if counter == prev_counter:
                break
            prev_counter = counter

        # Generate the frequency vector of the labels
        collector.add_dict_feature(self.key, dict(Counter(vec)))
