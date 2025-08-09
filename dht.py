import os
import socket
import bencodepy
from utils import logger

BOOTSTRAP_NODES = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


class DHTClient:
    """Simple DHT client for retrieving peers via get_peers"""

    def __init__(self, node_id: bytes = None):
        self.node_id = node_id or os.urandom(20)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(3)

    def _build_get_peers(self, tid: bytes, info_hash: bytes) -> bytes:
        return bencodepy.encode({
            b"t": tid,
            b"y": b"q",
            b"q": b"get_peers",
            b"a": {b"id": self.node_id, b"info_hash": info_hash},
        })

    def get_peers(self, info_hash: bytes):
        peers = []
        for host, port in BOOTSTRAP_NODES:
            try:
                tid = os.urandom(2)
                msg = self._build_get_peers(tid, info_hash)
                self.sock.sendto(msg, (host, port))
                data, _ = self.sock.recvfrom(2048)
                resp = bencodepy.decode(data)
                r = resp.get(b"r", {})
                if b"values" in r:
                    for entry in r[b"values"]:
                        ip = ".".join(str(b) for b in entry[:4])
                        peer_port = int.from_bytes(entry[4:], "big")
                        peers.append((ip, peer_port))
            except Exception as e:
                logger.debug("DHT問い合わせ失敗: %s:%s %s", host, port, e)
        return peers
