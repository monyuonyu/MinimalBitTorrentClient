import os
import random
import time
import bencodepy
import hashlib
import string
from utils import logger
from tracker import TrackerClient
from piece_manager import PieceManager
from peer import PeerConnection

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
        
        # 初回のトラッカー問い合わせ
        tracker = TrackerClient(self.announce_url, self.info_hash, self.peer_id, self.port, self.left)
        self.peers = tracker.contact_tracker()
        logger.info("取得ピア数: %d", len(self.peers))
        
        # 既に接続済みのピアを管理するためのセットを用意
        self.existing_peers = set()
        
        # 初回取得したピア情報に対して接続スレッドを生成
        for ip, port in self.peers:
            self.existing_peers.add((ip, port))
            pc = PeerConnection(ip, port, self.info_hash, self.peer_id, self.piece_manager)
            pc.start()
            self.peer_connections.append(pc)
        
        # 進捗監視と再接続のロジック
        last_completed = 0
        stagnant_time = 0  # 進捗が停滞している時間（秒）
        try:
            while not self.piece_manager.is_complete():
                # 現在の完成ピース数をカウント
                completed = sum(1 for i in self.piece_manager.piece_info if self.piece_manager.piece_info[i]["complete"])
                percent = completed / self.piece_manager.total_pieces * 100
                logger.info("ダウンロード進捗: %d/%d ピース (%.2f%%)", 
                            completed, self.piece_manager.total_pieces, percent)
                
                # 進捗があった場合はカウンターをリセット
                if completed > last_completed:
                    last_completed = completed
                    stagnant_time = 0
                else:
                    stagnant_time += 5  # このループは約5秒間隔で実行
                
                # 30秒以上進捗がなかった場合は、再接続を試みる
                if stagnant_time >= 30:
                    logger.info("進捗が停滞しているため、トラッカーから再度ピア情報を取得します。")
                    new_tracker = TrackerClient(self.announce_url, self.info_hash, self.peer_id, self.port, self.left)
                    new_peers = new_tracker.contact_tracker()
                    for ip, port in new_peers:
                        # 重複しないピアだけ新たに接続する
                        if (ip, port) not in self.existing_peers:
                            self.existing_peers.add((ip, port))
                            pc = PeerConnection(ip, port, self.info_hash, self.peer_id, self.piece_manager)
                            pc.start()
                            self.peer_connections.append(pc)
                    stagnant_time = 0  # 再問い合わせ後、停滞時間をリセット
                
                time.sleep(5)
        except KeyboardInterrupt:
            logger.warning("ユーザーによる中断")
        finally:
            # 全ピア接続スレッドの終了フラグをセット
            for pc in self.peer_connections:
                pc.running = False
            # すべてのピースが揃っていれば、ファイルの組み立てを実施
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
