import urllib.parse
import requests
import bencodepy
from utils import logger

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
