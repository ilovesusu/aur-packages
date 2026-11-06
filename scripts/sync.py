#!/usr/bin/env python3
"""
AUR Packages Auto-Sync Script
Monitors upstream GitHub releases, updates PKGBUILD and .SRCINFO,
and optionally pushes updates to the Arch User Repository (AUR).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES_FILE = os.path.join(ROOT_DIR, "packages.json")


def github_request(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "aur-packages-sync"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_upstream_version(upstream_repo: str, tag_prefix: str = "v") -> Tuple[str, str]:
    """Returns (version_number, tag_name)"""
    url = f"https://api.github.com/repos/{upstream_repo}/releases/latest"
    data = github_request(url)
    tag_name = data.get("tag_name", "")
    version = tag_name
    if tag_prefix and version.startswith(tag_prefix):
        version = version[len(tag_prefix):]
    return version, tag_name


def get_aur_version(pkgname: str) -> Optional[str]:
    """Queries AUR RPC v5 for the current package version (without pkgrel)"""
    url = f"https://aur.archlinux.org/rpc/v5/info?arg[]={pkgname}"
    req = urllib.request.Request(url, headers={"User-Agent": "aur-packages-sync"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("results", [])
            if not data:
                return None
            full_version = data[0].get("Version", "")
            # e.g., '0.4.0-1' -> '0.4.0'
            return full_version.split("-")[0]
    except Exception as e:
        print(f"[-] Warning: Failed to query AUR RPC for {pkgname}: {e}")
        return None


def strip_maintainer_lines(content: str) -> str:
    lines = content.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith("# Maintainer:") or lines[i].startswith("# Contributor:")):
        i += 1
    return "".join(lines[i:])


def sync_upstream_packaging(pkg: dict, new_ver: str, tag_name: str, pkg_dir: str) -> None:
    """Strategy for packages whose upstreams maintain packaging/aur/ in repo (like g502-hub)"""
    upstream = pkg["upstream"]
    base_url = f"https://raw.githubusercontent.com/{upstream}/{tag_name}/packaging/aur"
    
    # Download raw PKGBUILD and .SRCINFO
    req_pkg = urllib.request.Request(f"{base_url}/PKGBUILD", headers={"User-Agent": "aur-packages-sync"})
    req_src = urllib.request.Request(f"{base_url}/.SRCINFO", headers={"User-Agent": "aur-packages-sync"})
    
    with urllib.request.urlopen(req_pkg, timeout=20) as r:
        raw_pkgbuild = r.read().decode("utf-8")
    with urllib.request.urlopen(req_src, timeout=20) as r:
        raw_srcinfo = r.read().decode("utf-8")

    # Build clean PKGBUILD preserving maintainer lines
    header_lines = []
    if pkg.get("maintainer"):
        header_lines.append(f"# Maintainer: {pkg['maintainer']}\n")
    if pkg.get("contributor"):
        header_lines.append(f"# Contributor: {pkg['contributor']}\n")
    
    body = strip_maintainer_lines(raw_pkgbuild)
    new_pkgbuild = "".join(header_lines) + body

    pkgbuild_path = os.path.join(pkg_dir, "PKGBUILD")
    srcinfo_path = os.path.join(pkg_dir, ".SRCINFO")

    with open(pkgbuild_path, "w", encoding="utf-8") as f:
        f.write(new_pkgbuild)
    with open(srcinfo_path, "w", encoding="utf-8") as f:
        f.write(raw_srcinfo)


def sync_bump_pkgver(pkg: dict, new_ver: str, tag_name: str, pkg_dir: str) -> None:
    """Standard strategy: bump pkgver, reset pkgrel=1, and update .SRCINFO"""
    pkgbuild_path = os.path.join(pkg_dir, "PKGBUILD")
    srcinfo_path = os.path.join(pkg_dir, ".SRCINFO")

    if not os.path.exists(pkgbuild_path):
        raise FileNotFoundError(f"{pkgbuild_path} does not exist")

    with open(pkgbuild_path, "r", encoding="utf-8") as f:
        pkgbuild = f.read()

    # Update pkgver and reset pkgrel to 1
    pkgbuild = re.sub(r'(\bpkgver=)[^\n]+', rf'\g<1>{new_ver}', pkgbuild)
    pkgbuild = re.sub(r'(\bpkgrel=)[^\n]+', r'\g<1>1', pkgbuild)

    with open(pkgbuild_path, "w", encoding="utf-8") as f:
        f.write(pkgbuild)

    # If makepkg is available on the system, use it to generate precise .SRCINFO
    if shutil.which("makepkg"):
        try:
            res = subprocess.run(
                ["makepkg", "--printsrcinfo"],
                cwd=pkg_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                text=True
            )
            with open(srcinfo_path, "w", encoding="utf-8") as f:
                f.write(res.stdout)
            return
        except subprocess.CalledProcessError as e:
            print(f"[-] Warning: makepkg failed ({e.stderr.strip()}), falling back to regex update.")

    # Fallback regex replacement for .SRCINFO when makepkg is not installed
    if os.path.exists(srcinfo_path):
        with open(srcinfo_path, "r", encoding="utf-8") as f:
            srcinfo = f.read()
        srcinfo = re.sub(r'(\bpkgver\s*=\s*)[^\n]+', rf'\g<1>{new_ver}', srcinfo)
        srcinfo = re.sub(r'(\bpkgrel\s*=\s*)[^\n]+', r'\g<1>1', srcinfo)
        srcinfo = re.sub(r'(#tag=v?)[0-9a-zA-Z._-]+', rf'\g<1>{new_ver}', srcinfo)
        with open(srcinfo_path, "w", encoding="utf-8") as f:
            f.write(srcinfo)


def push_to_aur(pkgname: str, new_ver: str, pkg_dir: str, ssh_key: Optional[str] = None) -> bool:
    """Clones the AUR repository to a temporary dir, copies files, commits and pushes"""
    aur_url = f"ssh://aur@aur.archlinux.org/{pkgname}.git"
    env = os.environ.copy()
    if ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=accept-new"

    with tempfile.TemporaryDirectory() as tmp_dir:
        clone_dir = os.path.join(tmp_dir, pkgname)
        print(f"[>] Cloning AUR repo {aur_url}...")
        try:
            subprocess.run(
                ["git", "clone", aur_url, clone_dir],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
        except subprocess.CalledProcessError as e:
            print(f"[-] Error cloning AUR repo {pkgname}: {e.stderr.strip()}")
            return False

        # Copy PKGBUILD, .SRCINFO and any other relevant files
        for fname in ["PKGBUILD", ".SRCINFO", "LICENSE", "REUSE.toml"]:
            src = os.path.join(pkg_dir, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(clone_dir, fname))

        # Check git status in clone_dir
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=clone_dir,
            stdout=subprocess.PIPE,
            text=True
        ).stdout.strip()

        if not status:
            print(f"[=] AUR repository for {pkgname} already has identical files.")
            return True

        # Commit and push
        user_name = env.get("GIT_AUTHOR_NAME", "Joy Jasper")
        user_email = env.get("GIT_AUTHOR_EMAIL", "0x73757375@gmail.com")
        
        subprocess.run(["git", "config", "user.name", user_name], cwd=clone_dir, check=True)
        subprocess.run(["git", "config", "user.email", user_email], cwd=clone_dir, check=True)
        subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
        subprocess.run(["git", "commit", "-m", f"Update to {new_ver}"], cwd=clone_dir, check=True)
        
        print(f"[^] Pushing update ({new_ver}) to AUR ({pkgname})...")
        try:
            subprocess.run(
                ["git", "push", "origin", "master"],
                cwd=clone_dir,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            print(f"[+] Successfully pushed {pkgname} {new_ver} to AUR!")
            return True
        except subprocess.CalledProcessError as e:
            print(f"[-] Error pushing to AUR: {e.stderr.strip()}")
            return False


def main():
    parser = argparse.ArgumentParser(description="AUR packages auto-sync")
    parser.add_argument("--dry-run", action="store_true", help="Check versions without modifying files or pushing")
    parser.add_argument("--push-aur", action="store_true", help="Push changes to AUR via SSH")
    parser.add_argument("--package", type=str, help="Only process a single package")
    parser.add_argument("--aur-key", type=str, help="Path to AUR SSH private key")
    parser.add_argument("--force", action="store_true", help="Force update even if version matches")
    args = parser.parse_args()

    if not os.path.exists(PACKAGES_FILE):
        print(f"Error: {PACKAGES_FILE} not found.")
        sys.exit(1)

    with open(PACKAGES_FILE, "r", encoding="utf-8") as f:
        packages: List[dict] = json.load(f)

    if args.package:
        packages = [p for p in packages if p["name"] == args.package]
        if not packages:
            print(f"Error: Package '{args.package}' not found in packages.json.")
            sys.exit(1)

    updated_packages = []

    print("=" * 60)
    print("AUR Packages Auto-Sync")
    print("=" * 60)

    for pkg in packages:
        name = pkg["name"]
        upstream = pkg["upstream"]
        prefix = pkg.get("tag_prefix", "v")
        strategy = pkg.get("strategy", "bump_pkgver")
        pkg_dir = os.path.join(ROOT_DIR, name)

        if not os.path.exists(pkg_dir):
            os.makedirs(pkg_dir, exist_ok=True)

        try:
            up_ver, tag_name = get_latest_upstream_version(upstream, prefix)
        except Exception as e:
            print(f"[-] {name}: Failed to get upstream version from {upstream}: {e}")
            continue

        aur_ver = get_aur_version(name)

        print(f"\n[*] Package:   {name}")
        print(f"    Upstream:  {upstream} -> {up_ver} (tag: {tag_name})")
        print(f"    AUR:       {aur_ver}")

        need_update = args.force or (aur_ver is None) or (up_ver != aur_ver)

        if not need_update:
            print(f"    Status:    Up-to-date (skipping)")
            continue

        print(f"    Status:    NEW VERSION AVAILABLE ({aur_ver} -> {up_ver})")

        if args.dry_run:
            print(f"    Action:    [Dry-run] Would update using '{strategy}' strategy")
            updated_packages.append(name)
            continue

        # Execute update strategy
        print(f"    Action:    Updating local files ({strategy})...")
        if strategy == "upstream_packaging":
            sync_upstream_packaging(pkg, up_ver, tag_name, pkg_dir)
        elif strategy == "bump_pkgver":
            sync_bump_pkgver(pkg, up_ver, tag_name, pkg_dir)
        else:
            print(f"[-] Unknown strategy '{strategy}' for {name}")
            continue

        # Push to AUR if requested
        if args.push_aur:
            key_path = args.aur_key
            if not key_path and os.path.exists(os.path.expanduser("~/.ssh/aur")):
                key_path = os.path.expanduser("~/.ssh/aur")

            success = push_to_aur(name, up_ver, pkg_dir, key_path)
            if not success:
                print(f"[-] Failed to push {name} to AUR.")
                continue

        updated_packages.append(name)

    print("\n" + "=" * 60)
    if updated_packages:
        print(f"[+] Updated packages: {', '.join(updated_packages)}")
    else:
        print("[+] All packages are up to date.")
    print("=" * 60)

    # Export for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"updated_count={len(updated_packages)}\n")
            f.write(f"updated_packages={json.dumps(updated_packages)}\n")
            f.write(f"has_updates={'true' if updated_packages else 'false'}\n")


if __name__ == "__main__":
    main()
