# MelaSync

One button on your Mac publishes your Mela recipes as a password-protected website you can cook from on your phone. The site works offline once loaded.

```
iCloud Drive/MelaSync/          ← this folder (identical on every Mac)
  sync.sh        the button: snapshot → build → upload → print "Synced N recipes"
  build_site.py  reads a COPY of Mela's database and writes the site
  setup.sh       one-time setup, once per Mac
  config.sh      project name / title (no secrets)

~/Library/Caches/MelaSync/      ← per-Mac, never synced
  snapshot/      the copy of Mela's database that gets read
  images/        resized-image cache (makes re-syncs fast)
  site/          generated site (public/ + functions/_middleware.js)
  tools/         wrangler (Cloudflare CLI)
  last-sync.log  details of the last run
```

## How Mela's data is read (read-only)

Mela keeps its library in a Core Data SQLite store:

`~/Library/Group Containers/66JC38RDUD.recipes.mela/Data/Curcuma.sqlite`

Large photos are stored separately in `.Curcuma_SUPPORT/_EXTERNAL_DATA/` next to it.

The script byte-copies `Curcuma.sqlite`, `Curcuma.sqlite-wal`, and the external image folder into `~/Library/Caches/MelaSync/snapshot/`. If Mela writes during the copy, the script copies again. SQLite only ever opens the copy, with `query_only` on. Mela's files are never opened by SQLite, locked, or modified. The script never copies `-shm`, because that file holds Mela's live locks.

Tables used: `ZRECIPEOBJECT` (recipes), `ZRECIPETAG` + `Z_<n>TAGS` (categories), `ZRECIPEIMAGEOBJECT` (photos). The script detects the schema at runtime. To see what your copy contains:

```bash
python3 ~/Library/Mobile\ Documents/com~apple~CloudDocs/MelaSync/build_site.py inspect
```

## First Mac setup

1. Put this `MelaSync` folder in iCloud Drive. In Finder, right-click it and choose **Keep Downloaded**.
2. Edit `config.sh` and set `PAGES_PROJECT` to something unique, e.g. `casey-recipes-7k2`. Your site will be at `https://<that>.pages.dev`.
3. You need a free Cloudflare account.
4. In Terminal, run:
   ```bash
   bash ~/Library/Mobile\ Documents/com~apple~CloudDocs/MelaSync/setup.sh
   ```
   The script:
   - checks Python and Node
   - installs wrangler
   - opens a browser to log in to Cloudflare
   - creates the Pages project
   - **asks you to choose the site password**, which is stored only as a Cloudflare secret
   - reads Mela (click **Allow** when macOS asks about accessing other apps' data)
   - runs a first sync
5. Create the Shortcut (next section).

## The Shortcut

In the Shortcuts app, create a new shortcut called **Sync Recipes**:

1. Add the **Run Shell Script** action.
   - Shell: `zsh`. Input: *nothing*.
   - Script:
     ```
     bash "$HOME/Library/Mobile Documents/com~apple~CloudDocs/MelaSync/sync.sh"
     ```
2. Add the **Show Notification** action. Set its text to the **Shell Script Result** variable.

When a sync finishes, the notification reads "Synced 213 recipes → https://….pages.dev". If a sync fails, Shortcuts shows an error with the reason and the path of the log.

Optional extras:
- Pin the shortcut to the menu bar (shortcut details ⓘ → *Pin in Menu Bar*).
- Give it a keyboard shortcut.
- Add it to the Dock (File → *Add to Dock*).

Also check that Shortcuts → Settings → Advanced → **Allow Running Scripts** is on.

The first run from Shortcuts may trigger the macOS "Shortcuts would like to access data from other apps" prompt. Click **Allow**. If the run instead fails with "macOS blocked access", add **Shortcuts** in System Settings → Privacy & Security → **Full Disk Access**.

## Second Mac: one-time setup

The scripts and config arrive through iCloud Drive, and the Shortcut syncs through iCloud too if Shortcuts' iCloud sync is on. The second Mac still needs these local, per-machine pieces:

1. **Mela is installed and has synced your library.** Open it once and let iCloud fill it. The site is built from this Mac's local copy.
2. **Python 3:** run `xcode-select --install` (Apple Command Line Tools). Skip this if `python3 --version` already works.
3. **Node.js:** run `brew install node`, or use the installer from nodejs.org.
4. Right-click the `MelaSync` folder in iCloud Drive and choose **Keep Downloaded**.
5. Run `setup.sh` once, as above. It installs wrangler into this Mac's cache and logs this Mac in to Cloudflare. It finds the existing project and **does not touch the password**.
6. Allow the "access data from other apps" prompt, as on the first Mac. Turn on Shortcuts → Settings → Advanced → **Allow Running Scripts**. If needed, add Shortcuts to Full Disk Access.

Either Mac can sync at any time, and the last upload wins. Both Macs read the same iCloud-synced Mela library, so their uploads are the same.

## On your Android phone

1. Open `https://<project>.pages.dev` in Chrome and enter the password. You stay signed in for a year.
2. Chrome menu ⋮ → **Add to Home screen**. The site opens like an app.
3. Open the index once while online. The footer counts photos as they download, then says **Available offline**. After that, every recipe and photo opens with no signal.
4. The site follows Mela's iPhone layout: a recipe list (tap ‹ Recipes for categories). On a recipe, **Cook** opens cooking mode (one step at a time, screen stays on; the round button shows ingredients), and **Adjust** scales the amounts. Tap ingredients to fade them out and tap a step to mark your place.

Each sync uploads a new version. Pages load fresh whenever the phone is online, and cached copies are used when it's offline.

## Changing things

- **Change the password:** in the MelaSync folder, run
  ```bash
  ~/Library/Caches/MelaSync/tools/node_modules/.bin/wrangler pages secret put SITE_PASSWORD --project-name <project>
  ```
  then run a sync. Every device has to log in again.
- **Use an API token instead of `wrangler login`:** create a Cloudflare API token with *Account → Cloudflare Pages → Edit* permission. Store it in each Mac's login Keychain:
  ```bash
  security add-generic-password -a "$USER" -s melasync-cloudflare -w '<token>'
  ```
  `sync.sh` uses the token automatically when it's there.
- **Stronger access control:** instead of the password, you can put the project behind Cloudflare Zero Trust **Access** (e.g. a one-time PIN to your email). Then delete `MIDDLEWARE_JS` from the build. The password gate is simpler and works fine offline.
- **Preview without uploading:** run `bash sync.sh --no-deploy`, then `python3 -m http.server -d ~/Library/Caches/MelaSync/site/public`.
