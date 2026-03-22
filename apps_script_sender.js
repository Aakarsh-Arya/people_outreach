// ============================================================
//  ALUMNI OUTREACH EMAIL SENDER — FINAL v2
//  Base: VS Code agent script (safe mark-then-sleep flow)
//  Added: people_api validation + per-send quota check
// ============================================================

// ─── CONFIGURATION ───────────────────────────────────────────
var SHEET_TAB          = "cohort_2013";      // default tab for single-sheet sends
var SENDER_NAME        = "Aakarsh Arya";
var BATCH_SIZE         = 41;                 // set to total rows for one-shot send
var GMAIL_QUOTA_LIMIT  = 1500;
var GMAIL_QUOTA_BUFFER = 25;
var SEND_DELAY_MIN_MS  = 25000;              // min 25 seconds between emails
var SEND_DELAY_MAX_MS  = 40000;              // max 40 seconds between emails
var COHORT_TAB_REGEX   = /^cohort_\d{4}$/;

// ─── SCHEDULE ────────────────────────────────────────────────
// To schedule a start time:
// 1. Set SCHEDULE_TIME to your desired time
// 2. Run scheduleStart() once tonight
// 3. It will fire setupTrigger() at that time, which starts the repeating 5-min trigger
var SCHEDULE_TIME = "2026-03-17T10:00:00";   // change to your desired start time

// ============================================================

function _getTargetSheet(tabName) {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = spreadsheet.getSheetByName(tabName || SHEET_TAB);
  if (!sheet) throw new Error("Sheet not found: " + (tabName || SHEET_TAB));
  return sheet;
}

function _getColumnIndexes(headers) {
  var columns = {
    email:       headers.indexOf("Email"),
    emailSource: headers.indexOf("Email_Source"),
    subject:     headers.indexOf("Subject"),
    body:        headers.indexOf("Body"),
    sent:        headers.indexOf("Sent"),
    status:      headers.indexOf("STATUS")
  };
  if (columns.email === -1 || columns.subject === -1 || columns.body === -1 || columns.sent === -1) {
    throw new Error("Missing required columns. Need: Email, Subject, Body, Sent");
  }
  return columns;
}

function _remainingSendCapacity() {
  var rawQuota    = MailApp.getRemainingDailyQuota();
  var cappedQuota = Math.min(rawQuota, GMAIL_QUOTA_LIMIT);
  return Math.max(0, cappedQuota - GMAIL_QUOTA_BUFFER);
}

function _isSent(value) {
  return String(value || "").trim().toUpperCase() === "YES";
}

function _isSendReady(row, columns) {
  // Must not already be sent
  if (_isSent(row[columns.sent])) return false;

  // Must have email, subject, body
  if (!row[columns.email] || !row[columns.subject] || !row[columns.body]) return false;

  var VERIFIED_SOURCES = ["people_api", "manual_verified"];
  var source = columns.emailSource === -1 ? "" : String(row[columns.emailSource] || "").trim();
  var status = columns.status === -1 ? "" : String(row[columns.status] || "").trim();

  if (VERIFIED_SOURCES.indexOf(source) === -1) return false;
  if (status !== "EMAIL_DONE") return false;

  return true;
}

function _markRowSent(sheet, rowNumber, columns) {
  sheet.getRange(rowNumber, columns.sent + 1).setValue("YES");
  if (columns.status !== -1) {
    sheet.getRange(rowNumber, columns.status + 1).setValue("SENT");
  }
}

function _sleepBetweenSends() {
  var delay = SEND_DELAY_MIN_MS + Math.floor(Math.random() * (SEND_DELAY_MAX_MS - SEND_DELAY_MIN_MS + 1));
  Utilities.sleep(delay);
}

function _collectPendingRows(sheet, limit) {
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  var headers     = data[0];
  var columns     = _getColumnIndexes(headers);
  var pendingRows = [];

  for (var i = 1; i < data.length; i++) {
    if (limit && pendingRows.length >= limit) break;
    var row = data[i];
    if (!_isSendReady(row, columns)) continue;
    pendingRows.push({
      rowNumber: i + 1,
      email:     row[columns.email],
      subject:   row[columns.subject],
      body:      row[columns.body],
      status:    columns.status === -1 ? "" : row[columns.status]
    });
  }
  return pendingRows;
}

