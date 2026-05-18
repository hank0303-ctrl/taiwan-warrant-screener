// ============================================================
// 武樂運動空間｜老師請假申請表 - Google Apps Script
// ============================================================
// 使用步驟：
// 1. 開啟 Google 試算表，記下網址中的試算表 ID（/d/ 後面那串）
// 2. 在試算表選單 → 延伸功能 → Apps Script
// 3. 貼上此程式碼，並將 SPREADSHEET_ID 換成你的 ID
// 4. 點選「部署」→「新增部署作業」→ 類型選「網頁應用程式」
//    - 執行身分：「我（你的帳號）」
//    - 存取對象：「所有人」（Anyone）
// 5. 授權後，複製產生的「網頁應用程式 URL」
// 6. 將該 URL 貼入 index.html 與 admin.html 的 GAS_URL
// ============================================================

const SPREADSHEET_ID = '請貼上你的試算表_ID';
const SHEET_NAME = '老師請假申請';

const HEADERS = [
  'submitted_at',
  'teacher_name',
  'leave_type',
  'leave_date',
  'affected_class',
  'affected_period',
  'substitute_status',
  'substitute_teacher',
  'reschedule_status',
  'reschedule_date',
  'reschedule_time',
  'leave_reason',
  'note',
  'admin_status'
];

function doGet(e) {
  try {
    const sheet = getSheet_();
    const action = (e.parameter.action || 'list').toLowerCase();

    if (action === 'update') {
      const row = Number(e.parameter.row);
      const status = e.parameter.admin_status || '';
      updateAdminStatus_(sheet, row, status);
      return json_(e, { status: 'success' });
    }

    const values = sheet.getDataRange().getValues();
    if (values.length <= 1) return json_(e, { status: 'success', items: [] });

    const items = values.slice(1).map((row, index) => {
      const item = { row: index + 2 };
      HEADERS.forEach((key, i) => item[key] = row[i] || '');
      return item;
    }).reverse();

    return json_(e, { status: 'success', items });
  } catch (err) {
    return json_(e, { status: 'error', message: err.message });
  }
}

function doPost(e) {
  try {
    const sheet = getSheet_();
    const data = JSON.parse(e.postData.contents || '{}');

    if (data.action === 'update_status') {
      updateAdminStatus_(sheet, Number(data.row), data.admin_status || '');
      return json_(null, { status: 'success' });
    }

    sheet.appendRow([
      data.submitted_at || new Date(),
      data.teacher_name || '',
      data.leave_type || '',
      data.leave_date || '',
      data.affected_class || '',
      data.affected_period || '',
      data.substitute_status || '',
      data.substitute_teacher || '',
      data.reschedule_status || '',
      data.reschedule_date || '',
      data.reschedule_time || '',
      data.leave_reason || '',
      data.note || '',
      data.admin_status || '待確認'
    ]);

    return json_(null, { status: 'success' });
  } catch (err) {
    return json_(null, { status: 'error', message: err.message });
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

function updateAdminStatus_(sheet, row, status) {
  if (!row || row < 2) throw new Error('缺少有效列號');
  if (!status) throw new Error('缺少處理狀態');
  sheet.getRange(row, HEADERS.indexOf('admin_status') + 1).setValue(status);
}

function json_(e, payload) {
  const output = JSON.stringify(payload);
  const callback = e && e.parameter && e.parameter.callback;
  if (callback) {
    return ContentService
      .createTextOutput(callback + '(' + output + ')')
      .setMimeType(ContentService.MimeType.JAVASCRIPT);
  }

  return ContentService
    .createTextOutput(output)
    .setMimeType(ContentService.MimeType.JSON);
}

