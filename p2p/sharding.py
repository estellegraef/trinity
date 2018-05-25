import asyncio
from collections import (
    defaultdict,
)
import logging
import time
from typing import (
    cast,
    Dict,
    List,
    Set,
    Tuple,
)

from eth_typing import (
    Hash32,
)

from rlp import sedes

from evm.rlp.collations import Collation
from evm.rlp.headers import CollationHeader
from evm.rlp.sedes import (
    hash32,
)
from evm.chains.shard import Shard

from evm.db.shard import (
    Availability,
)

from evm.utils.padding import (
    zpad_right,
)
from evm.utils.blobs import (
    calc_chunk_root,
)

from evm.constants import (
    COLLATION_SIZE,
)
from evm.exceptions import (
    CollationHeaderNotFound,
    CollationBodyNotFound,
)

from p2p.cancel_token import (
    CancelToken,
    wait_with_token,
)
from p2p import protocol
from p2p.service import BaseService
from p2p.protocol import (
    Command,
    Protocol,
)
from p2p.peer import (
    BasePeer,
    PeerPool,
    PeerPoolSubscriber,
)
from p2p.p2p_proto import (
    DisconnectReason,
)
from p2p.utils import (
    gen_request_id,
)
from p2p.exceptions import (
    HandshakeFailure,
    OperationCancelled,
    UnexpectedMessage,
)

from cytoolz import (
    excepts,
)


COLLATION_PERIOD = 1


class Status(Command):
    _cmd_id = 0


class Collations(Command):
    _cmd_id = 1

    structure = [
        ("request_id", sedes.big_endian_int),
        ("collations", sedes.CountableList(Collation)),
    ]


class GetCollations(Command):
    _cmd_id = 2

    structure = [
        ("request_id", sedes.big_endian_int),
        ("collation_hashes", sedes.CountableList(hash32)),
    ]


class NewCollationHashes(Command):
    _cmd_id = 3

    structure = [
        (
            "collation_hashes_and_periods", sedes.CountableList(
                sedes.List([hash32, sedes.big_endian_int])
            )
        ),
    ]


class ShardingProtocol(Protocol):
    name = "sha"
    version = 0
    _commands = [Status, Collations, GetCollations, NewCollationHashes]
    cmd_length = 4

    logger = logging.getLogger("p2p.sharding.ShardingProtocol")

    def send_handshake(self) -> None:
        cmd = Status(self.cmd_id_offset)
        self.logger.debug("Sending status msg")
        self.send(*cmd.encode([]))

    def send_collations(self, request_id: int, collations: List[Collation]) -> None:
        cmd = Collations(self.cmd_id_offset)
        self.logger.debug("Sending %d collations (request id %d)", len(collations), request_id)
        data = {
            "request_id": request_id,
            "collations": collations,
        }
        self.send(*cmd.encode(data))

    def send_get_collations(self, request_id: int, collation_hashes: List[Hash32]) -> None:
        cmd = GetCollations(self.cmd_id_offset)
        self.logger.debug(
            "Requesting %d collations (request id %d)",
            len(collation_hashes),
            request_id,
        )
        data = {
            "request_id": request_id,
            "collation_hashes": collation_hashes,
        }
        self.send(*cmd.encode(data))

    def send_new_collation_hashes(self,
                                  collation_hashes_and_periods: List[Tuple[Hash32, int]]) -> None:
        cmd = NewCollationHashes(self.cmd_id_offset)
        self.logger.debug(
            "Announcing %d new collations (period %d to %d)",
            len(collation_hashes_and_periods),
            min(period for _, period in collation_hashes_and_periods),
            max(period for _, period in collation_hashes_and_periods)
        )
        data = {
            "collation_hashes_and_periods": collation_hashes_and_periods
        }
        self.send(*cmd.encode(data))