function _sendBatchInternal(sheet, maxToSend) {
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) {
    Logger.log("[" + sheet.getName() + "] No data rows found.");
    return { tabName: sheet.getName(), sent: 0, failures: 0, remainingQuota: MailApp.getRemainingDailyQuota(), quotaReached: false };
  }

  var headers        = data[0];
  var columns        = _getColumnIndexes(headers);
  var allowedByQuota = _remainingSendCapacity();
  var sendLimit      = Math.min(maxToSend || BATCH_SIZE, allowedByQuota);

  if (sendLimit <= 0) {
    Logger.log("[" + sheet.getName() + "] Quota buffer reached.");
    return { tabName: sheet.getName(), sent: 0, failures: 0, remainingQuota: MailApp.getRemainingDailyQuota(), quotaReached: true };
  }

  var sent     = 0;
  var failures = 0;

  for (var i = 1; i < data.length; i++) {
    if (sent >= sendLimit) break;

    var row = data[i];
    if (!_isSendReady(row, columns)) continue;

    // ── ADDED: per-send quota check mid-batch ─────────────
    if (_remainingSendCapacity() < 1) {
      Logger.log("Quota exhausted mid-run at row " + (i + 1) + ". Stopping safely.");
      break;
    }

    var rowNumber = i + 1;
    var email     = row[columns.email];

    try {
     GmailApp.sendEmail(email, row[columns.subject], "", {
  name: SENDER_NAME,
  htmlBody: row[columns.body].replace(/\n/g, "<br>")
});
      // ✅ SAFE ORDER: Send → Mark → Sleep (VS Code agent pattern)
      // Mark immediately after send — if crash during sleep, row is already marked, no duplicate
      _markRowSent(sheet, rowNumber, columns);
      sent++;
      Logger.log("[" + sheet.getName() + "] SENT row " + rowNumber + " → " + email);

      if (sent < sendLimit && _remainingSendCapacity() > 0) {
        _sleepBetweenSends();
      }

    } catch (error) {
      failures++;
      Logger.log("[" + sheet.getName() + "] FAILED row " + rowNumber + " → " + email + " | " + error);
    }
  }

  var quotaReached = _remainingSendCapacity() <= 0;
  Logger.log(
    "[" + sheet.getName() + "] Batch complete. Sent=" + sent +
    ", Failures=" + failures +
    ", Remaining quota=" + MailApp.getRemainingDailyQuota()
  );

  // NOTE: No auto-stop here — caller manages triggers
  // Run stopTrigger() manually when campaign is complete

  return { tabName: sheet.getName(), sent: sent, failures: failures, remainingQuota: MailApp.getRemainingDailyQuota(), quotaReached: quotaReached };
}

// ─── PUBLIC SEND FUNCTIONS ────────────────────────────────────

function sendBatch(tabName) {
  return _sendBatchInternal(_getTargetSheet(tabName || SHEET_TAB), BATCH_SIZE);
}

function sendAllPendingCohorts() {
  var cohortSheets = SpreadsheetApp.getActiveSpreadsheet()
    .getSheets()
    .filter(function(s) { return COHORT_TAB_REGEX.test(s.getName()); })
    .sort(function(a, b) { return a.getName().localeCompare(b.getName()); });

  var summaries = [];
  for (var i = 0; i < cohortSheets.length; i++) {
    if (_remainingSendCapacity() <= 0) break;
    var sheet = cohortSheets[i];
    if (_collectPendingRows(sheet, 1).length === 0) continue;
    var summary = _sendBatchInternal(sheet, Math.min(BATCH_SIZE, _remainingSendCapacity()));
    summaries.push(summary);
    if (summary.quotaReached) break;
  }

  Logger.log("Cohort send pass complete. Tabs processed: " + summaries.length);
  return summaries;
}

// ─── TRIGGER MANAGEMENT ──────────────────────────────────────

