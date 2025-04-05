import logging
import socket

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
