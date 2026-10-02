# Candid — anonymous feedback for specific coworkers

A small self-hosted web app for a team: anyone can write feedback (a
complaint or a suggestion) for a specific coworker. Before it's sent, the
app rewrites it to strip out identifying language and personal tone, the
submitter reviews and approves the rewritten version, and only then does
it land — anonymously — in that coworker's inbox.

Anonymity here is a database-level guarantee, not just a UI one: the
`feedback` table has no column, and no code path, that could ever link a
submission back to whoever sent it — see "How the anonymity actually
works" below for exactly what that means and where its limits are.

## How it works

1. Everyone creates an account (name, work email, password).
2. To give feedback, pick a person, pick "complaint" or "suggestion", and
   write it however you'd naturally say it (up to 1,200 characters).
3. The app rewords it (see "AI rewording" below) and shows you a
   side-by-side: what you wrote vs. what the recipient would see. You can
   regenerate it, hand-edit it, or discard it.
4. When you approve, it's queued for anonymous delivery — not sent
   instantly (see "Delivery timing protection" below for why). Once
   delivered, the recipient sees only the reworded text, the category,
   and the date.

There's also a public `/faq` page (linked from the nav bar and the signup
page, no login required) that explains the anonymity model in plain
language — worth reading before you point real coworkers at this, since
trust in the anonymity is the whole point.

There is no "my submissions" history. That's deliberate: keeping one
would mean storing exactly the submitter↔feedback link this design exists
to avoid. Once you hit Approve, the app itself has no record of what you
sent, and there's nothing to look back on — not even for you.

## How the anonymity actually works

The `feedback` table's schema is:

```
id, recipient_id, category, reworded_text, edited_by_submitter,
reword_source, queued_at, release_at, read_at
```

There's no `submitter_id`, no session id, no IP address — nothing. The
one function that writes to this table, `db.create_feedback_anonymous()`,
doesn't even accept a submitter as an argument, so there's no code path
that could persist that link by accident.

The trick is in when that function gets called. A submitter needs to
review, regenerate, and hand-edit the reworded text before sending it,
which means *something* has to remember the draft between page loads.
Instead of a database row, the draft lives only in the submitter's own
signed browser session cookie. The server can read it back because the
browser sends it with each request, but nothing is written to disk until
the moment "Approve & send" is clicked — at which point only the
recipient, category, reworded text, and timing are stored. Discarding a
draft, or just closing the tab without finishing, leaves nothing behind
anywhere.

One practical consequence: drafts are capped at 1,200 characters so the
signed session cookie reliably stays well under the ~4KB size browsers
impose on any single cookie (tested worst case: under 2.7KB even for
maximally incompressible text). If someone needs to say more, they submit
more than once.

**What this protects against:** anyone with full access to the database —
a backup, an export, a subpoena for "the database," a curious admin
poking at the SQLite file — learns who received what and roughly when,
and nothing about who sent any of it.

**What this does *not* protect against**, and no application-level design
can fully close on its own:

- **Live server/proxy access logs.** If your reverse proxy, load
  balancer, or WSGI server logs full request bodies (most don't, by
  default), a submission could theoretically be captured in transit at
  the moment it's made. Keep request-body logging off, and always serve
  over HTTPS so nobody on the network path between the browser and server
  can read it either.
- **Corporate/IT-level logging.** If your organization's network already
  logs which authenticated user accessed which URL at which time (a
  corporate proxy, an SSO gateway, endpoint monitoring), that's a layer
  outside this app's control and outside what any app running on top of
  it could prevent.
- **The AI rewording call.** If `ANTHROPIC_API_KEY` is set, the raw
  feedback text (not who wrote it — no identity is ever included in that
  request) is sent to Anthropic's API to be rewritten. That's a
  third-party data-sharing point worth knowing about even though it
  carries no identity.
- **Multi-process deployment.** The session cookie approach assumes a
  submitter's draft only needs to be readable by the same session that
  created it, which works with any number of worker processes — but if
  you scale this out, double check your chosen `SECRET_KEY` is shared
  identically across all workers (it already needs to be, for login
  sessions to work at all).

If your threat model includes a server operator willing to add logging
later specifically to de-anonymize submissions, no design can fully
prevent that except one where the server itself never sees the plaintext
(e.g., client-side encryption to the recipient only) — a meaningfully
bigger project than this one. What's here removes the database as an
attack surface, which was the specific gap flagged.

## Delivery timing protection

Even with the database itself anonymous, a recipient could sometimes
guess who sent something just from *when* it arrived ("this showed up
right after our meeting"). To blur that signal, approved feedback isn't
delivered instantly — it's released at the next fixed daily window that's
at least a few hours away, plus a little random jitter, and the inbox
query simply hides anything whose release time hasn't passed yet (no
background job/cron needed).

Defaults, all overridable via environment variables:

```bash
export FEEDBACK_RELEASE_WINDOWS="09:00,21:00"   # daily release times, UTC, comma-separated
export FEEDBACK_MIN_DELAY_HOURS=4               # floor: always at least this long before release
export FEEDBACK_RELEASE_JITTER_MINUTES=20       # small random jitter added to each release time
```

With the defaults, something approved at 2pm UTC releases around 9pm that
day; something approved at 10pm UTC releases around 9am the next day. The
recipient only ever sees a date, not a time, in their inbox.

## Running it

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000. The first person to sign up won't have
anyone to send feedback to until a second person signs up too.

Data is stored in `instance/feedback.db` (SQLite, created automatically).

This quickstart uses Flask's built-in dev server, which is fine for trying
it out on your own machine but isn't meant to serve your whole team — see
the next section for actually deploying it.

## Deploying on a local macOS server