// Run this once to start repeating sends every 5 minutes
function setupTrigger() {
  stopTrigger();
  ScriptApp.newTrigger("sendAllPendingCohorts").timeBased().everyMinutes(5).create();
  Logger.log("Trigger live. sendAllPendingCohorts every 5 minutes.");
}

// Run this to schedule a one-time start at SCHEDULE_TIME
// At that time it calls setupTrigger() which starts the 5-min repeating trigger
function scheduleStart() {
  stopTrigger();
  ScriptApp.newTrigger("setupTrigger")
    .timeBased()
    .at(new Date(SCHEDULE_TIME))
    .create();
  Logger.log("Scheduled: setupTrigger will fire at " + SCHEDULE_TIME + " and start the 5-min repeating trigger.");
}

// Run this manually to STOP all sending
function stopTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) { ScriptApp.deleteTrigger(t); });
  Logger.log("All triggers removed. Sending stopped.");
}

// ─── PREVIEW & UTILITY FUNCTIONS ─────────────────────────────

function checkQuota() {
  Logger.log(
    "Remaining quota: " + MailApp.getRemainingDailyQuota() +
    " | Usable after buffer: " + _remainingSendCapacity()
  );
}

function getPendingRows(tabName, limit) {
  var sheet = _getTargetSheet(tabName || SHEET_TAB);
  var rows  = _collectPendingRows(sheet, limit || BATCH_SIZE);
  Logger.log("[" + sheet.getName() + "] Pending rows: " + rows.length);
  return rows;
}

function previewNextBatch(tabName) {
  var sheet = _getTargetSheet(tabName || SHEET_TAB);
  var rows  = _collectPendingRows(sheet, BATCH_SIZE);
  if (rows.length === 0) { Logger.log("No send-ready rows."); return; }
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    Logger.log("============================================================");
    Logger.log("Preview #" + (i+1) + " | Row: " + r.rowNumber + " | To: " + r.email);
    Logger.log("Subject: " + r.subject);
    Logger.log(r.body);
    Logger.log("============================================================");
  }
  Logger.log("Total: " + rows.length + " | Quota remaining: " + MailApp.getRemainingDailyQuota());
}

function previewAllPending(tabName) {
  var sheet = _getTargetSheet(tabName || SHEET_TAB);
  var rows  = _collectPendingRows(sheet, 0);
  if (rows.length === 0) { Logger.log("No send-ready rows."); return; }
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    Logger.log("============================================================");
    Logger.log("Preview #" + (i+1) + " | Row: " + r.rowNumber + " | To: " + r.email);
    Logger.log("Subject: " + r.subject);
    Logger.log(r.body);
    Logger.log("============================================================");
  }
  Logger.log("Total send-ready rows: " + rows.length);
}

function exportPreviewToHTML(tabName) {
  var sheet = _getTargetSheet(tabName || SHEET_TAB);
  var rows  = _collectPendingRows(sheet, 0);
  if (rows.length === 0) { Logger.log("No send-ready rows."); return null; }

  var html = "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Email Preview - " + sheet.getName() + "</title>";
  html += "<style>body{font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px}";
  html += ".card{border:1px solid #ccc;border-radius:8px;padding:16px;margin-bottom:24px}";
  html += ".header{background:#f5f5f5;padding:8px 12px;border-radius:4px;margin-bottom:12px}";
  html += ".body{white-space:pre-wrap;line-height:1.6}</style></head><body>";
  html += "<h1>Email Preview: " + sheet.getName() + " (" + rows.length + " emails)</h1>";

  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    html += "<div class='card'><div class='header'>";
    html += "<small>Sheet Row " + r.rowNumber + "</small>";
    html += "<h2>To: " + _escapeHtml(r.email) + "</h2>";
    html += "<strong>Subject:</strong> " + _escapeHtml(r.subject);
    html += "</div><div class='body'>" + _escapeHtml(r.body) + "</div></div>";
  }

  html += "</body></html>";
  var fileName = "email_preview_" + sheet.getName() + "_" + Utilities.formatDate(new Date(), "UTC", "yyyyMMdd_HHmmss") + ".html";
  var file = DriveApp.createFile(fileName, html, MimeType.HTML);
  Logger.log("Preview exported to: " + file.getUrl());
  return file.getUrl();
}

function _escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
