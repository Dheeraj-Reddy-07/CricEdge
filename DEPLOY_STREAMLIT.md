# Deploying CricEdge to Streamlit Community Cloud (free)

Streamlit Community Cloud deploys **from a GitHub repo**. The flow is: push this
folder to GitHub → connect it at share.streamlit.io → it installs
`requirements.txt` and runs `app.py`. Free, ~2 minutes once the repo is up.

---

## ⚠️ Read this first — the one real limitation

Streamlit Community Cloud has an **ephemeral filesystem**. The app boots from a
fresh copy of your repo every time it starts, sleeps after inactivity, and
**reboots wipe any files written at runtime.**

For CricEdge that means:

- `data/cricedge.db` is shipped **read-mostly** from the repo — the stats it
  reads (teams, venues, position table) work fine.
- **Predictions saved during a session (`save_prediction`) are LOST** on every
  reboot/redeploy/sleep. You will *not* accumulate a prediction history or a
  CLV log on this host.

That's acceptable for a live analysis/demo tool. It is **not** acceptable if you
want to prove an edge over time — for that you'd need a persistent host (Fly.io
+ volume, or a VPS). See the end of this doc.

---

## Prerequisites
- A **GitHub** account (free).
- Git installed (you have `git 2.51`).
- A Streamlit Community Cloud account — sign in with GitHub at
  https://share.streamlit.io.

---

## Step 1 — Put the project on GitHub (private repo recommended)

This is a private betting tool, so make the **GitHub repo private**. Streamlit's
free tier can deploy from private repos.

From the project root (`D:\Web_devx\CricEdge`), run:

```bash
git init
git add .
git commit -m "CricEdge v1.0 — deployable runtime tree"
```

The `.gitignore` already excludes `.venv/`, `__pycache__/`, `archive/`, the
training DB, `data/raw/`, and `secrets.toml`. What **does** get committed (and
must, for the app to work): `app.py`, `config.py`, `requirements.txt`,
`model/`, `data/database.py`, `data/ingest.py`, `data/cricedge.db` (11 MB — fine),
`ui/`, `scraper/`, `.streamlit/config.toml`, and `live_calibrator.pkl`.

Then create the repo on GitHub and push. Easiest with the GitHub CLI:

```bash
gh repo create cricedge --private --source=. --remote=origin --push
```

Or manually: create an empty **private** repo named `cricedge` on github.com, then:

```bash
git remote add origin https://github.com/<your-username>/cricedge.git
git branch -M main
git push -u origin main
```

> Tip: in this Claude session you can run a shell command yourself by typing it
> with a leading `!`, e.g. `! gh repo create cricedge --private --source=. --remote=origin --push`.

---

## Step 2 — Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and **Sign in with GitHub** (authorize access
   to the private repo).
2. Click **Create app** → **Deploy a public app from a repo** (it works for
   private repos too once authorized).
3. Fill in:
   - **Repository:** `<your-username>/cricedge`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Open **Advanced settings** and set **Python version → 3.12** (matches your
   local 3.12.10 — important so the pinned wheels resolve).
5. (Optional but recommended) paste your **Secrets** now — see Step 3.
6. Click **Deploy**. First build installs `requirements.txt` (~1–2 min). The app
   URL will be `https://<something>.streamlit.app`.

---

## Step 3 — Lock it down (so it isn't open to the world)

**Two layers — use both:**

### (a) Restrict viewers (Streamlit setting)
In the app's **Settings → Sharing**, set the app so only **invited emails** can
view it (add your own email + anyone on the desk). This keeps it off public
search and blocks random visitors.

### (b) Password gate in the app (defense in depth)
Add a password via the **Secrets manager** (Settings → Secrets), in TOML:

```toml
APP_PASSWORD = "choose-a-strong-password"
```

Then add this gate near the **top of `app.py`'s main UI** (right after
`st.set_page_config(...)`), so nothing renders until the password is entered:

```python
def _check_password() -> bool:
    if st.session_state.get("auth_ok"):
        return True
    st.title("🔒 CricEdge")
    pw = st.text_input("Password", type="password")
    if not pw:
        st.stop()
    if pw == st.secrets.get("APP_PASSWORD", ""):
        st.session_state["auth_ok"] = True
        st.rerun()
    else:
        st.error("Incorrect password")
        st.stop()

_check_password()
```

> Locally (no secrets file) `st.secrets.get(...)` returns `""`, so set
> `APP_PASSWORD` in `.streamlit/secrets.toml` for local dev too — that file is
> already git-ignored. **Tell me and I'll wire this gate into `app.py` for you.**

---

## Step 4 — (Recommended) pin dependencies for reproducible builds

Your `requirements.txt` uses `>=`, which lets the cloud pull newer versions that
might break later. To freeze to the exact versions you've tested, replace
`requirements.txt` with:

```text
streamlit==1.58.0
requests==2.34.2
beautifulsoup4==4.15.0
pandas==3.0.3
numpy==2.4.6
plotly==6.8.0
PyYAML==6.0.3
tqdm==4.68.2
lxml==6.1.1
joblib==1.5.3
scikit-learn==1.9.0
scipy==1.17.1
```

This also guarantees the same `scikit-learn` that pickled `live_calibrator.pkl`,
avoiding unpickling warnings. (Say the word and I'll apply this.)

---

## Updating the app later
Just push to GitHub — Streamlit auto-redeploys on each push to `main`:

```bash
git add -A
git commit -m "your change"
git push
```

No "restart the server" dance like local dev — every deploy is a fresh container.

---

## Resource limits (free tier)
- ~1 GB RAM, shared CPU — plenty for this app.
- App **sleeps after ~12 h of inactivity**; first visit after that takes ~30 s to
  wake.
- Reboots/redeploys **reset the filesystem** (the SQLite caveat above).

---

## Troubleshooting
- **Build fails on a package:** confirm Python is set to **3.12** in Advanced
  settings; pin requirements (Step 4).
- **`ModuleNotFoundError`:** the package isn't in `requirements.txt`. Add and push.
- **Calibrator warning / "calibrator unavailable":** usually a `scikit-learn`
  version mismatch — pin it (Step 4). The app runs uncalibrated rather than
  crashing, so this is non-fatal.
- **Live scraping returns nothing:** Cricbuzz may rate-limit or block cloud IPs.
  Manual scorecard entry still works regardless.
- **File too large on push:** only `data/cricedge.db` (11 MB) is sizable — that's
  well under GitHub's 50 MB warning, so you're fine.

---

## If/when you need persistence (predictions history, CLV log)
Streamlit Cloud can't keep runtime DB writes. When you're ready to actually track
performance over time, move to **Fly.io with a volume** or a **$5 VPS** — same
codebase, just a persistent disk mounted where `data/cricedge.db` lives. Ask me
and I'll generate the `Dockerfile` + `fly.toml` + volume config.
