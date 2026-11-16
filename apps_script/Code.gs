/**
 * Portfolio chatbot: calendar bridge.
 *
 * Runs in YOUR Google account and lets the chatbot backend do exactly what call booking needs:
 * read free/busy times, create/update/remove the booking events it made, and send booking emails.
 * Every request must carry SECRET, which only the backend knows.
 *
 * Setup (see README "Call booking"):
 *   1. script.google.com → New project → paste this file over Code.gs.
 *   2. Services (+) → Google Calendar API → Add.
 *   3. Set SECRET below to a long random string (same value as APPS_SCRIPT_SECRET on Render).
 *   4. Run `authorize` once and allow access.
 *   5. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone → copy the /exec URL.
 */

const SECRET = 'PASTE_A_LONG_RANDOM_SECRET_HERE';
const CALENDAR_ID = 'primary';

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (!SECRET || SECRET.indexOf('PASTE_') === 0 || req.secret !== SECRET) {
      return json_({ error: 'unauthorized' });
    }
    return json_({ result: handle_(req.op, req.args || {}) });
  } catch (err) {
    return json_({ error: String(err && err.message ? err.message : err) });
  }
}

function doGet() {
  return json_({ ok: true, hint: 'This endpoint only accepts POST requests from the chatbot backend.' });
}

function handle_(op, a) {
  switch (op) {
    case 'ping':
      return 'pong';
    case 'freebusy':
      return Calendar.Freebusy.query({ timeMin: a.timeMin, timeMax: a.timeMax, items: [{ id: CALENDAR_ID }] });
    case 'list':
      return Calendar.Events.list(CALENDAR_ID, {
        timeMin: a.timeMin,
        timeMax: a.timeMax,
        privateExtendedProperty: 'source=portfolio-bot',
        singleEvents: true,
        maxResults: 250,
      });
    case 'get':
      try {
        return Calendar.Events.get(CALENDAR_ID, a.eventId);
      } catch (err) {
        if (/not found|404|410|deleted/i.test(String(err))) return null;
        throw err;
      }
    case 'insert':
      requireOwnEvent_(a.body);
      return Calendar.Events.insert(a.body, CALENDAR_ID, { sendUpdates: a.sendUpdates || 'none' });
    case 'patch':
      requireBotEvent_(a.eventId);
      return Calendar.Events.patch(a.body, CALENDAR_ID, a.eventId, {
        sendUpdates: a.sendUpdates || 'none',
        conferenceDataVersion: a.conferenceDataVersion || 0,
      });
    case 'delete':
      requireBotEvent_(a.eventId);
      Calendar.Events.remove(CALENDAR_ID, a.eventId, { sendUpdates: a.sendUpdates || 'none' });
      return true;
    case 'email':
      MailApp.sendEmail(a.to, a.subject, a.body);
      return true;
  }
  throw new Error('unknown op: ' + op);
}

/** New events must be tagged as created by the chatbot. */
function requireOwnEvent_(body) {
  const p = (body && body.extendedProperties && body.extendedProperties.private) || {};
  if (p.source !== 'portfolio-bot') throw new Error('only portfolio-bot events can be created');
}

/** Only events the chatbot created can be changed or removed, never your other events. */
function requireBotEvent_(eventId) {
  const ev = Calendar.Events.get(CALENDAR_ID, eventId);
  const p = (ev.extendedProperties && ev.extendedProperties.private) || {};
  if (p.source !== 'portfolio-bot') throw new Error('not a portfolio-bot event');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Run this once from the editor to grant access to Calendar and email. */
function authorize() {
  const now = new Date();
  Calendar.Freebusy.query({
    timeMin: now.toISOString(),
    timeMax: new Date(now.getTime() + 3600000).toISOString(),
    items: [{ id: CALENDAR_ID }],
  });
  Logger.log('Calendar OK. Emails left today: ' + MailApp.getRemainingDailyQuota());
}
