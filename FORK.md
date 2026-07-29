# Fork notes

Fork of [Yurich-ru/yandex-ads-mcp](https://github.com/Yurich-ru/yandex-ads-mcp) (MIT).

Base commit: `7ad0551` — *"Track Yandex Direct API points via the Units response header"*.
Everything below lives on the `fork-main` branch; `main` tracks upstream unchanged.
The whole delta is 193 added / 12 removed lines in `server.py`; no other file is touched.

Upstream's `README.md` still describes the project as a whole and is left untouched so the fork stays
easy to rebase.

## What this fork changes, and why

### Yandex Cloud IAM tokens from a service-account key — `365d3e3`

Wordstat is the only part of the server that goes through Yandex Cloud, and it needs an IAM token.
Upstream mints one by exchanging the Passport OAuth token
(`{"yandexPassportOauthToken": ...}`). **Yandex banned that exchange for tokens issued after
2026-06-01** — it fails with code 3, *"issued after '2026-06-01' is not supported for IAM token
exchange"* — so on any freshly issued token Wordstat is permanently dead. Reissuing does not help;
every new token is past the cutoff.

The fork prefers a service-account key when `YC_SA_KEY_FILE` points at an authorized-key JSON: it
builds a PS256 JWT (`aud` = the IAM tokens endpoint, `iss` = service account id, `kid` = key id,
360 s lifetime) and posts `{"jwt": ...}` instead. Without the variable it falls back to upstream's
behaviour, so nothing changes for older tokens.

Requires `pyjwt[crypto]`. The import is lazy — it only happens when `YC_SA_KEY_FILE` is set, so the
dependency stays optional.

The service account only needs the `search-api.executor` role on the folder holding the Wordstat
quota. That is the entire blast radius of the key: no Direct access, no Metrika, no billing.

### Wordstat: retry on 429 — `08679e4`

Yandex Cloud enforces a per-token Wordstat quota of **10 requests/second and 100 requests/hour** on a
fresh folder. Upstream called Wordstat with a plain `client.post` and re-raised on 429, so any bulk
semantic pass died after a handful of phrases — even though the repo already had a
`request_with_retry` helper (exponential backoff, honours `Retry-After`) that the Direct path used.
Wordstat now goes through the same helper.

Retries cannot beat an hourly cap, only a per-second one. For real volume, ask Yandex Cloud support
to raise the Search API quota on your folder — it is not exposed in the self-service Quotas UI.

### Wordstat: `num_phrases` default 30 → 2000 — `00175eb`

`/topRequests` has no batch endpoint: one seed phrase is one request and one quota unit, but a single
request returns up to 2000 related phrases. Defaulting to 30 spent a whole unit — out of only 100 per
hour — on 30 phrases. The tool descriptions were rewritten to tell the model that cost is per
request, not per phrase, so it stops calling the tool once per keyword.

### Combinatorial (ResponsiveAd) ads — `0e9947a`, `4618b8f`

Upstream could only build single-field `TextAd` objects (Title / Title2 / Text / Href), so
combinatorial ads — the modern Direct format with several headlines and texts that Yandex combines
itself — could not be uploaded at all. Added `yd_ads_add_combinatorial` and
`yd_ads_update_combinatorial`, plus `delete` in the `yd_ads_action` enum, and taught `yd_ads_get` to
request `ResponsiveAdFieldNames` (it used to read combinatorial ads back as empty).

Limits: 1–7 titles (≤56 chars), 1–3 texts (≤81), up to 5 image hashes, at most 3 combinatorial ads
per group. They only work inside a Unified Performance Campaign
(Единая перформанс-кампания); Yandex rejects them on legacy campaign types.

### Direct API v501 for combinatorial ads — `dd6c7c1`

The blocker that made the previous two commits useless in production: Direct answers
*"Объявление данного типа не поддерживается в v5, используйте v501"*, but upstream hard-coded the v5
endpoint for every call.

The documented v501 URL, `https://api.direct.yandex.com/v501/`, is the **SOAP** endpoint — posting
JSON to it returns HTTP 202 with an XML fault. The working JSON endpoint is
**`https://api.direct.yandex.com/json/v501/ads`**, determined empirically. A parity test against a
real campaign confirmed v501 reads `TEXT_AD` ads identically to v5, so it is a clean superset;
combinatorial add/update and `ads.get` are routed there while everything else stays on v5.

Verified end-to-end: campaign + ad groups + combinatorial `RESPONSIVE_AD` ads created and read back
with all titles and texts intact.

Two things worth knowing when creating campaigns through the API: they land as `Status=DRAFT` /
`State=OFF` (`Campaigns.suspend` then fails with error 8300 — expected, nothing to fix), and the
`ENABLE_EXTENDED_AD_TITLE` setting is deprecated (warning 10163 — drop it).

### Tool exposure for MCP clients — `4d8afcc`, `f7c8c28`, `f562f9c`

Three related changes for hosted MCP clients:

- **`YD_READONLY=true` now also hides mutating tools** from `tools/list` (120 → 45), instead of only
  refusing them at call time.
- **`YD_LEAN=true`** exposes a curated essential subset (37 tools) for clients that cap the total
  tool count across connectors.
- **Tools are re-exposed as `yandex_direct_*` / `yandex_metrika_*` / `yandex_wordstat_*`** with
  descriptions prefixed *"Yandex Direct / Яндекс Директ"*. This one is not cosmetic: clients that
  load third-party MCP tools lazily and retrieve them through a semantic search never surfaced the
  original `yd_*` names, because names like `yd_campaigns_get` with a description of *"Get list of
  campaigns"* contain no "yandex" or "direct" token at all — the search kept returning other ad
  platforms instead. `call_tool()` reverse-maps the new names, and the old `yd_*` names still
  dispatch, so existing integrations keep working.

Filter order is `readonly` → `lean`. Both default to off.

## Extra configuration

| Variable | Effect |
| --- | --- |
| `YC_SA_KEY_FILE` | Path to a Yandex Cloud service-account authorized-key JSON. Set it to mint IAM tokens via JWT instead of the (now banned) Passport exchange. |
| `YD_LEAN` | `true` exposes only the curated essential tool subset. |

`YD_READONLY`, `YD_CONFIRM`, `YD_ALLOWED_LOGINS` and the rest are upstream's — see its README.

## Safety

With `YD_READONLY=false` this server can spend real money on every account the token can reach. A
representative/managing Yandex account may hold delegated access to dozens of client accounts at
once.

- Keep `YD_CONFIRM=true`: mutating calls then return a preview and only execute when re-called with
  `confirm=true`.
- Use `YD_ALLOWED_LOGINS` to whitelist the client logins you actually intend to touch. An empty
  whitelist means "everything the token can see".
- Prefer `YD_READONLY=true` unless a session genuinely needs to write.

## Staying in sync with upstream

```bash
git remote add upstream https://github.com/Yurich-ru/yandex-ads-mcp.git
git fetch upstream
git rebase upstream/main fork-main
```

Every change is an ordinary commit in `server.py`, so conflicts surface as normal rebase conflicts.
This replaces the previous arrangement, where the same changes lived as standalone patch scripts and
were silently lost on every `git pull`.

## Contact

Issues and pull requests: [github.com/guyfullin10-lgtm](https://github.com/guyfullin10-lgtm)

Upstream project and its license belong to [Yurich-ru](https://github.com/Yurich-ru/yandex-ads-mcp);
this fork keeps the same MIT terms.
