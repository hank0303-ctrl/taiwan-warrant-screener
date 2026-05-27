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
// 請填寫要收到老師請假通知的信箱，例如：'hank@example.com'
const NOTIFY_EMAIL = '';

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
  'admin_status',
  'notification_status'
];

function doGet(e) {
  try {
    const sheet = getSheet_();
    const action = (e.parameter.action || 'list').toLowerCase();

    if (action === 'submit') {
      const data = JSON.parse(e.parameter.payload || '{}');
      appendSubmission_(sheet, data);
      return json_(e, { status: 'success' });
    }

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

    appendSubmission_(sheet, data);

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
  } else {
    ensureHeaders_(sheet);
  }

  return sheet;
}

function appendSubmission_(sheet, data) {
  const notificationStatus = sendLeaveNotification_(data);

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
    data.admin_status || '待確認',
    notificationStatus
  ]);
}

function ensureHeaders_(sheet) {
  const currentHeaders = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const missingHeaders = HEADERS.filter(header => currentHeaders.indexOf(header) === -1);
  if (!missingHeaders.length) return;

  const startColumn = sheet.getLastColumn() + 1;
  sheet.getRange(1, startColumn, 1, missingHeaders.length).setValues([missingHeaders]);
  sheet.getRange(1, startColumn, 1, missingHeaders.length)
    .setBackground('#D96B2A')
    .setFontColor('white')
    .setFontWeight('bold');
}

function updateAdminStatus_(sheet, row, status) {
  if (!row || row < 2) throw new Error('缺少有效列號');
  if (!status) throw new Error('缺少處理狀態');
  sheet.getRange(row, HEADERS.indexOf('admin_status') + 1).setValue(status);
}

function sendLeaveNotification_(data) {
  try {
    const recipient = String(NOTIFY_EMAIL || '').trim();
    if (!recipient) return '通知未寄出：尚未設定 NOTIFY_EMAIL';

    const leaveDates = data.affected_period || data.leave_date || '未填';
    const subject = '【武樂】老師請假申請通知 - ' + (data.teacher_name || '未填姓名');
    const body = [
      '有一筆新的老師請假申請：',
      '',
      '送出時間：' + formatDateTime_(data.submitted_at || new Date()),
      '老師姓名：' + (data.teacher_name || '未填'),
      '請假類型：' + (data.leave_type || '未填'),
      '請假日期：' + leaveDates,
      '請假課程：' + (data.affected_class || '未填'),
      '代課狀態：' + (data.substitute_status || '未填'),
      '代課老師：' + (data.substitute_teacher || '未填'),
      '請假原因：' + (data.leave_reason || '未填'),
      '其他備註：' + (data.note || '無'),
      '',
      '後台處理狀態：' + (data.admin_status || '待確認')
    ].join('\n');

    MailApp.sendEmail({
      to: recipient,
      subject: subject,
      body: body,
      name: '武樂老師請假表'
    });

    return '通知已寄出：' + recipient;
  } catch (err) {
    return '通知寄送失敗：' + err.message;
  }
}

function testEmailNotification() {
  const result = sendLeaveNotification_({
    submitted_at: new Date(),
    teacher_name: '測試老師',
    leave_type: '通知測試',
    leave_date: '2026-05-27',
    affected_class: '測試課程',
    affected_period: '',
    substitute_status: '測試通知',
    substitute_teacher: '',
    leave_reason: '測試',
    note: '如果收到這封信，代表 MailApp 通知設定成功。',
    admin_status: '待確認'
  });
  Logger.log(result);
}

function formatDateTime_(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (isNaN(date.getTime())) return value;
  return Utilities.formatDate(date, 'Asia/Taipei', 'yyyy/MM/dd HH:mm:ss');
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
