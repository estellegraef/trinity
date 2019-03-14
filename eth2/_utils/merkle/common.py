from typing import (
    Callable,
    Iterable,
    NewType,
    Sequence,
    Union,
)

from cytoolz import (
    iterate,
    partition,
    take,
)
from eth2.beacon._utils.hash import (
    hash_eth2,
)
from eth_typing import (
    Hash32,
)
from eth_utils import (
    ValidationError,
)


MerkleTree = NewType("MerkleTree", Sequence[Sequence[Hash32]])
MerkleProof = NewType("MerkleProof", Sequence[Hash32])


def get_root(tree: MerkleTree) -> Hash32:
    """
    Get the root hash of a Merkle tree.
    """
    return tree[0][0]


def get_branch_indices(node_index: int, depth: int) -> Iterable[int]:
    """
    Get the indices of all ancestors up until the root for a node with a given depth.
    """
    return tuple(take(depth, iterate(lambda index: index // 2, node_index)))


def _calc_parent_hash(left_node: Hash32, right_node: Hash32) -> Hash32:
    """
    Calculate the parent hash of a node and its sibling.
    """
    return hash_eth2(left_node + right_node)


def _hash_layer(layer: Sequence[Hash32]) -> Iterable[Hash32]:
    """
    Calculate the layer on top of another one.
    """
    return tuple(
        _calc_parent_hash(left, right)
        for left, right in partition(2, layer)
    )


def get_merkle_proof(tree: MerkleTree, item_index: int) -> Iterable[Hash32]:
    """
    Read off the Merkle proof for an item from a Merkle tree.
    """
    if item_index < 0 or item_index >= len(tree[-1]):
        raise ValidationError("Item index out of range")

    # special case of tree consisting of only root
    if len(tree) == 1:
        return ()

    branch_indices = get_branch_indices(item_index, len(tree))
    proof_indices = [i ^ 1 for i in branch_indices][:-1]  # get sibling by flipping rightmost bit
    return tuple(
        layer[proof_index]
        for layer, proof_index
        in zip(reversed(tree), proof_indices)
    )


def calc_merkel_tree_maker(
        calc_merkle_tree_from_leaves: Callable[[Sequence[Union[bytes, bytearray]]], MerkleTree]) -> Callable[[Sequence[Hash32]], MerkleTree]:  # noqa: E501
    def calc_merkle_tree(items: Sequence[Union[bytes, bytearray]]) -> MerkleTree:
        leaves = tuple(hash_eth2(item) for item in items)
        return calc_merkle_tree_from_leaves(leaves)
    return calc_merkle_tree