The `deploy/` folder has everything tailored for running this on a Mac
that your team keeps running on your internal network, fronted by nginx
with your team's existing internal HTTPS certificate — not a public
cloud platform, and not plain HTTP even internally (login passwords and
feedback text both cross this).

Prerequisites on the server: Python 3.9+, [Homebrew](https://brew.sh), and
your internal cert + key files already on disk somewhere the `candid`
process's user can read.

1. **Get the code and dependencies onto the server**, then set up a
   virtualenv (the plist and `run.sh` both expect one at `venv/` inside
   the app folder):
   ```bash
   cd /wherever/you/want/this/to/live
   # copy or clone the feedback-app folder here
   cd feedback-app
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

2. **Create the real environment file** from the template, generate a
   real secret key, and lock the file down:
   ```bash
   cp deploy/candid.env.example deploy/candid.env
   chmod 600 deploy/candid.env
   python3 -c "import secrets; print(secrets.token_hex(32))"
   # paste the output as SECRET_KEY= in deploy/candid.env
   # also set ANTHROPIC_API_KEY there if you want real AI rewording
   ```

3. **Install nginx** if it isn't already on this machine, and wire up the
   reverse-proxy config:
   ```bash
   brew install nginx
   ```
   Edit `deploy/nginx/candid.conf` and replace the three placeholders
   (`REPLACE_WITH_INTERNAL_HOSTNAME`, `REPLACE_WITH_CERT_PATH`,
   `REPLACE_WITH_KEY_PATH`) with your actual internal hostname and the
   paths to your team's cert and key files. Then install it:
   ```bash
   # Apple Silicon:
   cp deploy/nginx/candid.conf /opt/homebrew/etc/nginx/servers/
   # Intel Mac:
   cp deploy/nginx/candid.conf /usr/local/etc/nginx/servers/
   nginx -t   # should say "syntax is ok" / "test is successful"
   brew services start nginx
   ```

4. **Install the launchd service** so Candid itself starts on boot and
   restarts if it ever crashes. Edit
   `deploy/launchd/com.candid.app.plist` first and replace every
   `/REPLACE/WITH/ABSOLUTE/PATH/TO/feedback-app` with the real absolute
   path where you put this folder (launchd needs absolute paths — `~`
   and relative paths don't work in a plist):
   ```bash
   mkdir -p deploy/logs
   cp deploy/launchd/com.candid.app.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.candid.app.plist
   launchctl list | grep candid   # should show it running
   ```
   A LaunchAgent (as used here) runs as whichever user loads it, and
   keeps running only while that user is logged in — simplest to set up.
   If this server should keep serving Candid even when nobody's logged
   in (e.g., after a reboot with no auto-login), use a LaunchDaemon
   instead: install the same plist to `/Library/LaunchDaemons/` and load
   it with `sudo launchctl load -w` as root. A LaunchDaemon runs as root
   by default, so add a `<key>UserName</key>` entry to the plist naming
   a less-privileged account to run it as instead.

5. **Verify**: visit `https://<your-internal-hostname>/` from another
   machine on the network. You should get Candid over a trusted
   connection, not a certificate warning.

6. **Logs** land in `deploy/logs/candid.out.log` and `candid.err.log`
   (gunicorn's own request/error logs), and nginx's own logs are wherever
   your Homebrew nginx install keeps them (`nginx -T | grep access_log`
   to check). None of these log request bodies — don't add that later;
   see "How the anonymity actually works" above for why.

To update the code later: pull/copy the new files in, run
`venv/bin/pip install -r requirements.txt` again in case dependencies
changed, then `launchctl unload` and `launchctl load` the plist again to
restart it with the new code.

## AI rewording

Set an `ANTHROPIC_API_KEY` environment variable and the app will call
Claude to properly neutralize each submission — stripping identifying
phrasing while keeping every concrete point intact. Without a key, it
falls back to a basic built-in cleanup (removes "I think"/"I feel"
framing, normalizes punctuation) and the review page tells the submitter
so they know to read it extra carefully before sending. Either way,
nothing is queued until the submitter approves it.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

## Before you put this in front of real coworkers

Handled by the "Deploying on a local macOS server" section above, once you
follow it:

- ~~Put it behind HTTPS~~ — nginx terminates your internal cert; gunicorn
  itself only ever listens on localhost.
- ~~Set a real `SECRET_KEY`~~ — `deploy/candid.env` (not source control).
- ~~Use a production server~~ — gunicorn behind nginx, supervised by launchd.

Still genuinely open:

- **Decide on a moderation/abuse policy.** Right now anything approved by
  the submitter goes straight through — there's no profanity filter or
  admin review step. Add one if that's a concern for your team (note it
  would need to happen before approval, since nothing is recoverable
  after — there's no submitter to trace back to for follow-up).
- **Password reset** isn't implemented — add one before real rollout.
- **Rate limiting** on login/signup — nothing currently blunts repeated
  password-guessing attempts.
- Read "How the anonymity actually works" above and decide whether its
  residual risks (server logs, IT-level monitoring) matter enough for
  your organization to warrant more than what's here.

## Project layout

```
app.py                        Flask routes (auth, submit/review/approve, inbox)
db.py                         SQLite data layer + anonymity guarantees (no ORM dependency)
reword.py                     The rewording engine (AI + heuristic fallback)
templates/                    Jinja2 HTML templates
static/style.css              Styling
deploy/candid.env.example     Template for the real secrets file (copy to candid.env)
deploy/run.sh                 Wrapper launchd execs: loads candid.env, starts gunicorn
deploy/launchd/*.plist        macOS service definition (auto-start, auto-restart)
deploy/nginx/candid.conf      TLS termination + reverse proxy, for your internal cert
```
