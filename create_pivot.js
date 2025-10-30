// keyWordTableName = 'Plasmid_name'
// resourceTableName = 'Plasmid'
// 
async function runCreateVari({ keyWordTableName, resourceTableName, updateTableName}) {
    let keyWordTable = base.getTable(keyWordTableName);
    let querykeyWordTable = await keyWordTable.selectRecordsAsync();

    let resourceTable = base.getTable(resourceTableName);
    let queryResourceTable = await resourceTable.selectRecordsAsync();

    let targetNames = querykeyWordTable.records.map(record => record.getCellValue('keyword')).filter(Boolean);

    let data = queryResourceTable.records.map(record => {
        let row = {
            'No': record.getCellValue('No'),
            'Name': record.getCellValue('Name'),
        };
        
        targetNames.forEach(targetName => {
            const name = (record.getCellValue('Name') || '').toString().toLowerCase();
            const lowercaseTargetName = targetName.toLowerCase().trim();
            const result = name.includes(lowercaseTargetName);
            row[targetName] = result
            // console.log('-----');
            // console.log('Name:', name);
            // console.log('Target Name:', lowercaseTargetName);
            // console.log('Result:', result);
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
        // {name: 'Status', type: 'singleSelect'},
        // {name: 'Deriveration', type: 'singleLineText'},
        // {name: 'SequenceDate', type: 'date', options : {
        //     "dateFormat":{
        //         "format": "YYYY-MM-DD",
        //         "name": "iso"
        //     }
        // }},
        // {name: 'E Coli', type: 'singleSelect'},
        // {name: 'Note', type: 'singleLineText'},
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

        try{
            newTable = base.getTable(updateTableName);

            // 既存のテーブルのフィールドを更新
            let existingFields = newTable.fields;
            let fieldsToCreate = fields.filter(field => !existingFields.find(f => f.name === field.name));
            let fieldsToDelete = existingFields.filter(field => !fields.find(f => f.name === field.name) && field.name !== 'Name');


            // // 不要なフィールドを削除 (Nameフィールドは削除しない)
            // for (let field of fieldsToDelete) {
            //     // await newTable.deleteFieldAsync(field);
            //     await newTable.updateFieldAsync(field, {hidden: true});
            // }

            // 新しいフィールドを作成
            // await newTable.createFieldAsync(fieldsToCreate);
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

            // await newTable.deleteRecordsAsync(existingRecords.records);
            // await newTable.createRecordsAsync(data.map(row => ({fields: row})));
        }catch(error1){
            console.log(error1)
            // テーブルが存在しない場合、新規作成
            console.log(fields)
            // newTable = await base.createTableAsync(updateTableName, [{name: "Name", type: "singleLineText"}, {name: "aaa", type: "checkbox"}]);
            newTable = await base.createTableAsync(updateTableName, fields);
            newTable = base.getTable(updateTableName);

            // データを50個ずつのバッチに分割
            let batches = chunkArray(data, 50);
            for (let batch of batches) {
                await newTable.createRecordsAsync(batch.map(row => ({fields: row})));
            }
            // await newTable.createRecordsAsync(data.map(row => ({fields: row})));
        }

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