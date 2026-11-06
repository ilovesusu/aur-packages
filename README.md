# aur-packages

Personal Arch User Repository (AUR) packages maintained by [@ilovesusu](https://github.com/ilovesusu).

Automated with GitHub Actions to poll upstream releases hourly, bump versions, and push directly to the AUR.

---

## 📦 Maintained Packages

| Package | Upstream | AUR Link | Strategy |
| :--- | :--- | :--- | :--- |
| **`g502-hub`** | [Ahmedxsaad/g502-hub](https://github.com/Ahmedxsaad/g502-hub) | [![AUR version](https://img.shields.io/aur/version/g502-hub)](https://aur.archlinux.org/packages/g502-hub) | `upstream_packaging` |
| **`yunting`** | [nephelesound/yunting](https://github.com/nephelesound/yunting) | [![AUR version](https://img.shields.io/aur/version/yunting)](https://aur.archlinux.org/packages/yunting) | `bump_pkgver` |

---

## ⚙️ How It Works

1. **Schedule**: A GitHub Action (`.github/workflows/aur-sync.yml`) runs every hour (`0 * * * *`).
2. **Detection**: `scripts/sync.py` checks the latest release tag on GitHub and compares it with AUR RPC (`v5`).
3. **Packaging**:
   - `upstream_packaging`: Pulls upstream's `packaging/aur/` directly (guaranteeing 100% byte-for-byte compliance for packages with strict validation).
   - `bump_pkgver`: Updates `pkgver`, resets `pkgrel=1`, and generates `.SRCINFO`.
4. **Publish**: Pushes to `ssh://aur@aur.archlinux.org/<pkgname>.git` using the repository's `AUR_SSH_PRIVATE_KEY` secret.
5. **Sync Back**: Commits updated `PKGBUILD` and `.SRCINFO` back to this GitHub repository.

---

## 🚀 Adding a New Package

1. Create a directory named `<pkgname>` with `PKGBUILD` and `.SRCINFO`.
2. Add an entry to [`packages.json`](file:///home/susu/aur/packages.json):
   ```json
   {
     "name": "your-pkg-name",
     "upstream": "owner/repo",
     "maintainer": "Joy Jasper <0x73757375@gmail.com>",
     "strategy": "bump_pkgver",
     "tag_prefix": "v"
   }
   ```
3. Commit and push. The workflow will automatically handle future releases!

---

## 🛠️ Local Usage

Check for updates without modifying files:
```bash
python3 scripts/sync.py --dry-run
```

Sync and push updates to AUR locally:
```bash
python3 scripts/sync.py --push-aur --aur-key ~/.ssh/aur
```
