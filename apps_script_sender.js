// ────────────────────────────────────────────────────────────
// PHASE 3: Gmail Sending Script (Google Apps Script)
// Deploy this in: script.google.com → bound to your Google Sheet
// ────────────────────────────────────────────────────────────

/**
 * Send a batch of emails from the Sheet.
 * Reads rows where Sent is blank, sends using pre-generated Subject + Body.
 * Marks Sent = YES immediately after each successful send.
 */
function sendBatch() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data  = sheet.getDataRange().getValues();
  var h     = data[0];

  var emailCol   = h.indexOf('Email');
  var subjectCol = h.indexOf('Subject');
  var bodyCol    = h.indexOf('Body');
  var sentCol    = h.indexOf('Sent');

  // Validate columns exist
  if (emailCol === -1 || subjectCol === -1 || bodyCol === -1 || sentCol === -1) {
    Logger.log('ERROR: Missing required columns. Need: Email, Subject, Body, Sent');
    return;
  }

  var BATCH = 5;
  var sent  = 0;

  // Guard: abort if quota is low
  if (MailApp.getRemainingDailyQuota() < BATCH) {
    Logger.log('Quota too low (' + MailApp.getRemainingDailyQuota() + '). Skipping run.');
    return;
  }

  for (var i = 1; i < data.length; i++) {
    if (sent >= BATCH) break;
    if (data[i][sentCol] === 'YES') continue;
    if (!data[i][subjectCol] || !data[i][bodyCol]) continue; // skip unprepared rows
    if (!data[i][emailCol]) continue; // skip rows without email

    try {
      GmailApp.sendEmail(data[i][emailCol], data[i][subjectCol], data[i][bodyCol], {
        name: 'Aakarsh Arya'
      });
      sheet.getRange(i + 1, sentCol + 1).setValue('YES');
      sent++;
      Logger.log('Sent to: ' + data[i][emailCol]);

      if (sent < BATCH) {
        Utilities.sleep(25000 + Math.random() * 15000); // 25–40s gap
      }
    } catch(e) {
      Logger.log('Failed: ' + data[i][emailCol] + ' | ' + e);
    }
  }
  Logger.log('Batch complete. Sent: ' + sent + ' | Remaining quota: ' + MailApp.getRemainingDailyQuota());
}

/**
 * Check remaining daily email quota.
 * Run this before any real send to verify capacity.
 */
function checkQuota() {
  Logger.log('Remaining daily quota: ' + MailApp.getRemainingDailyQuota());
}

/**
 * Set up a time-based trigger to run sendBatch every 5 minutes.
 * Run this ONCE to start automated sending.
 */
function setupTrigger() {
  // Remove any existing triggers first
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendBatch').timeBased().everyMinutes(5).create();
  Logger.log('Trigger live. sendBatch will run every 5 minutes.');
}

/**
 * Remove all triggers. Run this to STOP automated sending.
 */
function stopTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });
  Logger.log('All triggers removed. Sending stopped.');
}

/**
 * Preview: Show what would be sent without actually sending.
 * Use this to verify data before enabling triggers.
 */
function previewNextBatch() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data  = sheet.getDataRange().getValues();
  var h     = data[0];

  var emailCol   = h.indexOf('Email');
  var subjectCol = h.indexOf('Subject');
  var bodyCol    = h.indexOf('Body');
  var sentCol    = h.indexOf('Sent');

  var count = 0;
  for (var i = 1; i < data.length; i++) {
    if (count >= 5) break;
    if (data[i][sentCol] === 'YES') continue;
    if (!data[i][subjectCol] || !data[i][bodyCol]) continue;

    Logger.log('--- Preview #' + (count + 1) + ' ---');
    Logger.log('To: ' + data[i][emailCol]);
    Logger.log('Subject: ' + data[i][subjectCol]);
    Logger.log('Body: ' + data[i][bodyCol].substring(0, 200) + '...');
    Logger.log('');
    count++;
  }
  Logger.log('Total unsent rows ready: ' + count);
  Logger.log('Remaining quota: ' + MailApp.getRemainingDailyQuota());
}
