import socket
import threading
import struct
import random
import time
from utils import recv_all, logger
from constants import BLOCK_SIZE, KEEPALIVE_INTERVAL

# -----------------------------------------------------------
# クラス：PeerConnection
# 概要：
#   各ピアとの通信を担当するスレッドクラスです。
#   ここでは、接続時の再試行機能と接続前のランダムディレイを追加しています。
# -----------------------------------------------------------
class PeerConnection(threading.Thread):
    def __init__(self, ip, port, info_hash, peer_id, piece_manager):
        """
        初期化処理
        :param ip: ピアの IP アドレス（文字列）
        :param port: ピアのポート番号（整数）
        :param info_hash: torrent の info_hash（バイナリ）
        :param peer_id: 自身のピアID（バイナリ）
        :param piece_manager: PieceManager のインスタンス
        """
        super().__init__()
        self.ip = ip
        self.port = port
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.piece_manager = piece_manager
        self.sock = None                # 通信に使用するソケット
        self.choked = True              # choke 状態かどうかのフラグ
        self.running = True             # スレッドが継続して動作するかのフラグ
        self.last_activity = time.time()  # 最後に通信があった時刻

    def send_message(self, msg):
        """
        ソケットに対してメッセージを送信する関数
        """
        # ソケットが存在しない場合は何もしない
        if self.sock is None:
            logger.error("ソケットが存在しないため、メッセージ送信をスキップします: %s:%s", self.ip, self.port)
            return
        try:
            self.sock.sendall(msg)
            self.last_activity = time.time()  # 送信成功時に最終アクティビティ時刻を更新
        except Exception as e:
            logger.error("送信エラー (%s:%s): %s", self.ip, self.port, e)

    def recv_message(self):
        """
        ソケットからメッセージを受信する関数
        """
        # ソケットが存在しない場合は何もしない
        if self.sock is None:
            logger.error("ソケットが存在しないため、メッセージ受信をスキップします: %s:%s", self.ip, self.port)
            return None
        try:
            # まず4バイトでメッセージ長を受信
            length_data = recv_all(self.sock, 4)
            if len(length_data) < 4:
                return None
            msg_length = struct.unpack("!I", length_data)[0]
            # メッセージ長が0の場合はkeep-alive
            if msg_length == 0:
                return {"id": None, "payload": None}
            # 残りのバイトを受信
            msg_data = recv_all(self.sock, msg_length)
            if len(msg_data) < msg_length:
                return None
            msg_id = msg_data[0]
            payload = msg_data[1:]
            self.last_activity = time.time()  # 受信成功時に最終アクティビティ時刻を更新
            return {"id": msg_id, "payload": payload}
        except Exception as e:
            logger.error("recv_message 中の例外 (%s:%s): %s", self.ip, self.port, e)
            return None

    def send_handshake(self):
        """
        ハンドシェイクメッセージを送信する関数
        ※ BitTorrent プロトコルのハンドシェイクは 68 バイト固定です。
        """
        pstr = b"BitTorrent protocol"
        pstrlen = len(pstr)
        reserved = b"\x00" * 8  # 予約領域（未使用）
        handshake = struct.pack("!B", pstrlen) + pstr + reserved + self.info_hash + self.peer_id
        self.send_message(handshake)
        logger.debug("ハンドシェイク送信 (%s:%s)", self.ip, self.port)

    def receive_handshake(self):
        """
        ハンドシェイクメッセージを受信し、内容を検証する関数
        :return: True（成功） / False（失敗）
        """
        handshake = recv_all(self.sock, 68)
        if len(handshake) < 68:
            return False
        pstrlen = handshake[0]
        pstr = handshake[1:1+pstrlen]
        if pstr != b"BitTorrent protocol":
            return False
        # 受信した info_hash と自分が送信した info_hash の比較
        received_info_hash = handshake[1+pstrlen+8:1+pstrlen+8+20]
        if received_info_hash != self.info_hash:
            return False
        logger.debug("有効なハンドシェイク受信 (%s:%s)", self.ip, self.port)
        return True

    def send_interested(self):
        """
        Interested メッセージ（ID=2）を送信する関数
        """
        msg = struct.pack("!Ib", 1, 2)  # 長さ 1 と ID=2
        self.send_message(msg)
        logger.debug("Interested 送信 (%s:%s)", self.ip, self.port)

    def send_request(self, piece_index, offset, length):
        """
        Request メッセージ（ID=6）を送信する関数
        :param piece_index: リクエストするピース番号
        :param offset: ピース内のオフセット
        :param length: リクエストするブロックの長さ
        """
        msg = struct.pack("!IbIII", 13, 6, piece_index, offset, length)
        self.send_message(msg)
        logger.debug("Request 送信: ピース %d, オフセット %d, 長さ %d (%s:%s)",
                     piece_index, offset, length, self.ip, self.port)

    def run(self):
        """
        ピアとの通信を行うメイン処理
        """
        max_retries = 3  # 接続の最大再試行回数
        attempt = 0
        while attempt < max_retries and self.running:
            try:
                # 接続前に短いランダム待機を実施
                time.sleep(random.uniform(0.1, 0.5))
                self.sock = socket.create_connection((self.ip, self.port), timeout=10)
                self.sock.settimeout(30)  # 受信タイムアウトを30秒に設定
                logger.info("ピア接続確立: %s:%s (試行 %d/%d)", self.ip, self.port, attempt+1, max_retries)
                self.send_handshake()
                if not self.receive_handshake():
                    raise Exception("ハンドシェイク失敗")
                break  # ハンドシェイク成功なら再試行ループを抜ける
            except Exception as e:
                attempt += 1
                logger.error("接続エラー (%s:%s): %s (試行 %d/%d)", self.ip, self.port, e, attempt, max_retries)
                if self.sock:
                    self.sock.close()
                    self.sock = None
                time.sleep(random.uniform(0.5, 1.5))
        if attempt >= max_retries or self.sock is None:
            logger.error("最大再試行回数に達またはソケットが取得できませんでした。接続を中止します: %s:%s", self.ip, self.port)
            return

        self.send_interested()
        consecutive_failures = 0  # 連続エラー回数のカウンター
        max_failures = 3  # 連続エラーが3回以上ならループを終了する
        while self.running:
            # ソケットがなくなっている場合はループを抜ける
            if self.sock is None:
                logger.error("ソケットがなくなったのでループを終了します: %s:%s", self.ip, self.port)
                break
            try:
                if time.time() - self.last_activity > KEEPALIVE_INTERVAL:
                    self.send_message(struct.pack("!I", 0))
                    logger.debug("keep-alive 送信 (%s:%s)", self.ip, self.port)
                msg = self.recv_message()
                if msg is None:
                    consecutive_failures += 1
                    logger.warning("受信エラー回数: %d (%s:%s)", consecutive_failures, self.ip, self.port)
                    if consecutive_failures >= max_failures:
                        logger.error("連続受信エラーにより接続を終了します: %s:%s", self.ip, self.port)
                        break
                    time.sleep(0.5)
                    continue
                else:
                    consecutive_failures = 0  # 受信成功時はカウンターをリセット

                # キープアライブの場合はmsg["id"]がNone
                if msg["id"] is None:
                    continue

                msg_id = msg["id"]
                if msg_id == 0:
                    self.choked = True
                    logger.debug("choke 受信 (%s:%s)", self.ip, self.port)
                elif msg_id == 1:
                    self.choked = False
                    logger.debug("unchoke 受信 (%s:%s)", self.ip, self.port)
                elif msg_id == 4:
                    piece_index = struct.unpack("!I", msg["payload"])[0]
                    logger.debug("have 受信: ピース %d (%s:%s)", piece_index, self.ip, self.port)
                elif msg_id == 5:
                    logger.debug("bitfield 受信 (%s:%s)", self.ip, self.port)
                elif msg_id == 7:
                    if len(msg["payload"]) < 8:
                        continue
                    piece_index = struct.unpack("!I", msg["payload"][:4])[0]
                    offset = struct.unpack("!I", msg["payload"][4:8])[0]
                    block_data = msg["payload"][8:]
                    logger.debug("piece 受信: ピース %d, オフセット %d, サイズ %d (%s:%s)",
                                piece_index, offset, len(block_data), self.ip, self.port)
                    self.piece_manager.mark_block_received(piece_index, offset, block_data)
                if not self.choked:
                    req = self.piece_manager.get_next_request()
                    if req is not None:
                        piece_index, offset, req_length = req
                        self.send_request(piece_index, offset, req_length)
            except Exception as e:
                logger.error("ピア通信中のエラー (%s:%s): %s", self.ip, self.port, e)
                break
        if self.sock:
            self.sock.close()
        logger.info("ピア接続終了: %s:%s", self.ip, self.port)
