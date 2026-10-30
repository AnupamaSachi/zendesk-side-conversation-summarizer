/**
 * Generic Google Sheets bridge for locally generated rows.
 * Configure all values through Apps Script Project Settings > Script properties.
 */

function doPost(e) {
  const properties = PropertiesService.getScriptProperties();
  const expectedSecret = properties.getProperty('BRIDGE_SHARED_SECRET') || '';
  const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  if (expectedSecret && body.secret !== expectedSecret) {
    return jsonResponse_({ok: false, error: 'Unauthorized'});
  }

  const rows = Array.isArray(body.rows) ? body.rows : [];
  const spreadsheetId = properties.getProperty('SPREADSHEET_ID');
  const sheetName = properties.getProperty('SHEET_NAME') || 'Tracker';
  if (!spreadsheetId) {
    return jsonResponse_({ok: false, error: 'SPREADSHEET_ID is not configured'});
  }

  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  const sheet = spreadsheet.getSheetByName(sheetName) || spreadsheet.insertSheet(sheetName);
  const headers = [
    'Date', 'Agent', 'Zendesk ID', 'Title', 'Description',
    'Zendesk Ticket status', 'Product', 'Lead Response', 'Jira ID',
    'Dev Response', 'Jira case status'
  ];
  const values = rows.map(function(row) {
    return headers.map(function(header) {
      const key = header.toLowerCase().replace(/[^a-z0-9]+(.)/g, function(_, c) {
        return c.toUpperCase();
      });
      return row[key] == null ? '' : row[key];
    });
  });

  sheet.clearContents();
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  if (values.length) {
    sheet.getRange(2, 1, values.length, headers.length).setValues(values);
  }
  return jsonResponse_({ok: true, updated: values.length});
}

function jsonResponse_(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
