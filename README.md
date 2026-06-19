# VCC: V2ray Config Collector

Scans GitHub for repositories that were updated recently and look related to
v2ray / xray / vmess / vless / shadowsocks / trojan, pulls out config links,
renames every config's "remark" (display name) to a fixed value, removes
duplicates, and saves them into per-protocol/per-transport `.txt` files plus
one combined file. Runs hourly via GitHub Actions once deployed.

Output goes into a `configs/` folder, e.g.:

```
configs/
  vmess.txt
  vless.txt
  shadowsocks.txt
  trojan.txt
  websocket.txt
  grpc.txt
  reality.txt
  tls.txt
  all_configs.txt
  last_update.txt
```

(A single config can appear in more than one file — e.g. a vless config
that uses gRPC over Reality ends up in `vless.txt`, `grpc.txt`, *and*
`reality.txt`.)

---
## Notes & tuning

- **Why two-stage search instead of searching code directly for
  "vmess://"?** GitHub's code-search API doesn't support filtering by last
  push date, and tokenizes special characters like `://` oddly. Instead the
  script first finds *repositories* updated recently (where GitHub's
  `pushed:` date filter works reliably), then scans plausible files inside
  each one (`.txt`, `.yaml`, `.yml`, `.conf`, `.json`, `.md`, `.sub`) for
  config URIs.
- **Tune the search keywords** in `SEARCH_KEYWORDS` inside `main.py` if you
  want to broaden or narrow which repos get scanned.
- **Rate limits**: with a token, the repo-search endpoint allows ~30
  requests/min, and the general API ~5000/hour — comfortable for hourly
  runs at the current `MAX_REPOS` / `MAX_FILES_PER_REPO` settings. If you
  raise those caps a lot, you may need to slow things down further.
- Some repos publish shadowsocks links in the older "fully base64" format
  rather than the newer SIP002 format — these still get their remark
  rewritten correctly, just with a less precise dedup key.
