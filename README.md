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
