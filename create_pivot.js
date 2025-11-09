/* スクリプト仕様
引数
1. keyWordTableName:マトリックスの列項目名のテーブル名
 - このテーブルの”keyword”列がマトリックスの列項目となる。
 - ”keyword”列がなければエラー。
2. resourceTableName:マトリックスの各行に当たる
 - 引数4:resourceNo, 引数5:resourceNameの2列の情報は、マトリックスに“No”列、”Name”列として各行にコピーされる。
　resourceNo、resourceNameに該当するがなければエラー。
 - このテーブルの”Name”列に各keywordが含まれているか否かがマトリックスの要素になる。
3. updateTableName:マトリックスのテーブル名
 - テーブルが存在しなければ新規作成
4. resourceNo:resourceTableの中でキーとなる列名
 - マトリックスに“No”列として各行にコピーされる。
 - この引数に指定した列名がresourceTableに存在しなければエラー。
5. resourceName:resourceTableの中で名称となる列名
 - マトリックスに“Name”列として各行にコピーされる。
 - この引数に指定した列名がresourceTableに存在しなければエラー。
*/

/* airtableのScriptサンプル
// 1) パラメータ設定
const keyWordTableName = "Plasmid_name_test";
const resourceTableName = "Plasmid";
const updateTableName = "Plasmid_test";
const resourceNo = "No"
const resourceName = "Name"
// ======= 設定ここまで =======

// 2) 外部ホストされた共通コードを fetch
const url = "https://gist.githubusercontent.com/reoreo2/41b9fe9c688883b2f4b7f55e24d67237/raw/gistfile1.txt";

// 3) コードを取得し、評価
eval(await (await remoteFetchAsync(url)).text());

// 4) 実行
await runCreateVari ({keyWordTableName, resourceTableName, updateTableName , resourceNo, resourceName});
*/

async function runCreateVari({ keyWordTableName, resourceTableName, updateTableName, resourceNo, resourceName}) {
    let keyWordTable = base.getTable(keyWordTableName);
    let querykeyWordTable = await keyWordTable.selectRecordsAsync();

    let resourceTable = base.getTable(resourceTableName);
    let queryResourceTable = await resourceTable.selectRecordsAsync();

    let targetNames = querykeyWordTable.records.map(record => record.getCellValue('keyword')).filter(Boolean);

    let data = queryResourceTable.records.map(record => {
        let row = {
            'No': record.getCellValue(resourceNo),
            'Name': record.getCellValue(resourceName),
        };
        
        targetNames.forEach(targetName => {
            const name = (record.getCellValue(resourceName) || '').toString().toLowerCase();
            const lowercaseTargetName = targetName.toLowerCase().trim();
            const result = name.includes(lowercaseTargetName);
            row[targetName] = result
        });
        
        return row;
    });
    console.log(data)


    // 新規テーブルの作成または既存テーブルの更新
    let newTable;

    // フィールドの定義
    let fields = [
        {name: 'No', type: 'number', options: {
            "precision" : 0
        }},
        {name: 'Name', type: 'singleLineText'},
        ...targetNames.map(name => ({name, type: 'checkbox', options : {
            "color": "greenBright",
            "icon": "check"
        },}))
    ];
    async function createOrUpdateTable() {

        let existResultTable = false
        for (let table of base.tables) {
            if(table.name === updateTableName){
                existResultTable = true
                break
            }
        }

        // try{

            // 更新先テーブル名が現在のBase内のテーブルに存在するか確認
            const isExist = base.tables.some(table => table.name === updateTableName);

            if(isExist){
                newTable = base.getTable(updateTableName);
                // 既存のテーブルのフィールドを更新
                let existingFields = newTable.fields;
                let fieldsToCreate = fields.filter(field => !existingFields.find(f => f.name === field.name));
                let fieldsToDelete = existingFields.filter(field => !fields.find(f => f.name === field.name) && field.name !== 'Name');

                for (let field of fieldsToCreate) {
                    await newTable.createFieldAsync(field.name, field.type, field.options);
                }

                // 既存のレコードを削除
                let existingRecords = await newTable.selectRecordsAsync();
                console.log("existingRecordsSize", existingRecords)
                let existingRecordBatches = await chunkArray(existingRecords.recordIds, 50);
                console.log("existingRecordBatches", existingRecordBatches)
                for (let batch of existingRecordBatches) {
                    await newTable.deleteRecordsAsync(batch);
                }

                // レコード再追加
                let batches = chunkArray(data, 50);
                for (let batch of batches) {
                    await newTable.createRecordsAsync(batch.map(row => ({fields: row})));
                }
            }else{
                newTable = await base.createTableAsync(updateTableName, fields);
                newTable = base.getTable(updateTableName);

                // データを50個ずつのバッチに分割
                let batches = chunkArray(data, 50);
                for (let batch of batches) {
                    const row = batch.map(row => ({fields: row}))
                    console.log('row',row)
                    await newTable.createRecordsAsync(row);
                }
            }

            // await newTable.deleteRecordsAsync(existingRecords.records);
            // await newTable.createRecordsAsync(data.map(row => ({fields: row})));
        // }catch(error1){
        //     console.log(error1)
        //     // テーブルが存在しない場合、新規作成
        //     console.log(fields)
        //     // newTable = await base.createTableAsync(updateTableName, [{name: "Name", type: "singleLineText"}, {name: "aaa", type: "checkbox"}]);
        //     newTable = await base.createTableAsync(updateTableName, fields);
        //     newTable = base.getTable(updateTableName);

        //     // データを50個ずつのバッチに分割
        //     let batches = chunkArray(data, 50);
        //     for (let batch of batches) {
        //         const row = batch.map(row => ({fields: row}))
        //         console.log('row',row)
        //         await newTable.createRecordsAsync(row);
        //     }
        //     // await newTable.createRecordsAsync(data.map(row => ({fields: row})));
        // }

        return true

    }

    let success = await createOrUpdateTable();

    if (success) {
        output.markdown(`# Results saved to table: ${updateTableName}`);
        output.markdown(`Created/updated ${data.length} records.`);
        output.markdown(`Table fields have been updated to match the current data structure.`);
    } else {
        output.markdown(`# Error: Unable to create or update table ${updateTableName}`);
    }

    // データを50個ずつのバッチに分割する関数
    function chunkArray(array, size) {
        const chunks = [];
        for (let i = 0; i < array.length; i += size) {
            chunks.push(array.slice(i, i + size));
        }
        return chunks;
    }

}
globalThis.runCreateVari = runCreateVari;