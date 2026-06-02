// ============================================================
// 武樂運動空間｜課程許願池 - Google Apps Script
// ============================================================
// 使用步驟：
// 1. 建立一份 Google 試算表，複製網址 /d/ 後面的試算表 ID
// 2. 在試算表選單：延伸功能 → Apps Script
// 3. 貼上此檔案內容，將 SPREADSHEET_ID 換成你的試算表 ID
// 4. 部署 → 新增部署作業 → 類型選「網頁應用程式」
//    - 執行身分：「我」
//    - 存取對象：「所有人」
// 5. 複製 Web App URL，貼到 index.html 和 dashboard.html 的 API_ENDPOINT
// ============================================================

const SPREADSHEET_ID = '請貼上你的試算表 ID';
const SHEET_NAME = '課程許願池';

const HEADERS = [
  'submitted_at',
  'name',
  'line_name',
  'phone',
  'member_status',
  'wish_categories',
  'wish_course_name',
  'wish_reason',
  'needs',
  'preferred_times',
  'ideal_time',
  'frequency',
  'preferred_plan',
  'acceptable_price',
  'notify_methods',
  'note',
  'source',
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'status'
];

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents || '{}');
    const sheet = getSheet_();
    const row = HEADERS.map((key) => normalizeValue_(data[key]));

    if (!row[0]) row[0] = new Date().toISOString();
    if (!row[20]) row[20] = '未回覆';

    sheet.appendRow(row);
    return output_({ status: 'success' });
  } catch (err) {
    return output_({ status: 'error', message: err.message });
  }
}

function doGet(e) {
  try {
    const records = readRecords_();
    const payload = { status: 'success', records: records };
    const callback = e && e.parameter && e.parameter.callback;

    if (callback) {
      return ContentService
        .createTextOutput(`${callback}(${JSON.stringify(payload)})`)
        .setMimeType(ContentService.MimeType.JAVASCRIPT);
    }

    return output_(payload);
  } catch (err) {
    return output_({ status: 'error', message: err.message });
  }
}

function getSheet_() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(SHEET_NAME);

  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.getRange(1, 1, 1, HEADERS.length)
      .setBackground('#D96B2A')
      .setFontColor('white')
      .setFontWeight('bold');
    sheet.setFrozenRows(1);
  }

  return sheet;
}

function readRecords_() {
  const sheet = getSheet_();
  const lastRow = sheet.getLastRow();
  if (lastRow <= 1) return [];

  return sheet
    .getRange(2, 1, lastRow - 1, HEADERS.length)
    .getValues()
    .map((row) => {
      const record = {};
      HEADERS.forEach((key, index) => {
        record[key] = parseValue_(key, row[index]);
      });
      return record;
    })
    .reverse();
}

function normalizeValue_(value) {
  if (Array.isArray(value)) return value.join('、');
  return value || '';
}

function parseValue_(key, value) {
  const arrayKeys = ['wish_categories', 'needs', 'preferred_times', 'notify_methods'];
  if (arrayKeys.indexOf(key) !== -1) {
    return String(value || '').split('、').filter(Boolean);
  }
  if (value instanceof Date) return value.toISOString();
  return value || '';
}

function output_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
