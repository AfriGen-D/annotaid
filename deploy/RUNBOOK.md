# AnnotAid: server runbook

For the administrator of the host (Rocky Linux 9, nginx already running).
AnnotAid's developer has no shell access, so everything on the server is done
from this page. Everything AnnotAid-specific, such as user accounts and
backups, is managed afterwards in the app's own **Admin** page.

**What it is:** one Python process (standard library + `jsonschema`), one SQLite
database file, a folder of PDFs. No Docker, no database server, no queue
service. nginx terminates HTTPS and forwards to it on `127.0.0.1:8765`.

| Thing | Where |
|---|---|
| Code (current release) | `/opt/annotaid` → symlink to `/opt/annotaid-releases/<version>` |
| Python virtualenv | `/opt/annotaid-venv` |
| Data: database, PDFs, backups | `/var/lib/annotaid` (`annotaid.db`, `pdfs/`, `backups/`) |
| Settings and secrets | `/etc/annotaid/env` |
| Service | `annotaid.service` (systemd), user `annotaid` |
| Web | `/etc/nginx/conf.d/annotaid.conf` |
| Logs | `journalctl -u annotaid` |

> **Run exactly one instance.** The app serialises concurrent saves with a lock
> that only exists inside one process. A second process on the same data would
> let two people's saves silently overwrite each other.

---

## 1. First install

You need the release tarball, e.g. `annotaid-2.0.0-dev-abc1234.tar.gz`.

```bash
# 1. account and folders
sudo useradd --system --home-dir /var/lib/annotaid --shell /sbin/nologin annotaid
sudo install -d -o annotaid -g annotaid -m 0750 /var/lib/annotaid
sudo install -d -m 0755 /opt/annotaid-releases
sudo install -d -o root -g annotaid -m 0750 /etc/annotaid

# 2. code
sudo tar -xzf annotaid-*.tar.gz -C /opt/annotaid-releases
REL=$(ls -d /opt/annotaid-releases/annotaid-* | sort | tail -1)
sudo ln -sfn "$REL" /opt/annotaid          # the link MUST be named "annotaid"

# 3. Python. 3.11 if you're happy to install it, otherwise the system 3.9 works.
sudo dnf install -y python3.11              # optional
PY=$(command -v python3.11 || command -v python3)
sudo "$PY" -m venv /opt/annotaid-venv
# from the bundled wheels (no internet needed); falls back to PyPI if they don't match
sudo /opt/annotaid-venv/bin/pip install --no-index --find-links /opt/annotaid/wheels -r /opt/annotaid/requirements.txt \
  || sudo /opt/annotaid-venv/bin/pip install -r /opt/annotaid/requirements.txt

# 4. settings: fill in the key and the first admin (see comments in the file)
sudo install -o root -g annotaid -m 0640 /opt/annotaid/deploy/annotaid.env.example /etc/annotaid/env
sudo vi /etc/annotaid/env

# 5. service
sudo cp /opt/annotaid/deploy/annotaid.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now annotaid
sudo systemctl status annotaid             # should say "active (running)"
curl -sI http://127.0.0.1:8765/login | head -1   # HTTP/1.0 200 OK
```

The release tarball's wheels are built for one Python version (3.9 by
default). If you use 3.11, ask for a tarball built with `PYVER=3.11`, or let
the pip fallback fetch from PyPI.

### nginx + HTTPS

```bash
sudo cp /opt/annotaid/deploy/nginx-annotaid.conf /etc/nginx/conf.d/annotaid.conf
sudo vi /etc/nginx/conf.d/annotaid.conf     # set server_name + certificate paths
sudo certbot --nginx -d annotaid.afrigen-d.org   # or your usual certificate process
sudo nginx -t && sudo systemctl reload nginx
```

Hostnames: `annotaid.tibly.net` (testing, until the production name exists)
and `annotaid.afrigen-d.org` (production). Each hostname needs its own
`server` block or `server_name` entry, and a certificate. Nothing else changes
in the app; it accepts requests from whichever hostname nginx passes in `Host`.

The block **must** pass `Host` and `X-Forwarded-Proto` unchanged. The app
uses them for its CSRF check, to mark the session cookie `Secure`, and to
build invite links.

### SELinux (Rocky enforces it)

nginx may not connect to a local port by default. The symptom is **502 Bad
Gateway** while `curl http://127.0.0.1:8765/login` works fine on the box.

```bash
sudo setsebool -P httpd_can_network_connect 1
# or, narrower: allow just this port
sudo semanage port -a -t http_port_t -p tcp 8765
```

### Firewall: outbound

