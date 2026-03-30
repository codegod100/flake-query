#!/usr/bin/env python3
"""
flake-query: Inspect a Nix flake installable and report:
  - What derivations it will build/install
  - Closure size and individual package sizes
  - Compatible substituters and cache availability
  - Derivation metadata (system, outputs, license, description)

Usage:
    flake-query <flake-url>
    flake-query nixpkgs#hello
    flake-query github:NixOS/nixpkgs/nixpkgs-unstable#hello
    flake-query .#my-package

Requirements: nix (with flake support)
"""

import json
import subprocess
import sys
import argparse
import re
from datetime import datetime


# ─── helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command, return CompletedProcess."""
    if not quiet:
        print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"ERROR running: {' '.join(cmd)}", file=sys.stderr)
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return r


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "N/A"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def parse_json_or_die(text: str, label: str = "") -> any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON from {label}: {e}", file=sys.stderr)
        print(f"Raw output:\n{text[:500]}", file=sys.stderr)
        sys.exit(1)


# ─── phase 1: flake metadata ──────────────────────────────────────────────────

def get_flake_metadata(installable: str) -> dict:
    """Get flake-level metadata via `nix flake metadata`."""
    # Flake metadata works on flake refs without the #attr fragment
    flake_ref = installable.split("#")[0] if "#" in installable else installable
    r = run(["nix", "flake", "metadata", "--json", flake_ref], quiet=True)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    return parse_json_or_die(r.stdout, "nix flake metadata")


def get_derivation_show(installable: str) -> dict:
    """Get the derivation details via `nix derivation show`."""
    r = run(["nix", "derivation", "show", installable], quiet=True)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    return parse_json_or_die(r.stdout, "nix derivation show")


def get_build_dry_run(installable: str) -> tuple[list[dict], str]:
    """Dry-run build to see what would be fetched/built. Returns (json, stderr_hints)."""
    r = run(["nix", "build", "--dry-run", "--json", installable], check=False, quiet=True)
    builds = parse_json_or_die(r.stdout, "nix build --dry-run --json") if r.stdout.strip() else []
    hints = r.stderr.strip()
    return builds, hints


