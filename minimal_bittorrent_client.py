#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【Minimal BitTorrent クライアント】
---------------------------------------------------------------------------
このプログラムは、torrent ファイルから情報を読み込み、
トラッカーに問い合わせてピア情報を取得し、複数のピアと同時に
通信を行いながらファイルのピースを取得、SHA1 検証を実施し、
最終的にシングルファイルまたはマルチファイルとして出力する BitTorrent クライアントです。

【主な機能】
・torrent ファイルの解析と info_hash の計算
・トラッカーへの問い合わせによるピア情報の取得
・各ピアとのハンドシェイク、Interested、Request、Piece の通信
・ブロック単位でのダウンロード、SHA1 による検証
・シングルファイル／マルチファイル torrent の両方に対応したファイル再構成

【注意点】
・DHT、PEX、暗号化通信、高度なピース選択アルゴリズムなどは未実装です。
・ネットワーク環境、タイムアウト設定などにより動作が左右される場合があります。
---------------------------------------------------------------------------  
"""

import socket
import threading
import struct
import hashlib
import random
import string
import time
import urllib.parse
import bencodepy    # torrent ファイル解析用ライブラリ（pip install bencodepy）
import requests
import logging
import os

# -----------------------------------------------------------
# 定数の定義
# -----------------------------------------------------------
BLOCK_SIZE = 16384              # 1ブロックのサイズ（16KB）
KEEPALIVE_INTERVAL = 120        # ピアとの接続が一定時間無通信の場合に送信する keep-alive の間隔（秒）

# -----------------------------------------------------------
# ログ出力の設定（INFO レベル以上のログを標準出力へ表示）
# -----------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# 関数：recv_all
# 概要：
#   ソケットから指定バイト数を必ず受信するための関数です。
#   途中で接続が切れた場合は、受信したデータを返します。
# 引数：
#   sock : 対象となるソケットオブジェクト
#   n    : 受信するバイト数
# 戻り値：
#   受信したバイト列
# -----------------------------------------------------------
def recv_all(sock, n):
    data = b""
    # 指定バイト数に達するまで繰り返し受信
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except Exception as e:
            logger.error("recv_all 中の例外: %s", e)
            break
        if not chunk:
            break
        data += chunk
    return data

# -----------------------------------------------------------
# クラス：TrackerClient
# 概要：
#   トラッカーサーバへ問い合わせ、torrent ファイルに記述されたトラッカー URL
#   に対してピア情報を取得します。
# -----------------------------------------------------------
class TrackerClient:
    def __init__(self, announce_url, info_hash, peer_id, port, left):
        """
        初期化処理
        :param announce_url: トラッカー URL（文字列）
        :param info_hash: torrent の info_hash（20バイトのバイナリ）
        :param peer_id: 自身のピアID（20バイトのバイナリ）
        :param port: 自身が利用するポート番号（整数）
        :param left: ダウンロードすべき総バイト数（整数）
        """
        self.announce_url = announce_url
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.port = port
        self.left = left

    def contact_tracker(self):
        """
        トラッカーに問い合わせ、ピア一覧を取得するメソッド
        ※ URL のパラメータについて、info_hash と peer_id は手動でエンコードして使用します。
        :return: ピア情報のリスト [(ip, port), ...]
        """
        # -----------------------------------------------------------
        # ブロックコメント：
        #   info_hash と peer_id はバイナリデータなので、URL 内に正しく含めるために
        #   urllib.parse.quote を使ってシングルエンコードを行います。
        # -----------------------------------------------------------
        encoded_info_hash = urllib.parse.quote(self.info_hash, safe='')
        encoded_peer_id = urllib.parse.quote(self.peer_id, safe='')
        # URL のパラメータを手動で連結して正しい形式の URL を生成する
        url = (
            f"{self.announce_url}"
            f"?info_hash={encoded_info_hash}"
            f"&peer_id={encoded_peer_id}"
            f"&port={self.port}"
            f"&uploaded=0"
            f"&downloaded=0"
            f"&left={self.left}"
            f"&compact=1"
            f"&numwant=200"
        )
        try:
            logger.info("トラッカーに問い合わせ中: %s", url)
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                logger.error("トラッカーエラー: ステータスコード %d", response.status_code)
                return []
            # bencodepy によりレスポンス内容をデコード
            tracker_response = bencodepy.decode(response.content)
            # バイト列のキーを文字列に変換する
            tracker_response = { key.decode('utf-8') if isinstance(key, bytes) else key: value
                                 for key, value in tracker_response.items() }
            # トラッカーからエラー応答があればログ出力
            if "failure reason" in tracker_response:
                logger.error("トラッカーからエラー応答: %s", tracker_response["failure reason"])
                return []
            peers = tracker_response.get("peers")
            peer_list = []
            # コンパクト形式の場合：6 バイトごとに IP とポートの情報が含まれる
            if isinstance(peers, bytes):
                for i in range(0, len(peers), 6):
                    ip_bytes = peers[i:i+4]
                    port_bytes = peers[i+4:i+6]
                    ip = '.'.join(str(b) for b in ip_bytes)
                    port = int.from_bytes(port_bytes, byteorder='big')
                    peer_list.append((ip, port))
            # 非コンパクト形式の場合：リスト形式になっている可能性がある
            elif isinstance(peers, list):
                for p in peers:
                    ip = p.get(b"ip") if b"ip" in p else p.get("ip")
                    port = p.get(b"port") if b"port" in p else p.get("port")
                    if isinstance(ip, bytes):
                        ip = ip.decode('utf-8')
                    peer_list.append((ip, port))
            logger.info("トラッカーから %d 件のピア情報取得", len(peer_list))
            return peer_list
        except Exception as e:
            logger.error("トラッカー問い合わせエラー: %s", e)
            return []

# -----------------------------------------------------------
# クラス：PieceManager
# 概要：
#   torrent の各ピースのダウンロード状況、ブロックごとの受信状態、
#   SHA1 による検証、及び完成したピースのデータ保持を管理します。
# -----------------------------------------------------------
class PieceManager:
    def __init__(self, info):
        """
        初期化処理
        :param info: torrent の info セクション（bencode 済み、キーは bytes）
        """
        self.info = info
        self.pieces_hash = info[b"pieces"]  # 各ピースの SHA1 ハッシュが連結されたバイト列
        self.piece_length = info[b"piece length"]
        self.total_pieces = len(self.pieces_hash) // 20
        
        # 単一ファイルの場合は "length"、マルチファイルの場合は "files" キーを使用して合計サイズを求める
        if b"length" in info:
            self.total_length = info[b"length"]
        else:
            # マルチファイルの場合は、各ファイルの長さを合計する
            self.total_length = sum(f[b"length"] for f in info[b"files"])
        
        # 各ピースの状態を保持する辞書
        self.piece_info = {}
        # 完成したピースのデータを保持する辞書（キー: ピース番号、値: バイナリデータ）
        self.completed_pieces = {}
        # 各ピースごとに、期待されるサイズ、受信済みブロック、既にリクエスト済みのオフセット、完了フラグを設定
        for i in range(self.total_pieces):
            if i < self.total_pieces - 1:
                expected = self.piece_length
            else:
                # 最終ピースはファイルサイズがピースサイズと異なる場合がある
                expected = self.total_length - self.piece_length * (self.total_pieces - 1)
            self.piece_info[i] = {
                "expected_length": expected,
                "blocks": {},   # オフセットごとに受信したブロックを格納
                "requested": set(),  # 既にリクエスト済みのオフセット
                "complete": False    # ピースが完成しているかのフラグ
            }
        # スレッドセーフな処理のためのロック
        self.lock = threading.Lock()

    def get_next_request(self):
        """
        未取得ブロックの中から、次にリクエストすべきブロック情報を返す
        :return: (piece_index, offset, length) のタプル、または None（全ブロック取得済みの場合）
        """
        with self.lock:
            # 全ピースを順番に確認
            for i in range(self.total_pieces):
                if not self.piece_info[i]["complete"]:
                    expected = self.piece_info[i]["expected_length"]
                    # 0 から expected まで BLOCK_SIZE ごとに確認
                    for offset in range(0, expected, BLOCK_SIZE):
                        if offset not in self.piece_info[i]["blocks"] and offset not in self.piece_info[i]["requested"]:
                            # リクエスト済みとしてセットに追加
                            self.piece_info[i]["requested"].add(offset)
                            req_len = min(BLOCK_SIZE, expected - offset)
                            return (i, offset, req_len)
        return None

    def mark_block_received(self, piece_index, offset, data):
        """
        受信したブロックを記録し、ブロックが全てそろった場合に SHA1 で検証する
        :param piece_index: ピース番号（整数）
        :param offset: ブロックのオフセット（整数）
        :param data: 受信したデータ（バイナリ）
        """
        with self.lock:
            info = self.piece_info[piece_index]
            info["blocks"][offset] = data
            # 現在受信した総バイト数を計算
            total_received = sum(len(block) for block in info["blocks"].values())
            if total_received >= info["expected_length"]:
                # オフセット順にブロックを結合して一つのピースデータにする
                piece_data = b"".join(info["blocks"][o] for o in sorted(info["blocks"].keys()))
                expected_hash = self.pieces_hash[piece_index*20:(piece_index+1)*20]
                actual_hash = hashlib.sha1(piece_data).digest()
                if actual_hash == expected_hash:
                    logger.info("ピース %d の検証成功", piece_index)
                    info["complete"] = True
                    # 完成したピースのデータを保持
                    self.completed_pieces[piece_index] = piece_data
                else:
                    logger.warning("ピース %d の検証失敗。再取得します。", piece_index)
                    # ハッシュ検証失敗時は、ブロック情報をリセットして再度リクエスト可能にする
                    info["blocks"] = {}
                    info["requested"] = set()

    def is_complete(self):
        """
        全ピースが完成しているかどうかを返す
        :return: True（全ピース完了） / False（未完了のピースがある）
        """
        with self.lock:
            return all(info["complete"] for info in self.piece_info.values())

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

# -----------------------------------------------------------
# クラス：TorrentClient
# 概要：
#   torrent ファイルの読み込み、トラッカー問い合わせ、複数ピアとの通信開始、
#   ダウンロード進捗の監視、及びファイルの組み立て処理（シングル／マルチファイル対応）を行います。
# -----------------------------------------------------------
class TorrentClient:
    def __init__(self, torrent_file, output_dir="."):
        """
        初期化処理
        :param torrent_file: torrent ファイルのパス（文字列）
        :param output_dir: ダウンロード完了後の出力先ディレクトリ（文字列）
        """
        self.torrent_file = torrent_file
        self.output_dir = output_dir
        self.meta_info = None    # torrent ファイルの全情報（bencode 済み）
        self.info_hash = None    # info セクションの SHA1 ハッシュ
        self.peer_id = None      # 自身のピアID
        self.announce_url = None # トラッカー URL
        self.left = 0            # ダウンロードすべき総バイト数
        self.piece_manager = None
        self.peers = []          # トラッカーから取得したピア情報のリスト
        self.peer_connections = []  # ピアとの接続スレッドのリスト
        # 自身が利用するポートはランダムに選択
        self.port = random.randint(10000, 60000)

    def load_torrent(self):
        """
        torrent ファイルを読み込み、必要な情報を初期化するメソッド
        ・bencodepy によりファイル内容をデコード
        ・announce URL, info_hash, peer_id, ダウンロード総サイズなどを設定
        """
        try:
            with open(self.torrent_file, "rb") as f:
                torrent_data = f.read()
            self.meta_info = bencodepy.decode(torrent_data)
            # バイトキーを文字列に変換する
            self.meta_info = { key.decode("utf-8") if isinstance(key, bytes) else key: value
                               for key, value in self.meta_info.items() }
            announce = self.meta_info.get("announce")
            self.announce_url = announce.decode("utf-8") if isinstance(announce, bytes) else announce
            info = self.meta_info.get("info")
            if info is None:
                raise Exception("torrent ファイルに info セクションがありません")
            # info セクションを bencode し、その結果から SHA1 ハッシュを計算
            bencoded_info = bencodepy.encode(info)
            self.info_hash = hashlib.sha1(bencoded_info).digest()
            # ランダムな文字列からピアIDを生成（20文字）
            self.peer_id = ''.join(random.choices(string.ascii_letters + string.digits, k=20)).encode("utf-8")
            # 単一ファイルの場合は "length"、マルチファイルの場合は "files" を用いて総サイズを計算
            if b"length" in info:
                self.left = info[b"length"]
            elif b"files" in info:
                self.left = sum(f[b"length"] for f in info[b"files"])
            else:
                self.left = 0
            # PieceManager のインスタンスを生成
            self.piece_manager = PieceManager(info)
            logger.info("torrent ファイル読み込み完了")
        except Exception as e:
            logger.error("torrent ファイル読み込みエラー: %s", e)
            raise

    def assemble_single_file(self):
        """
        シングルファイル torrent の場合、全ピースのデータを結合して
        一つのファイルとして保存する処理
        """
        info = self.meta_info.get("info")
        if b"name" in info:
            filename = info[b"name"].decode("utf-8")
        elif "name" in info:
            filename = info["name"]
        else:
            filename = "output.dat"
        output_path = os.path.join(self.output_dir, filename)
        logger.info("ダウンロード完了。ファイル組み立て中: %s", output_path)
        try:
            with open(output_path, "wb") as f:
                for i in range(self.piece_manager.total_pieces):
                    piece_data = self.piece_manager.completed_pieces.get(i)
                    if piece_data is None:
                        raise Exception(f"ピース {i} が欠損")
                    f.write(piece_data)
            logger.info("ファイル保存完了: %s", output_path)
        except Exception as e:
            logger.error("ファイル保存エラー: %s", e)

    def assemble_multi_file(self):
        """
        マルチファイル torrent の場合、全ピースのデータを連結した後、
        各ファイルごとに必要な長さに切り分けて保存する処理
        """
        info = self.meta_info.get("info")
        if b"name" in info:
            base_name = info[b"name"].decode("utf-8")
        elif "name" in info:
            base_name = info["name"]
        else:
            base_name = "output_dir"
        output_dir = os.path.join(self.output_dir, base_name)
        os.makedirs(output_dir, exist_ok=True)
        logger.info("ダウンロード完了。マルチファイル組み立て中: %s", output_dir)
        try:
            # 全ピースのデータを順番に連結
            full_data = b""
            for i in range(self.piece_manager.total_pieces):
                piece_data = self.piece_manager.completed_pieces.get(i)
                if piece_data is None:
                    raise Exception(f"ピース {i} が欠損")
                full_data += piece_data
            # 各ファイルに対して、指定された長さ分を切り出して保存
            files = info[b"files"] if b"files" in info else info["files"]
            offset = 0
            for f in files:
                length = f[b"length"]
                # ファイルパスは、path（リスト）を結合して生成
                path_components = [comp.decode("utf-8") if isinstance(comp, bytes) else comp for comp in f[b"path"]]
                file_path = os.path.join(output_dir, *path_components)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as fp:
                    fp.write(full_data[offset:offset+length])
                logger.info("ファイル保存完了: %s", file_path)
                offset += length
        except Exception as e:
            logger.error("マルチファイル組み立てエラー: %s", e)

    def start(self):
        """
        torrent 処理のメイン処理を開始するメソッド
        ・torrent ファイルの読み込み
        ・トラッカー問い合わせによるピア情報の取得
        ・各ピアとの通信スレッドを起動
        ・ダウンロード進捗の監視
        ・ダウンロード完了後のファイル組み立て
        """
        self.load_torrent()
        tracker = TrackerClient(self.announce_url, self.info_hash, self.peer_id, self.port, self.left)
        self.peers = tracker.contact_tracker()
        logger.info("取得ピア数: %d", len(self.peers))
        # 各ピアとの接続スレッドを生成して起動する
        for ip, port in self.peers:
            pc = PeerConnection(ip, port, self.info_hash, self.peer_id, self.piece_manager)
            pc.start()
            self.peer_connections.append(pc)
        # ダウンロード進捗を監視するループ
        try:
            while not self.piece_manager.is_complete():
                completed = sum(1 for i in self.piece_manager.piece_info if self.piece_manager.piece_info[i]["complete"])
                percent = completed / self.piece_manager.total_pieces * 100
                logger.info("ダウンロード進捗: %d/%d ピース (%.2f%%)", completed, self.piece_manager.total_pieces, percent)
                time.sleep(5)
        except KeyboardInterrupt:
            logger.warning("ユーザーによる中断")
        finally:
            # 全てのピア接続スレッドに対して終了フラグをセット
            for pc in self.peer_connections:
                pc.running = False
            # ダウンロードが完了している場合、ファイルの組み立てを実施
            if self.piece_manager.is_complete():
                info = self.meta_info.get("info")
                if b"files" in info or "files" in info:
                    self.assemble_multi_file()
                else:
                    self.assemble_single_file()
            else:
                logger.error("ダウンロードが完全に完了しませんでした")

# -----------------------------------------------------------
# メイン処理
# -----------------------------------------------------------
if __name__ == "__main__":
    """
    プログラムのエントリーポイント
    コマンドライン引数で torrent ファイルのパスと、出力ディレクトリ（任意）を受け取ります。
    使用例：
        python simpe_bittorrent_client.py <torrentファイル> [出力ディレクトリ]
    """
    import sys
    if len(sys.argv) < 2:
        print("使用方法: python simple_bittorrent_client.py <torrentファイル> [出力ディレクトリ]")
        sys.exit(1)
    torrent_file = sys.argv[1]
    output_directory = sys.argv[2] if len(sys.argv) >= 3 else "."
    client = TorrentClient(torrent_file, output_directory)
    client.start()
