
// // ======= 設定ここから =======
// const settings = [
//     {
//         tableName: "Lysate",
//         excludeFields: [],
//         timelineLinkField: "Lysate" // time line テーブル側のリンクフィールド名
//     },
//     {
//         tableName: "Mice",
//         excludeFields: ["Birth_custom"],
//         timelineLinkField: "Mice"
//     },
//         {
//         tableName: "Histology",
//         excludeFields: ["Birth_custom"],
//         timelineLinkField: "Histology"
//     }
// ];

// const rename_map = {
//     "Injection date": "M1_Injection date",
//     "Tam start": "M2_Tam start",
//     "Tam end": "M3_Tam end",
//     "Biotin start": "M4_Biotin start",
//     "Biotin end": "M5_Biotin end",
//     "Sacrifice date":"M6_Sacrifice date",
//     "Lysate date": "L1_Lysate date",
//     "Biotin date": "L2_Biotin date",
//     "Purification date": "L3_Purification date",
//     "WB date": "L4_WB date",
//     "SYPRO Ruby date": "L5_SYPRO Ruby date",
//     "SliceDate": "H1_SliceDate",
//     "StainingDate": "H2_StainingDate",
//     "ImagingDate": "H3_ImagingDate",
// };

// //タイムラインを作るテーブル名
// const timelineTableName = "time line";
// const daysAgo = 120;
// // ======= 設定ここまで =======

// // 2) 外部ホストされた共通コードを fetch
// const url = "https://raw.githubusercontent.com/ylabjp/airtable_scripts/refs/heads/main/create_timeline.js";
// eval(await (await remoteFetchAsync(url)).text());

// // // 3) 取得したコードを評価し、実行
// // eval(code);
// // globalThis.runTimelineUpdate が定義されている想定
// await runTimelineUpdate({ settings, rename_map, timelineTableName, daysAgo:daysAgo});