def get_path_info_recursive(installable: str) -> dict:
    """Get closure info: sizes, narHash, etc for each path in the closure."""
    r = run([
        "nix", "path-info", "--recursive", "--json",
        "--closure-size", "--size", "--json-format", "2",
        installable
    ], quiet=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    return parse_json_or_die(r.stdout, "nix path-info --recursive --json")


# ─── phase 2: substituter checking ────────────────────────────────────────────

def get_configured_substituters() -> list[str]:
    """Read configured substituters from nix config."""
    r = run(["nix", "config", "show"], quiet=True)
    substs = []
    for line in r.stdout.splitlines():
        if line.startswith("substituters"):
            _, vals = line.split("=", 1)
            substs = [s.strip() for s in vals.strip().split() if s.strip()]
    return substs


def check_substituter(url: str) -> dict:
    """Check if a substituter is reachable and get its info."""
    result = {"url": url, "reachable": False, "info": {}}
    r = run(["nix", "store", "info", "--store", url], check=False, quiet=True)
    if r.returncode == 0:
        result["reachable"] = True
        for line in r.stdout.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                result["info"][k.strip()] = v.strip()
    else:
        result["error"] = r.stderr.strip().split("\n")[0] if r.stderr.strip() else "unknown error"
    return result


# ─── formatting ────────────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
RESET = "\033[0m"


def header(text: str):
    print(f"\n{BOLD}{BLUE}{'═' * 60}{RESET}")
    print(f"{BOLD}{BLUE}  {text}{RESET}")
    print(f"{BOLD}{BLUE}{'═' * 60}{RESET}\n")


def subheader(text: str):
    print(f"\n{BOLD}{CYAN}  ── {text} ──{RESET}")


def kv(key: str, value: str, indent: int = 4):
    print(f"{' ' * indent}{BOLD}{key}:{RESET} {value}")



# ─── main logic ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query a Nix flake installable for build/closure/cache metadata."
    )
    parser.add_argument(
        "installable",
        help="Flake URL to query (e.g. nixpkgs#hello, .#my-package, github:owner/repo#attr)"
    )
    parser.add_argument(
        "--no-cache-check", action="store_true",
        help="Skip binary cache availability checks (faster)"
    )
    parser.add_argument(
        "--system", default=None,
        help="Target system (e.g. x86_64-linux, aarch64-darwin)"
    )
    args = parser.parse_args()

    installable = args.installable
    extra_args = []
    if args.system:
        extra_args += ["--system", args.system]

    print(f"\n{BOLD}🔍 Querying flake:{RESET} {CYAN}{installable}{RESET}\n")

    # ── Phase 1: Flake metadata ────────────────────────────────────────────
    header("1. FLAKE METADATA")

    flake_meta = get_flake_metadata(installable)
    if flake_meta:
        # Extract useful fields
        if "description" in flake_meta:
            kv("Description", flake_meta["description"])
        if "url" in flake_meta:
            kv("URL", flake_meta["url"])
        if "lastModified" in flake_meta:
            ts = datetime.fromtimestamp(flake_meta["lastModified"])
            kv("Last Modified", ts.strftime("%Y-%m-%d %H:%M:%S"))
        if "revision" in flake_meta:
            kv("Revision", flake_meta["revision"])
        if "narHash" in flake_meta:
            kv("NAR Hash", flake_meta["narHash"])
        if "locks" in flake_meta:
            kv("Flake locks", f"{len(flake_meta['locks'].get('nodes', {}))} nodes")

        # Print lock info
        if "locks" in flake_meta and "nodes" in flake_meta["locks"]:
            subheader("Lock file nodes")
            for name, node in flake_meta["locks"]["nodes"].items():
                if name == "root":
                    continue
                url = node.get("original", {}).get("ref", "")
                rev = node.get("locked", {}).get("rev", "")
                nar = node.get("locked", {}).get("narHash", "")
                line = f"    {GREEN}{name}{RESET}"
                if rev:
                    line += f" {DIM}(rev: {rev[:12]}){RESET}"
                print(line)
    else:
        print("    (no flake metadata available — not a flake reference?)")

    # ── Phase 2: Derivation info ──────────────────────────────────────────
    header("2. DERIVATION INFO")

    # Get the drv path first
    drv_r = run(["nix", "path-info", "--derivation", "--json", "--json-format", "2"]
                + extra_args + [installable], quiet=True)
    drv_data = {}
    if drv_r.returncode == 0 and drv_r.stdout.strip():
        drv_data = parse_json_or_die(drv_r.stdout, "nix path-info --derivation")

    if drv_data:
        # json-format 2 wraps in {"info": {...}, "storeDir": ..., "version": 2}
        inner = drv_data
        if "info" in drv_data and isinstance(drv_data["info"], dict):
            inner = drv_data["info"]
        for drv_path, info in inner.items():
            if not isinstance(info, dict):
                continue
            kv("Derivation", drv_path)
            if info.get("narSize"):
                kv("NAR Size", human_bytes(info["narSize"]))
            if info.get("narHash"):
                kv("NAR Hash", info["narHash"])
            if info.get("references"):
                kv("Derivation refs", str(len(info["references"])))

    # Derivation show for detailed info
    drv_show = get_derivation_show(installable)
    if drv_show and "derivations" in drv_show:
        for drv_name, drv in drv_show["derivations"].items():
            subheader(f"Derivation: {drv_name}")
            env = drv.get("env", {})
            sa = drv.get("structuredAttrs", {})  # structured attrs have pname/version

            # Get name, version, system from best available source
            name = sa.get("name") or drv.get("name", "")
            pname = sa.get("pname") or env.get("pname", "")
            version = sa.get("version") or env.get("version", "")
            system = drv.get("system", sa.get("system", env.get("system", "N/A")))

            # Fallback: extract from drv filename
            if not pname or not version:
                m_hash = re.match(r"[a-z0-9]+-(.+)", drv_name)
                if m_hash:
                    clean = m_hash.group(1)
                    if clean.endswith(".drv"):
                        clean = clean[:-4]
                    name_ver = re.match(r"(.+?)-(\\d[^-]*$)", clean)
                    if name_ver:
                        if not pname:
                            pname = name_ver.group(1)
                        if not version:
                            version = name_ver.group(2)

            kv("Name", name or pname)
            kv("Pname", pname or "N/A")
            kv("Version", version or "N/A")
            kv("System", system)
            kv("Builder", drv.get("builder", "N/A"))

            outputs = drv.get("outputs", {})
            if outputs:
                out_strs = []
                for k, v in outputs.items():
                    if isinstance(v, dict) and "path" in v:
                        out_strs.append(f"{k} → {v['path']}")
                    else:
                        out_strs.append(f"{k} → {v}")
                kv("Outputs", ", ".join(out_strs))

            # Input derivations
            input_drvs = drv.get("inputs", {}).get("drvs", {})
            if input_drvs:
                kv("Input derivations", str(len(input_drvs)))

            # Build inputs
            for label in ["buildInputs", "nativeBuildInputs", "propagatedBuildInputs",
                          "depsBuildBuild", "depsBuildTarget", "depsHostHost",
                          "depsTargetTarget"]:
                val = env.get(label, "")
                if val:
                    count = len([p for p in val.strip().split() if p])
                    kv(label, f"{count} packages")

    # ── Phase 3: Build plan (dry run) ─────────────────────────────────────
    header("3. BUILD PLAN (dry run)")

    builds, hints = get_build_dry_run(installable)
    if builds:
        print(f"    {BOLD}Will build these derivations:{RESET}")
        for b in builds:
            drv = b.get("drvPath", "?")
            outputs = b.get("outputs", {})
            out_paths = ", ".join(outputs.values()) if outputs else ""
            print(f"      {YELLOW}{drv}{RESET}")
            if out_paths:
                print(f"        → {out_paths}")
    else:
        print("    (nothing to build — all outputs already in store)")

    if hints:
        # Parse "these paths will be fetched" messages
        fetch_lines = [l for l in hints.split("\n") if "fetch" in l.lower() or "build" in l.lower()]
        if fetch_lines:
            subheader("Nix hints")
            for line in fetch_lines:
                print(f"    {DIM}{line.strip()}{RESET}")

    # ── Phase 4: Closure analysis ─────────────────────────────────────────
    header("4. CLOSURE ANALYSIS")

    path_info = get_path_info_recursive(installable)
    if not path_info:
        # Path not in store yet — try to get info from substituters
        print("    Closure not in local store. Querying available info...")
        # Parse the dry-run stderr for download info
        if hints:
            size_match = re.search(r"([\d.]+\s*\w+)\s+download.*?([\d.]+\s*\w+)\s+unpacked", hints)
            if size_match:
                kv("Download size", size_match.group(1))
                kv("Unpacked size", size_match.group(2))
        else:
            print("    (run `nix build --dry-run` to see download sizes)")

    if path_info:
        # json-format 2 may wrap
        inner_pi = path_info
        if "info" in path_info and isinstance(path_info["info"], dict):
            inner_pi = path_info["info"]

        total_closure_size = 0
        total_nar_size = 0
        packages = []

        for store_path, info in inner_pi.items():
            if not isinstance(info, dict):
                continue
            nar = info.get("narSize", 0) or 0
            closure = info.get("closureSize", 0) or 0
            total_closure_size = max(total_closure_size, closure)  # top-level has the biggest
            total_nar_size += nar

            # Extract name from store path
            name = store_path.rsplit("/", 1)[-1]
            # Strip hash prefix
            name_match = re.match(r"[a-z0-9]+-(.+)", name)
            if name_match:
                name = name_match.group(1)

            packages.append({
                "path": store_path,
                "name": name,
                "nar_size": nar,
                "closure_size": closure,
            })

        # Sort by NAR size descending
        packages.sort(key=lambda p: p["nar_size"], reverse=True)

        kv("Total packages in closure", str(len(packages)))
        kv("Total NAR size (download)", human_bytes(total_nar_size))
        kv("Total closure size (on disk)", human_bytes(total_closure_size))

        subheader(f"Top 30 largest packages (by NAR size)")
        name_w = 55
        print(f"    {'Package':<{name_w}}  {'NAR Size':>10}  {'Closure':>10}")
        print(f"    {'─' * name_w}  {'─' * 10}  {'─' * 10}")
        for p in packages[:30]:
            name = p["name"][:name_w]
            print(f"    {DIM}{name:<{name_w}}{RESET}  {human_bytes(p['nar_size']):>10}  {human_bytes(p['closure_size']):>10}")

        if len(packages) > 30:
            print(f"    {DIM}... and {len(packages) - 30} more{RESET}")

    # ── Phase 5: Substituter analysis ─────────────────────────────────────
    header("5. SUBSTITUTER / CACHE ANALYSIS")

    substituters = get_configured_substituters()
    # Deduplicate (trailing slash variants)
    seen = set()
    unique_substs = []
    for s in substituters:
        normalized = s.rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            unique_substs.append(normalized)
    substituters = unique_substs
    kv("Configured substituters", ", ".join(substituters) if substituters else "(none)")

    subheader("Cache availability")
    for url in substituters:
        result = check_substituter(url)
        status = f"{GREEN}✓ reachable{RESET}" if result["reachable"] else f"{RED}✗ unreachable{RESET}"
        line = f"    {url}  {status}"
        if result.get("info"):
            for k, v in result["info"].items():
                line += f"\n      {DIM}{k}: {v}{RESET}"
        if result.get("error"):
            line += f"\n      {DIM}{result['error']}{RESET}"
        print(line)

    # Check if output paths are in caches
    if not args.no_cache_check and substituters:
        inner_pi = path_info
        if isinstance(path_info, dict) and "info" in path_info and isinstance(path_info["info"], dict):
            inner_pi = path_info["info"]
        all_paths = list(inner_pi.keys()) if path_info else []
        # Only check the top-level output paths (first few) to keep it fast
        # For a full check you can use --full-cache-check
        top_paths = []
        if builds:
            for b in builds:
                for out_path in b.get("outputs", {}).values():
                    top_paths.append(out_path)
        if not top_paths:
            top_paths = all_paths[:5]

        subheader("Output path cache status")
        for url in substituters:
            if top_paths:
                print(f"    Checking {CYAN}{url}{RESET} for {len(top_paths)} output path(s)...")
                cached = 0
                missing = 0
                for p in top_paths:
                    r = run(["nix", "path-info", "--store", url, p], check=False, quiet=True)
                    if r.returncode == 0 and r.stdout.strip():
                        cached += 1
                    else:
                        missing += 1
                pct = (cached / len(top_paths) * 100) if top_paths else 0
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = f"{GREEN}{'█' * filled}{RED}{'░' * (bar_len - filled)}{RESET}"
                print(f"      {bar} {pct:.0f}% ({cached}/{len(top_paths)} paths available)")
    elif args.no_cache_check:
        print("    (skipped — use without --no-cache-check to check)")

    # ── Phase 6: Summary ──────────────────────────────────────────────────
    header("6. SUMMARY")

    # Gather summary info from what we collected
    env = {}
    drv_fields = {}
    sa = {}
    if drv_show and "derivations" in drv_show:
        for _, drv in drv_show["derivations"].items():
            env = drv.get("env", {})
            drv_fields = drv
            sa = drv.get("structuredAttrs", {})
            break

    pname = sa.get("pname") or env.get("pname", "N/A")
    version = sa.get("version") or env.get("version", "")
    system = drv_fields.get("system", sa.get("system", env.get("system", "N/A")))

    # Fallback: extract from drv path if env is empty
    if pname == "N/A" and drv_data:
        inner_dv = drv_data
        if "info" in drv_data and isinstance(drv_data["info"], dict):
            inner_dv = drv_data["info"]
        for dp in inner_dv:
            m_hash = re.match(r"[a-z0-9]+-(.+?)(\.drv)?$", dp)
            if m_hash:
                clean = m_hash.group(1)
                if clean.endswith(".drv"):
                    clean = clean[:-4]
                m2 = re.match(r"(.+?)-(\d[^-]*$)", clean)
                if m2:
                    pname = m2.group(1)
                    version = m2.group(2)
            break

    print(f"    {BOLD}Package:{RESET}     {pname} {version}")
    print(f"    {BOLD}System:{RESET}      {system}")
    if drv_data:
        inner_dv = drv_data
        if "info" in drv_data and isinstance(drv_data["info"], dict):
            inner_dv = drv_data["info"]
        for dp in inner_dv:
            print(f"    {BOLD}Derivation:{RESET}  /nix/store/{dp}")
            break
    if path_info:
        inner_pi = path_info
        if isinstance(path_info, dict) and "info" in path_info and isinstance(path_info["info"], dict):
            inner_pi = path_info["info"]
        print(f"    {BOLD}Closure:{RESET}     {len(inner_pi)} packages, {human_bytes(total_nar_size)} to download, {human_bytes(total_closure_size)} on disk")
    if builds is not None:
        print(f"    {BOLD}To build:{RESET}    {len(builds)} derivation(s)")
    print()


if __name__ == "__main__":
    main()
