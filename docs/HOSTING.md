# Hosting Tickmark

Tickmark ships as one Docker image (`Dockerfile`): it builds the web app and serves it with the API. Any host that runs a Dockerfile from
GitHub works. Render is set up already (`render.yaml`); the options below are alternatives or backups.

Free tiers change often. As of late 2026, reviews disagree on which still need a card, so check each provider's pricing page first.

| Host | Free? | Disk | Notes |
|---|---|---|---|
| **Render** (current) | Free web service | Wiped on sleep or deploy | Live at https://tickmark-yqj2.onrender.com. Turn on the GitHub backup below. |
| **Zeabur** | Monthly free credit, reported as no card needed | Ephemeral unless you add a volume | Detects the Dockerfile from GitHub. |
| **Koyeb** | One small free instance (reports say a card may now be required) | Ephemeral | Detects the Dockerfile from GitHub. |
| **Northflank** | Sandbox plan with free services (card status unclear) | Ephemeral; volumes on paid plans | Detects the Dockerfile from GitHub. |
| **Google Cloud Run** | Monthly free requests and CPU; needs a billing account | Ephemeral | `gcloud run deploy --source .` builds the Dockerfile. |
| **Your own machine or a free VM** (e.g. Oracle Cloud Always Free) | Free | Persistent (`docker-compose.yml` volume) | Best for keeping data; you handle HTTPS (e.g. Caddy or Cloudflare Tunnel). |
| Hugging Face Spaces (Docker) | Was free for public demos; reports say Docker Spaces now need a paid plan | Ephemeral | Public by design; not a good fit for private accounts. |

## Settings every host needs

| Variable | Value |
|---|---|
| `SWINGDESK_PORTAL` | `on` (multi-user sign-in) |
| `SWINGDESK_OPEN_SIGNUP` | `on` (anyone can sign up; `off` = invite codes) |
| `SWINGDESK_MASTER_KEY` | a long random string; encrypts everyone's keys and the backup. **Never change it** once people have signed up. |
| `SWINGDESK_SETUP_CODE` | a random string; you enter it once in Account → Become administrator |
| `SWINGDESK_SECURE_COOKIES` | `on` when the site is served over HTTPS (all hosts above) |
| `SWINGDESK_PUBLIC_URL` | the site's https address, e.g. `https://tickmark.zeabur.app` (Render sets its own) |
| `PORT` | set by most hosts; the image listens on it (default 8000) |

Optional: `SWINGDESK_GOOGLE_CLIENT_ID/SECRET`, `SWINGDESK_GITHUB_CLIENT_ID/SECRET` (sign-in buttons), `SWINGDESK_BACKUP_REPO/TOKEN`
(encrypted backup), AI keys (or paste them in Account → Free AI plans).

## Keeping data on a host whose disk is wiped

Set `SWINGDESK_BACKUP_REPO` (a private GitHub repo, e.g. `you/tickmark-data`) and `SWINGDESK_BACKUP_TOKEN` (a fine-grained token with
Contents read/write on that repo only). The data folder is encrypted with the master key and pushed after changes; an empty disk restores it on start.

**Give each host its own backup repo.** Two hosts writing to one repo would overwrite each other. To move hosts, stop the old one
(its final backup is pushed on shutdown), then start the new one with the same repo, token and `SWINGDESK_MASTER_KEY`.

## Sign-in callback URLs

Each extra host is a different address, so register its callback with Google and GitHub too:
- Google: `<public url>/api/auth/google/callback` (Google Cloud console → APIs & Services → Credentials → your OAuth client → Authorized redirect URIs)
- GitHub: one callback per OAuth app, so create one OAuth app per host: `<public url>/api/auth/github/callback`
- TradingView webhooks use the address of the host that created them.

## Step by step

**Zeabur / Koyeb / Northflank**
1. Create an account and connect GitHub; allow access to the private `tickmark` repo.
2. New service → from GitHub → `yesjermo/tickmark`, branch `main`. It finds the `Dockerfile`.
3. Port 8000 (or let it use `PORT`). Add the settings above. Deploy.
4. Open the public URL, set `SWINGDESK_PUBLIC_URL` to it, and redeploy.

**Google Cloud Run**
```bash
gcloud run deploy tickmark --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars SWINGDESK_PORTAL=on,SWINGDESK_OPEN_SIGNUP=on,SWINGDESK_SECURE_COOKIES=on \
  --set-env-vars SWINGDESK_MASTER_KEY=change-me,SWINGDESK_SETUP_CODE=change-me
```
Use Secret Manager for the master key, setup code and tokens in real use.

**Your own machine or VM**
```bash
cp .env.example .env    # then edit the values
docker compose up -d
```