The app needs outbound HTTPS (443) to:
`openrouter.ai`, `eutils.ncbi.nlm.nih.gov`, `www.ncbi.nlm.nih.gov`,
`ftp.ncbi.nlm.nih.gov`, `pmc-oa-opendata.s3.amazonaws.com`, `api.openalex.org`,
`api.unpaywall.org`, `europepmc.org`.

Caveat for strict allowlists: OpenAlex and Unpaywall answer with links to the
open-access PDF **on the publisher's own site**, which can be any domain. With
an allowlist, those two sources fail and fewer papers import automatically.
Managers can still upload those PDFs by hand. Allowing general outbound 443
avoids that.

Inbound: only nginx's 80/443. The app itself listens on 127.0.0.1 only.

### First login

Open `https://<hostname>/`, sign in with `ANNOTAID_ADMIN_EMAIL` /
`ANNOTAID_ADMIN_PASSWORD`, and choose a new password when asked. After that the
two env lines do nothing, and you can blank the password in the file. All other
accounts are created or approved in the app (**Admin → Users / Pending**).

If the startup log says *"no active superadmin, and ANNOTAID_ADMIN_EMAIL /
ANNOTAID_ADMIN_PASSWORD are not set"*, fill them in and
`systemctl restart annotaid`. The same trick recovers a lost admin account:
an existing account with that email is promoted and given that password.

---

## 2. Upgrading

```bash
sudo tar -xzf annotaid-<new>.tar.gz -C /opt/annotaid-releases
sudo ln -sfn /opt/annotaid-releases/annotaid-<new> /opt/annotaid
sudo /opt/annotaid-venv/bin/pip install --no-index --find-links /opt/annotaid/wheels -r /opt/annotaid/requirements.txt
sudo systemctl restart annotaid
sudo journalctl -u annotaid -n 20             # look for "annotaid <version> serving"
```

Database changes between versions are applied automatically on start-up
(forward only). **Rollback:** point the symlink back at the previous release
and restart. If the new version had already upgraded the database, restore the
backup taken just before the upgrade (section 4). Take one first:
**Admin → System → Back up now**, or step 3's command.

A restart interrupts background jobs that are mid-item. Those items are marked
"interrupted by server restart" and can be retried from the app. Nothing is
lost or left half-done.

---

## 3. Backups

- **The database:** the app writes a consistent online copy every night at
  02:00 UTC to `/var/lib/annotaid/backups/annotaid-<timestamp>.db` and keeps the
  last 14. Admins can also press **Back up now**, or download a fresh copy
  (**Admin → System**). Manual copy from the shell:
  `sudo -u annotaid sqlite3 /var/lib/annotaid/annotaid.db ".backup /var/lib/annotaid/backups/manual.db"`
- **The PDFs:** `/var/lib/annotaid/pdfs/` only ever gains files (named by
  content hash, never modified).
- **Please include all of `/var/lib/annotaid` in the host's regular off-machine
  backup.** The app's own copies live on the same disk, so they protect
  against mistakes, not against losing the disk.

Never copy `annotaid.db` by itself while the service runs. Use the backups
above, because the live file may be mid-write and its `-wal` file holds recent
changes.

## 4. Restoring (test this once before go-live)

```bash
sudo systemctl stop annotaid
cd /var/lib/annotaid
sudo -u annotaid mkdir -p restore-saved
sudo -u annotaid mv annotaid.db annotaid.db-wal annotaid.db-shm restore-saved/ 2>/dev/null || true
sudo -u annotaid cp backups/annotaid-<timestamp>.db annotaid.db
sudo systemctl start annotaid
```

Then sign in and check that projects, papers and curated values are there. PDFs
are restored from the host backup of `pdfs/` if that folder was lost. A
restored database simply points at PDFs by hash, so the two don't need to be
from the same moment, as long as no PDF is missing.

---

## 5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| 502 Bad Gateway | Service down (`systemctl status annotaid`), or SELinux (see above) |
| Every form says "request refused: it did not come from this site" | nginx isn't passing `Host` / `X-Forwarded-Proto`; check the `proxy_set_header` lines |
| Users get logged out instantly over HTTPS | `X-Forwarded-Proto` missing, so the cookie is set without `Secure` and then dropped. Or set `ANNOTAID_COOKIE_SECURE=always`. |
| "upload too large" | nginx `client_max_body_size` must be at least `64m` |
| Paper import says "could not reach PubMed" | outbound firewall to NCBI |
| Extraction errors mention OpenRouter / 401 | `OPENROUTER_API_KEY` in `/etc/annotaid/env`, then restart |
| Service won't start: "schema version … newer than this build" | Code was rolled back past a database upgrade; restore the pre-upgrade backup |

The app's **Admin → System** page shows the last 50 server errors, for people
without shell access.
