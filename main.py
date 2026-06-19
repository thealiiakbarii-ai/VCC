"""
V2Ray / Xray config collector & validator (Heavy & Lite Split).

Searches GitHub for recently updated configuration repositories, crawls proxy URIs,
validates node availability using multi-threaded TCP pings, and splits the output
into two master files:
  - all.txt: A heavy file containing ALL verified online nodes.
  - lite.txt: A lite file restricted to the top 20 fastest nodes per type.

Environment variables (all optional):
    GH_TOKEN / GITHUB_TOKEN   GitHub token used for API auth.
    HOURS_BACK                How far back to look for repo updates (default 2).
    MAX_REPOS                 Safety cap on number of repos scanned per run (default 10).
    MAX_FILES_PER_REPO        Safety cap on files inspected per repo (default 60).
    OUTPUT_DIR                Where to write the .txt files (default "configs").
"""

import os
import re
import json
import time
import base64
import socket
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, quote

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

API_BASE = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"
else:
    print("WARNING: no GH_TOKEN/GITHUB_TOKEN set. Rate limits will be very low.")

HOURS_BACK = int(os.environ.get("HOURS_BACK", "2"))
MAX_REPOS = int(os.environ.get("MAX_REPOS", "10"))
MAX_FILES_PER_REPO = int(os.environ.get("MAX_FILES_PER_REPO", "60"))
MAX_FILE_BYTES = 400_000
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "configs")
VERBOSE = os.environ.get("VERBOSE", "0") == "1"

# Connection check configurations
CHECK_TIMEOUT = 3.0       # Seconds to wait for a network handshake
MAX_CHECK_WORKERS = 100   # Number of parallel worker threads for testing
MAX_LITE_PER_TYPE = 25    # Cap per protocol type inside lite.txt

# The remark/name every collected config will be renamed to.
NEW_REMARK = "💮 ＳＥＧＡＲＯ"

SEARCH_KEYWORDS = [
    "v2ray",
    "vmess",
    "vless",
    "xray",
    "shadowsocks",
    "trojan",
    "v2ray-config",
    "free-v2ray",
    "v2ray-subscription",
]

FILE_EXTENSIONS = (".txt", ".yaml", ".yml", ".conf", ".json", ".md", ".sub")
SKIP_PATH_HINTS = (".git/", "node_modules/", "vendor/", "dist/", "build/")

CONFIG_REGEX = re.compile(
    r"(?:vmess|vless|ss|ssr|trojan|hysteria2?|tuic|snell)://[^\s\"'<>`]+",
    re.IGNORECASE,
)
TRAILING_JUNK = ")]},.;\"'>`"

MISSING_QMARK = re.compile(r"(:\d{2,5})(?=[a-zA-Z][a-zA-Z0-9_]*=)")
GLUED_SUFFIX = re.compile(r"-{2,}.*$", re.DOTALL)


def repair_uri(uri):
    uri = GLUED_SUFFIX.sub("", uri)
    if "?" not in uri:
        uri = MISSING_QMARK.sub(r"\1?", uri, count=1)
    return uri

NICE_NAME = {
    "vmess": "vmess",
    "vless": "vless",
    "ss": "shadowsocks",
    "ssr": "shadowsocksr",
    "trojan": "trojan",
    "hysteria": "hysteria",
    "hysteria2": "hysteria2",
    "tuic": "tuic",
    "snell": "snell",
}

session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------------------------------
# GitHub API helpers
# --------------------------------------------------------------------------

def gh_get(url, params=None, max_retries=5):
    for _ in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  network error on {url}: {exc}")
            time.sleep(5)
            continue

        if resp.status_code == 200:
            return resp
        if resp.status_code == 404:
            return None
        if resp.status_code in (403, 429):
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = 60
            if reset:
                wait = max(5, int(reset) - int(time.time()) + 2)
            wait = min(wait, 120)
            print(f"  rate limited, sleeping {wait}s...")
            time.sleep(wait)
            continue

        print(f"  GitHub API error {resp.status_code} for {url}: {resp.text[:200]}")
        time.sleep(5)
    return None


