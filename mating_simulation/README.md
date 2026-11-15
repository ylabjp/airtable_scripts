# 繁殖シミュレーション（mating_simulation）

Airtable の Resource_Mice（在庫マウス・交配履歴・実験計画）から、毎週の交配リストと WT メスの購入リストを作るしくみです。
毎週木曜 10:00 に Claude がクラウドで自動実行し、結果をチャットで報告します。承認した交配だけが Mating に予定として登録されます。

**本体（コード・利用ガイド・Claude 向けの手順）は `mating-simulation` ブランチにあります。** main にはこの案内だけを置いています。

- 一式（ここから開く）: https://github.com/ylabjp/airtable_scripts/tree/mating-simulation/mating_simulation
- 利用ガイド（初回の準備・毎週の使い方・困ったとき）: https://github.com/ylabjp/airtable_scripts/blob/mating-simulation/mating_simulation/GUIDE.md
- 利用ガイド（Claude Docs 版・コメント可）: https://claude.ai/artifact/3SaqZzaFBbs9TDqAaFxyyR
- 仕様書（計算の前提・結果）: https://claude.ai/artifact/25Erd2HEDtPGW7p9dwpa7q