class ShardingPeer(BasePeer):
    _supported_sub_protocols = [ShardingProtocol]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.known_collation_hashes: Set[Hash32] = set()
        self._pending_replies: Dict[
            int,
            asyncio.Future[Tuple[Command, protocol._DecodedMsgType]]
        ] = {}

    #
    # Handshake
    #
    async def send_sub_proto_handshake(self) -> None:
        cast(ShardingProtocol, self.sub_proto).send_handshake()

    async def process_sub_proto_handshake(self,
                                          cmd: Command,
                                          msg: protocol._DecodedMsgType) -> None:
        if not isinstance(cmd, Status):
            self.disconnect(DisconnectReason.other)
            raise HandshakeFailure("Expected status msg, got {}, disconnecting".format(cmd))

    #
    # Message handling
    #
    def handle_sub_proto_msg(self, cmd: Command, msg: protocol._DecodedMsgType) -> None:
        if isinstance(msg, dict):
            request_id = msg.get("request_id")
            if request_id is not None and request_id in self._pending_replies:
                # This is a reply we're waiting for, so we consume it by resolving the registered
                # future
                future = self._pending_replies.pop(request_id)
                future.set_result((cmd, msg))
                return
        super().handle_sub_proto_msg(cmd, msg)

    #
    # Requests
    #
    async def get_collations(self,
                             collation_hashes: List[Hash32],
                             cancel_token: CancelToken) -> Set[Collation]:
        # Don't send empty request
        if len(collation_hashes) == 0:
            return set()

        request_id = gen_request_id()
        pending_reply: asyncio.Future[Tuple[Command, protocol._DecodedMsgType]] = asyncio.Future()
        self._pending_replies[request_id] = pending_reply
        cast(ShardingProtocol, self.sub_proto).send_get_collations(request_id, collation_hashes)
        cmd, msg = await wait_with_token(pending_reply, token=cancel_token)

        if not isinstance(cmd, Collations):
            raise UnexpectedMessage(
                "Expected Collations as response to GetCollations, but got %s",
                cmd.__class__.__name__
            )
        return set(msg["collations"])