def search_recent_repos():
    since = (datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    found = {}
    for kw in SEARCH_KEYWORDS:
        if len(found) >= MAX_REPOS:
            break
        query = f"{kw} in:name,description,topics pushed:>={since}"
        params = {"q": query, "sort": "updated", "order": "desc", "per_page": 50}
        resp = gh_get(f"{API_BASE}/search/repositories", params=params)
        time.sleep(2)
        if not resp:
            continue
        for item in resp.json().get("items", []):
            found[item["full_name"]] = item
    return list(found.values())[:MAX_REPOS]


def get_repo_tree(full_name, branch):
    url = f"{API_BASE}/repos/{full_name}/git/trees/{branch}"
    resp = gh_get(url, params={"recursive": "1"})
    if not resp:
        return []
    data = resp.json()
    if data.get("truncated"):
        print(f"  (tree truncated for {full_name} — only partial results scanned)")
    return data.get("tree", [])


def candidate_files(tree):
    files = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        lower = path.lower()
        if any(h in lower for h in SKIP_PATH_HINTS):
            continue
        if not lower.endswith(FILE_EXTENSIONS):
            continue
        if item.get("size", 0) > MAX_FILE_BYTES:
            continue
        files.append(path)
    return files


def fetch_raw(full_name, branch, path):
    url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{quote(path)}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.text
    except requests.RequestException:
        pass
    return None


def extract_configs(text):
    cleaned = []
    for m in CONFIG_REGEX.findall(text):
        m = m.rstrip(TRAILING_JUNK)
        m = repair_uri(m)
        if m:
            cleaned.append(m)
    return cleaned


# --------------------------------------------------------------------------
# Parsing, renaming, classification
# --------------------------------------------------------------------------

def b64_decode_any(s):
    s = s.strip()
    padded = s + "=" * (-len(s) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded).decode("utf-8", errors="ignore")
        except Exception:
            continue
    return None


def b64_encode_std(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def b64_encode_urlsafe_nopad(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def safe_port(parsed):
    try:
        return parsed.port
    except ValueError:
        return None


def make_key(parts, fallback_uri):
    if all(parts):
        return ":".join(str(p) for p in parts)
    return "h:" + str(abs(hash(fallback_uri)))


def rewrite_vmess(uri):
    payload = uri[len("vmess://"):]
    decoded = b64_decode_any(payload)
    if decoded is None:
        return None
    try:
        obj = json.loads(decoded)
    except Exception:
        return None

    obj["ps"] = NEW_REMARK
    net = str(obj.get("net", "")).lower()
    tls = str(obj.get("tls", "")).lower()

    new_b64 = b64_encode_std(json.dumps(obj, ensure_ascii=False))
    new_uri = "vmess://" + new_b64

    categories = {"vmess"}
    if net in ("ws", "websocket"):
        categories.add("websocket")
    elif net == "grpc":
        categories.add("grpc")
    if tls == "reality":
        categories.add("reality")
    elif tls == "tls":
        categories.add("tls")

    host = obj.get("add")
    port = obj.get("port")
    key = make_key((host, port, obj.get("id")), new_uri)
    return new_uri, categories, key, host, port


def rewrite_fragment_style(uri, scheme):
    base, _, _ = uri.partition("#")
    new_uri = base + "#" + quote(NEW_REMARK)

    parsed = urlparse(base)
    qs = parse_qs(parsed.query)
    categories = {NICE_NAME.get(scheme, scheme)}

    net = (qs.get("type") or qs.get("net") or [""])[0].lower()
    security = (qs.get("security") or [""])[0].lower()
    if net in ("ws", "websocket"):
        categories.add("websocket")
    elif net == "grpc":
        categories.add("grpc")
    if security == "reality":
        categories.add("reality")
    elif security == "tls":
        categories.add("tls")

    host = parsed.hostname
    port = safe_port(parsed)
    key = make_key((host, port, parsed.username), new_uri)
    return new_uri, categories, key, host, port


def rewrite_ss(uri):
    base, _, _ = uri.partition("#")
    new_uri = base + "#" + quote(NEW_REMARK)

    payload = base[len("ss://"):]
    body = payload.split("?", 1)[0]

    host = port = None
    if "@" in body:
        parsed = urlparse(base)
        host, port = parsed.hostname, safe_port(parsed)
    else:
        decoded = b64_decode_any(body)
        if decoded:
            inner = urlparse("ss://" + decoded)
            host, port = inner.hostname, safe_port(inner)

    categories = {"shadowsocks"}
    key = make_key((host, port), new_uri)
    return new_uri, categories, key, host, port


def rewrite_ssr(uri):
    payload = uri[len("ssr://"):]
    decoded = b64_decode_any(payload)
    if not decoded:
        return None

    main_part, _, param_part = decoded.partition("/?")
    fields = main_part.split(":")
    if len(fields) < 6:
        return None
    host, port, protocol, method, obfs = fields[0], fields[1], fields[2], fields[3], fields[4]
    password_b64 = ":".join(fields[5:])

    params = dict(parse_qsl(param_part))
    params["remarks"] = b64_encode_urlsafe_nopad(NEW_REMARK)
    new_decoded = f"{host}:{port}:{protocol}:{method}:{obfs}:{password_b64}/?{urlencode(params)}"
    new_uri = "ssr://" + b64_encode_urlsafe_nopad(new_decoded)

    categories = {"shadowsocksr"}
    key = make_key((host, port, password_b64), new_uri)
    return new_uri, categories, key, host, port


def process_config(raw_uri, skip_counts):
    scheme = raw_uri.split("://", 1)[0].lower()
    handler = None
    if scheme == "vmess":
        handler = rewrite_vmess
    elif scheme == "ss":
        handler = rewrite_ss
    elif scheme == "ssr":
        handler = rewrite_ssr
    elif scheme in ("vless", "trojan", "hysteria", "hysteria2", "tuic", "snell"):
        handler = lambda u: rewrite_fragment_style(u, scheme)

    if handler is None:
        skip_counts[scheme] += 1
        return None

    try:
        result = handler(raw_uri)
    except Exception as exc:
        if VERBOSE:
            print(f"  failed to parse a {scheme} config: {exc}")
        skip_counts[scheme] += 1
        return None

    if result is None:
        skip_counts[scheme] += 1
    return result


# --------------------------------------------------------------------------
# Connection Verification Worker
# --------------------------------------------------------------------------

def test_node_latency(config_tuple):
    """Performs a network port handshake to check if the remote host is alive."""
    new_uri, categories, primary_cat, host, port = config_tuple
    if not host or not port:
        return None
    try:
        port_num = int(port)
        start_time = time.perf_counter()
        
        # Test TCP connection layer accessibility
        with socket.create_connection((host, port_num), timeout=CHECK_TIMEOUT) as _:
            pass
            
        latency = (time.perf_counter() - start_time) * 1000  # Convert to ms
        return {
            "uri": new_uri,
            "categories": categories,
            "primary_cat": primary_cat,
            "latency": latency
        }
    except Exception:
        return None


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def main():
    print(f"Searching for v2ray-related repos updated in the last {HOURS_BACK}h...")
    repos = search_recent_repos()
    print(f"Found {len(repos)} candidate repos.")

    unique_raw_pool = {}
    skip_counts = Counter()

    for i, repo in enumerate(repos, 1):
        full_name = repo["full_name"]
        branch = repo.get("default_branch") or "main"
        print(f"[{i}/{len(repos)}] Scanning {full_name}...")

        tree = get_repo_tree(full_name, branch)
        files = candidate_files(tree)

        for path in files[:MAX_FILES_PER_REPO]:
            text = fetch_raw(full_name, branch, path)
            if not text:
                continue
            for raw_uri in extract_configs(text):
                result = process_config(raw_uri, skip_counts)
                if not result:
                    continue
                
                new_uri, categories, key, host, port = result
                if key in unique_raw_pool:
                    continue
                
                scheme = raw_uri.split("://", 1)[0].lower()
                primary_cat = NICE_NAME.get(scheme, scheme)
                
                unique_raw_pool[key] = (new_uri, categories, primary_cat, host, port)

    print(f"\nExtracted {len(unique_raw_pool)} unique configurations.")
    print(f"Testing node health concurrently using {MAX_CHECK_WORKERS} threads...")

    verified_nodes = []
    with ThreadPoolExecutor(max_workers=MAX_CHECK_WORKERS) as executor:
        results = executor.map(test_node_latency, unique_raw_pool.values())
        for res in results:
            if res:
                verified_nodes.append(res)

    print(f"Connection checking complete. {len(verified_nodes)} links responded successfully.")

    # Group validated items by their core configuration type
    grouped_by_type = {}
    for node in verified_nodes:
        grouped_by_type.setdefault(node["primary_cat"], []).append(node)

    heavy_all_uris = []
    lite_all_uris = []
    heavy_by_subcategory = {}

    # Separate logic for building Heavy datasets vs Lite datasets
    for p_cat, nodes in grouped_by_type.items():
        # Sort nodes inside the group by latency (lowest/fastest first)
        nodes.sort(key=lambda x: x["latency"])
        
        # 1. Heavy Pool Processing (All working configs go here)
        for node in nodes:
            heavy_all_uris.append(node["uri"])
            for sub_cat in node["categories"]:
                heavy_by_subcategory.setdefault(sub_cat, []).append(node["uri"])
        
        # 2. Lite Pool Processing (Take top 20 fastest entries per group)
        lite_subset = nodes[:MAX_LITE_PER_TYPE]
        for node in lite_subset:
            lite_all_uris.append(node["uri"])

        print(f"  -> {p_cat}: {len(nodes)} total online nodes. Saved top {len(lite_subset)} into lite bundle.")

    # Create destination output workspace
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Write Category-specific text files (Heavy Mode - All functional endpoints)
    for cat, uris in heavy_by_subcategory.items():
        out_path = os.path.join(OUTPUT_DIR, f"{cat}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(uris) + ("\n" if uris else ""))

    # Write Master Heavy File (All successful configs)
    heavy_path = os.path.join(OUTPUT_DIR, "all.txt")
    with open(heavy_path, "w", encoding="utf-8") as f:
        f.write("\n".join(heavy_all_uris) + ("\n" if heavy_all_uris else ""))

    # Write Master Lite File (Max 20 performance links per protocol)
    lite_path = os.path.join(OUTPUT_DIR, "lite.txt")
    with open(lite_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lite_all_uris) + ("\n" if lite_all_uris else ""))

    # Metadata audit log
    with open(os.path.join(OUTPUT_DIR, "last_update.txt"), "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat() + "\n")
        f.write(f"repos_scanned={len(repos)}\n")
        f.write(f"heavy_configs_total={len(heavy_all_uris)}\n")
        f.write(f"lite_total={len(lite_all_uris)}\n")

    print(f"\nDone! Files completely wiped and updated inside '{OUTPUT_DIR}/'.")
    print(f"  - Heavy master sheet: {heavy_path} ({len(heavy_all_uris)} nodes)")
    print(f"  - Lite subscription sheet: {lite_path} ({len(lite_all_uris)} nodes)")


if __name__ == "__main__":
    main()