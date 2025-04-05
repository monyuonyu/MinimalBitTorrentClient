# Minimal BitTorrent クライアント

## 概要

このプロジェクトは、最小限の機能を備えた **BitTorrent クライアント** です。以下の主要な機能を実装しています。

- **torrent ファイルの解析**
- **トラッカーへの問い合わせとピア情報の取得**
- **ピアとのハンドシェイク**
- **Request / Piece メッセージによるピースのダウンロード**
- **SHA1 検証を伴うダウンロード完了の確認**
- **シングルファイル/マルチファイルの両方に対応**

> **注意**  
> この実装はサンプル的な最小機能版です。DHT や PEX、暗号化通信などの高度な機能は含まれていません。

---

## 使用方法

```bash
python client.py <torrentファイルのパス> [出力ディレクトリ]
```

- `<torrentファイルのパス>`  
  ダウンロード対象の `.torrent` ファイルを指定します。

- `[出力ディレクトリ]`（オプション）  
  ダウンロード完了後のファイル保存先を指定します。  
  省略した場合、カレントディレクトリに出力されます。

---

## 必要なライブラリ

このプログラムを実行するには、以下のライブラリが必要です。

- **Python 3.x**
- **[bencodepy](https://pypi.org/project/bencodepy/)**  
  torrent ファイルの解析に使用します。
- **[requests](https://pypi.org/project/requests/)**  
  トラッカーとの通信に使用します。

インストール方法：

```bash
pip install bencodepy requests
```

---

## ファイル構成

- **client.py**  
  メインのクライアントプログラムで、以下の処理を担当します。
  - torrent ファイルの読み込みと解析
  - トラッカーへの問い合わせ
  - ダウンロード管理
  - ファイル再構成

- **tracker.py**  
  トラッカーとの通信を担当します。

- **piece_manager.py**  
  ピースのダウンロード状態と検証を管理します。

- **peer.py**  
  ピアとの通信を担当します。

- **utils.py**  
  共通のユーティリティ関数を提供します。

- **constants.py**  
  プログラム全体で使用する定数を定義します。

---

## 特徴

- **モジュール化された設計**  
  - 各機能が独立したモジュールに分割されており、保守性が高いです。
- **詳細なログ出力**  
  - ログを通じて処理の流れを詳細に確認できます。
- **シングル/マルチファイル対応**  
  - 両方の形式のtorrentファイルに対応しています。
- **再試行ロジック**  
  - 接続失敗時の自動再試行機能を備えています。
