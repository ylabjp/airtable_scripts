# 繁殖シミュレーションの週次運用（Claude 向けの手順）

Resource_Mice（Airtable base `appBwTnvAtEXHip0i`）の在庫と交配履歴から、今週金曜の交配リストと WT 購入リストを作り、
チャットで確認を取ってから Airtable の Mating テーブルに予定として登録する。ユーザーにはレポートを読ませず、チャットで完結させる。

## 1. 週次の実行
1. `pip install -r requirements.txt`
2. `python mating_simulation.py --auto`（約25分。長いのでバックグラウンドで実行し、終わるまで待つ）
   - 読み取りトークンは環境変数 `AIRTABLE_TOKEN`（クラウド環境のシークレット）。値を表示・保存・コミットしない
   - 見る期間は系統ごとに自動で決まる（年間欠品ペアの95%範囲の上側が `MAX_MISSING_PAIRS` 以下で、ケージ費が最も安い期間）
3. 出力は `results/<日時>/`：`proposal.json`（今週の交配・購入・見る期間の判定）と `report.html`
4. `report.html` を Artifact（非公開）として公開し、リンクを得る。Artifact が使えない環境なら、そのことを伝える

## 2. チャットへの報告
`proposal.json` から、次の順に短く書く。
1. 警告：見る期間の判定で `auto.kind == "shortage"` の系統があれば最初に書く（欠品の上限を満たせない）
2. 系統ごとの見る期間と、年間の欠品ペア・ケージ費（`groups[].auto.text` を要約）
3. 今週金曜（`mating_date`）の交配リスト：系統ごとに、オス × メス、実験用／維持用。購入予定の個体を使う交配はそう書く
4. WT 購入：`status` が「期限切れ」「今週中に発注」のものだけ（必要日・匹数）
5. レポートのリンク
6. 最後に「この交配を Airtable の Mating に予定として登録しますか？」と聞く

## 3. Airtable への登録（ユーザーが明示的に承認したときだけ）
1. `python apply_proposal.py results/<日時>/proposal.json --emit-json` で、登録するレコードを得る
   - 同じ交配日・同じオス・同じメスの Mating がすでにあるもの、Airtable にまだいない個体（購入予定・誕生予定）を使うものは除かれる
   - 交配日が過ぎていればエラーになる。その場合は登録せず、実行し直しを提案する
2. Airtable コネクタの `create_records_for_table` で、`records` の `fields` をそのまま Mating（`tbly0ddmHQ8AiFA76`）に作成する
   - 入れる項目：`scheduledDate` / `purposeStrain` / `purpose` / `fatherMouseId` / `motherMouseId` / `Note` のみ
   - `matingDate` は入れない（実際に交配したら担当者が入れる）。`indexInYear` は Airtable の自動化 MatingAddYear が採番する
3. 作成した件数と、登録しなかったもの（`skipped`）を報告する

## 守ること
- 承認なしに Airtable へ書き込まない。承認されたのは確認した `proposal.json` の内容だけ。再計算した結果を登録しない
- 書き込むのは Mating テーブルへの新規作成だけ。既存レコードの変更・削除はユーザーに具体的に頼まれたときだけ
- トークンを表示・コミットしない。`results/` はコミットしない