// --- 完全版: runTimelineUpdate (Airtable Scripting API) ---
async function runTimelineUpdate({ settings, rename_map, timelineTableName, daysAgo = 30 }) {
  try {
    // basic validation
    if (!Array.isArray(settings) || settings.length === 0) {
      throw new Error("settings が空または配列ではありません。");
    }
    if (!timelineTableName || typeof timelineTableName !== "string") {
      throw new Error("timelineTableName が正しく指定されていません。");
    }

    // --- 1) Timeline テーブルが存在するか取得。なければ作成（まず date は含めず作成） ---
    let timelineTable;
    try {
      timelineTable = base.getTable(timelineTableName);
      output.markdown(`Found timeline table "${timelineTableName}"`);
    } catch (e) {
      output.markdown(`⚠️ テーブル "${timelineTableName}" が見つかりません。新規作成します...`);
      // createTableAsync に date オプションを渡すと環境により失敗するため、まずは date を含めず作成
      timelineTable = await base.createTableAsync(timelineTableName, [
        { name: "Name", type: "singleLineText" },
        { name: "Task type", type: "singleLineText" }
      ]);
      // createTableAsync 後は確実に最新オブジェクトを参照するため再取得
      timelineTable = base.getTable(timelineTableName);
      output.markdown(`Created timeline table "${timelineTableName}".`);
    }

    // helper: 常に最新のフィールド一覧を取る
    const refreshFields = () => base.getTable(timelineTableName).fields;
    let fields = refreshFields();
    output.markdown(`timeline fields: ${fields.map(f => f.name).join(", ")}`);

    // --- 2) Name フィールドの確認/追加 ---
    if (!fields.find(f => f.name === "Name")) {
      output.markdown(`⚠️ Name フィールドが無いため追加します。`);
      await base.getTable(timelineTableName).createFieldAsync("Name", "singleLineText");
      fields = refreshFields();
    }

    // --- 3) Task type の確認/追加 + 型チェック ---
    let taskTypeField = fields.find(f => f.name === "Task type");
    if (!taskTypeField) {
      output.markdown(`⚠️ Task type フィールドが無いため追加します。`);
      await base.getTable(timelineTableName).createFieldAsync("Task type", "singleLineText");
      fields = refreshFields();
      taskTypeField = fields.find(f => f.name === "Task type");
    }
    if (!taskTypeField) throw new Error('Task type フィールドが作成できませんでした。');
    if (taskTypeField.type !== "singleLineText") {
      throw new Error(`Task type フィールドの型が不正です（期待: singleLineText, 実際: ${taskTypeField.type}）。`);
    }

    // --- 4) Date の確認/自動作成（堅牢なトライロジック） ---
    let dateField = fields.find(f => f.name === "Date");
    if (!dateField) {
      output.markdown(`⚠️ Date フィールドが無いため追加します（複数パターンで options を試行）。`);

      // (A) 可能なら settings のいずれかのソーステーブルから sample options をコピー
      let sampleOptions = null;
      for (const s of settings) {
        if (!s.tableName) continue;
        try {
          const src = base.getTable(s.tableName);
          const sample = src.fields.find(f => (f.type === "date" || f.type === "dateTime") && f.options);
          if (sample && sample.options) {
            sampleOptions = sample.options;
            output.markdown(`サンプル options を ${s.tableName} から取得しました: ${JSON.stringify(sampleOptions)}`);
            break;
          }
        } catch (e) {
          // ソーステーブルが存在しない場合は早期に明示的エラーにする
          throw new Error(`設定のソーステーブル "${s.tableName}" が存在しません。settings を確認してください。`);
        }
      }

      // (B) 試す options 候補リスト（sampleOptions を先頭に置く）
      const attempts = [];
      if (sampleOptions) attempts.push(sampleOptions);
      // 複数の候補（環境差を考慮して列挙）
      attempts.push({ dateFormat: "local" });
      attempts.push({ dateFormat: { name: "local" } });
      attempts.push({ dateFormat: "ISO" });
      attempts.push({ format: "YYYY-MM-DD" });
      attempts.push({}); // 最後の手段として空オブジェクト（環境によってはこれで通る）
      // ※必要なら更に候補を追加できます

      let lastErr = null;
      for (const opt of attempts) {
        try {
          output.markdown(`Date を作成: options = ${JSON.stringify(opt)}`);
          await base.getTable(timelineTableName).createFieldAsync("Date", "date", opt);
          output.markdown(`✅ Date フィールド作成成功（options=${JSON.stringify(opt)}）。`);
          lastErr = null;
          break;
        } catch (e) {
          lastErr = e;
          output.markdown(`Date 作成試行で失敗（options=${JSON.stringify(opt)}）: ${e.message}`);
        }
      }

      if (lastErr) {
        const sampleHint = sampleOptions ? `コピー元 sampleOptions: ${JSON.stringify(sampleOptions)}` : '';
        throw new Error(
          `Date フィールドの自動作成に失敗しました。最後のエラー: ${lastErr.message}。手動で Date フィールドを追加するか ` +
          `既存の date フィールドの options を教えてください。 ${sampleHint}`
        );
      }

      // 作成後はリフレッシュして確認
      fields = refreshFields();
      dateField = fields.find(f => f.name === "Date");
      if (!dateField) {
        throw new Error("Date フィールドが作成されたようですが、フィールド一覧で確認できませんでした。");
      }
    } else {
      if (dateField.type !== "date") {
        throw new Error(`Date フィールドの型が不正です（期待: date, 実際: ${dateField.type}）。`);
      }
    }

    // --- 5) settings にある timelineLinkField が無ければ追加（リンク先は linkedTableId） ---
    fields = refreshFields();
    for (const s of settings) {
      if (!s.timelineLinkField || !s.tableName) {
        throw new Error(`settings のエントリに timelineLinkField または tableName がありません: ${JSON.stringify(s)}`);
      }
      if (!fields.find(f => f.name === s.timelineLinkField)) {
        output.markdown(`⚠️ リンクフィールド "${s.timelineLinkField}" を追加します（リンク先: ${s.tableName}）`);
        // ソーステーブルが存在するか確認
        let srcTable;
        try {
          srcTable = base.getTable(s.tableName);
        } catch (e) {
          throw new Error(`リンク元テーブル "${s.tableName}" が見つかりません。settings を確認してください。`);
        }
        await base.getTable(timelineTableName).createFieldAsync(s.timelineLinkField, "multipleRecordLinks", { linkedTableId: srcTable.id });
        fields = refreshFields();
      }
    }

    // --- 6) 既存タイムラインレコードを全削除（50件ずつのバッチ） ---
    output.markdown(`既存のタイムラインレコードを削除します（存在する場合）。`);
    const existingRecords = (await base.getTable(timelineTableName).selectRecordsAsync()).records;
    if (existingRecords.length > 0) {
      output.markdown(`既存レコード数: ${existingRecords.length} -> 削除を開始します。`);
      for (let i = 0; i < existingRecords.length; i += 50) {
        const batch = existingRecords.slice(i, i + 50);
        const ids = batch.map(r => r.id);
        await base.getTable(timelineTableName).deleteRecordsAsync(ids);
        output.markdown(`Deleted records ${i}..${i + ids.length - 1}`);
      }
    } else {
      output.markdown("既存レコードはありません。");
    }

    // --- 7) 新規作成データ収集 ---
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - daysAgo);
    const toCreate = [];

    for (const s of settings) {
      if (!s.tableName) throw new Error(`settings のエントリに tableName がありません: ${JSON.stringify(s)}`);
      const srcTable = base.getTable(s.tableName);
      if (!srcTable) throw new Error(`ソーステーブル "${s.tableName}" が取得できません。`);
      // date / dateTime フィールドを対象（excludeFields を除外）
      const exclude = Array.isArray(s.excludeFields) ? s.excludeFields : [];
      const dateFields = srcTable.fields.filter(f => (f.type === "date" || f.type === "dateTime") && !exclude.includes(f.name));
      output.markdown(`ソース ${s.tableName} の date フィールド: ${dateFields.map(f => f.name).join(", ")}`);

      const srcQuery = await srcTable.selectRecordsAsync();
      for (const rec of srcQuery.records) {
        for (const fld of dateFields) {
          const val = rec.getCellValue(fld);
          if (!val) continue;
          const dt = new Date(val);
          if (isNaN(dt.getTime())) continue;
          if (dt >= cutoff) {
            const dateStr = dt.toISOString().split("T")[0];
            // Defensive: timeline にリンクフィールドが存在するか確認
            fields = refreshFields();
            if (!fields.find(f => f.name === s.timelineLinkField)) {
              throw new Error(`タイムラインにリンクフィールド "${s.timelineLinkField}" が存在しません（想定外）。`);
            }

            toCreate.push({
              fields: {
                [s.timelineLinkField]: [{ id: rec.id }],
                "Task type": rename_map && rename_map[fld.name] ? rename_map[fld.name] : fld.name,
                "Date": dateStr,
                "Name": rec.name || "(no name)"
              }
            });
          }
        }
      }
    }

    output.markdown(`作成予定のレコード数: ${toCreate.length}`);

    // --- 8) レコード作成（50件ずつ） ---
    for (let i = 0; i < toCreate.length; i += 50) {
      const batch = toCreate.slice(i, i + 50);
      await base.getTable(timelineTableName).createRecordsAsync(batch);
      output.markdown(`Created batch ${i}..${i + batch.length - 1}`);
    }

    output.markdown("完了しました。");
    return { created: toCreate.length };
  } catch (err) {
    output.markdown(`❌ エラーが発生しました: ${err.message}`);
    console.error(err);
    throw err;
  }
}

// (エクスポート)
globalThis.runTimelineUpdate = runTimelineUpdate;
