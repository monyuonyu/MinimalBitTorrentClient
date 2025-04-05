import hashlib
import threading
from utils import logger

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
