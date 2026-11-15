# 繁殖シミュレーション（mating_simulation）

Airtable の Resource_Mice（在庫マウス・交配履歴・実験計画）から、52週分の交配と在庫を週ごとに再現し、次を出します。

- 今週金曜の交配リスト（どのオスとどのメスを交配するか）
- WT メスの購入リスト（必要日と発注期限）
- 系統ごとの「何ヶ月先の動けるマウスを数えるか（見る期間）」の自動判定と、欠品・ケージ費の比較

## 運用
毎週、Claude Code のクラウドで実行し、結果をチャットで確認してから Airtable の Mating に予定として登録します。
手順は [CLAUDE.md](CLAUDE.md) にあります（Claude はこれに従って動きます）。

- 読み込み：Python が Airtable の REST API で読む（読み取り専用トークンを環境変数 `AIRTABLE_TOKEN` に設定）
- 書き込み：Claude が Airtable コネクタで Mating に登録する（書き込み用のトークンは使わない）

## 手元で動かす場合
```
pip install -r requirements.txt
python mating_simulation.py --auto          # 見る期間を系統ごとに自動で決める（約25分）
python mating_simulation.py --runs 0        # 交配リストだけ（速い）
python apply_proposal.py results/<日時>/proposal.json   # 登録する内容の表示（書き込まない）
```
トークンは環境変数 `AIRTABLE_TOKEN`、またはこのフォルダの `.env`（`AIRTABLE_TOKEN=...`）に置きます。`.env` はコミットしません。
画面から使う場合は `start_webapp.bat`（`python webapp.py`）。

## ファイル
- `mating_simulation.py`：シミュレーション本体。出力は `results/<日時>/`（report.html, proposal.json, CSV）
- `apply_proposal.py`：proposal.json から Mating に登録するレコードを作る（重複・未登録個体を除く）
- `webapp.py` / `start_webapp.bat`：手元で条件を選んで実行する画面
- `simulation_spec.md`：最初の仕様（依頼時のもの）