class ShardSyncer(BaseService, PeerPoolSubscriber):
    logger = logging.getLogger("p2p.sharding.ShardSyncer")

    def __init__(self, shard: Shard, peer_pool: PeerPool, token: CancelToken=None) -> None:
        super().__init__(token)

        self.shard = shard
        self.peer_pool = peer_pool
        self._running_peers: Set[ShardingPeer] = set()

        self.collation_hashes_at_peer: Dict[ShardingPeer, Set[Hash32]] = defaultdict(set)

        self.start_time = time.time()

    async def _run(self) -> None:
        with self.subscribe(self.peer_pool):
            await self.cancel_token.wait()

    async def _cleanup(self) -> None:
        pass

    def propose(self) -> Collation:
        """Broadcast a new collation to the network, add it to the local shard, and return it."""
        # create collation for current period
        period = self.get_current_period()
        body = zpad_right(str(self).encode("utf-8"), COLLATION_SIZE)
        header = CollationHeader(self.shard.shard_id, calc_chunk_root(body), period, b"\x11" * 20)
        collation = Collation(header, body)

        self.logger.debug("Proposing collation {}".format(collation))

        # add collation to local chain
        self.shard.add_collation(collation)

        # broadcast collation
        for peer in self.peer_pool.peers:
            cast(ShardingProtocol, peer.sub_proto).send_new_collation_hashes(
                [(collation.hash, collation.period)]
            )

        return collation

    #
    # Peer handling
    #
    def register_peer(self, peer: BasePeer) -> None:
        asyncio.ensure_future(self.handle_peer(cast(ShardingPeer, peer)))

    async def handle_peer(self, peer: ShardingPeer) -> None:
        """Handle the lifecycle of the given peer."""
        self._running_peers.add(peer)
        # Use a local token that we'll trigger to cleanly cancel the _handle_peer() sub-tasks when
        # self.finished is set.
        peer_token = self.cancel_token.chain(CancelToken("HandlePeer"))
        try:
            await asyncio.wait(
                [self._handle_peer(peer, peer_token), self.finished.wait()],
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            peer_token.trigger()
            self._running_peers.remove(peer)

    async def _handle_peer(self, peer: ShardingPeer, token: CancelToken) -> None:
        while not self.is_finished:
            try:
                cmd, msg = await peer.read_sub_proto_msg(token)
            except OperationCancelled:
                # Either our cancel token or the peer's has been triggered, so break out of the
                # loop.
                break

            pending_msgs = peer.sub_proto_msg_queue.qsize()
            if pending_msgs:
                self.logger.debug(
                    "Read %s msg from %s's queue; %d msgs pending", cmd, peer, pending_msgs)

            if isinstance(cmd, GetCollations):
                await self._handle_get_collations(peer, msg)
            elif isinstance(cmd, Collations):
                await self._handle_collations(peer, msg)
            elif isinstance(cmd, NewCollationHashes):
                await self._handle_new_collation_hashes(peer, msg)

    async def _handle_get_collations(self, peer, msg):
        """Respond with all requested collations that we know about."""
        collation_hashes = set(msg["collation_hashes"])
        self.collation_hashes_at_peer[peer] |= collation_hashes

        get_collation_or_none = excepts(
            (CollationHeaderNotFound, CollationBodyNotFound),
            self.shard.get_collation_by_hash
        )
        collations = [
            collation for collation in [
                get_collation_or_none(collation_hash) for collation_hash in collation_hashes
            ]
            if collation is not None
        ]
        self.logger.info(
            "Responding to peer %s with %d collations",
            peer.remote,
            len(collations),
        )
        peer.sub_proto.send_collations(msg["request_id"], collations)

    async def _handle_collations(self, peer, msg):
        """Add collations to our shard and notify peers about new collations available here."""
        collations_by_hash = {collation.hash: collation for collation in msg["collations"]}
        self.collation_hashes_at_peer[peer] |= set(collations_by_hash.keys())

        # add new collations to shard
        new_collations_by_hash = {
            collation.hash: collation for collation in collations_by_hash.values()
            if self.shard.get_availability(collation.header) is not Availability.AVAILABLE
        }
        self.logger.info(
            "Received %d collations, %d of which are new",
            len(collations_by_hash),
            len(new_collations_by_hash),
        )
        self.logger.info("%s %s", collations_by_hash, new_collations_by_hash)
        for collation in new_collations_by_hash.values():
            self.shard.add_collation(collation)

        # inform peers about new collations they might not know about already
        for peer in self.peer_pool.peers:
            known_hashes = self.collation_hashes_at_peer[peer]
            new_hashes = set(new_collations_by_hash.keys()) - known_hashes
            self.collation_hashes_at_peer[peer] |= new_hashes

            if new_hashes:
                new_collations = [
                    new_collations_by_hash[collation_hash] for collation_hash in new_hashes
                ]
                hashes_and_periods = [
                    (collation.hash, collation.period) for collation in new_collations
                ]
                peer.sub_proto.send_new_collation_hashes(hashes_and_periods)

    async def _handle_new_collation_hashes(self, peer, msg):
        """Request those collations."""
        # Request all collations for now, no matter if we now about them or not, as there's no way
        # to header existence at the moment. In the future we won't transfer collations anyway but
        # only collation bodies (headers are retrieved from the main chain), so there's no need to
        # add this at the moment.
        collation_hashes = set(
            collation_hash for collation_hash, _ in msg["collation_hashes_and_periods"]
        )
        if collation_hashes:
            peer.sub_proto.send_get_collations(gen_request_id(), list(collation_hashes))

    def get_current_period(self):
        # TODO: get this from main chain
        return int((time.time() - self.start_time) // COLLATION_PERIOD)
