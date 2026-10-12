# Phase 0 feasibility summary

**Date:** 11 October 2026 · **Status:** source measurements complete; Phase 0 not finished (see
[What is still open](#what-is-still-open)).

This summarises the project's Turkish feasibility report, which is kept outside the repository like the raw evidence.
Codes such as K21 refer to the project's decision log.

## The question

Can refurbished iPhone offers sold in Turkey be collected every day, politely and on free infrastructure, with enough
detail to model prices? Phase 0 checked which sites answer an honest crawler, what they expose, what a daily run costs
and how much storage a year of data needs.

## How the evidence was collected

- One day (a Sunday), from the owner's computer: 104 requests each to Getmobil and Easycep and 5 to Trendyol, sent as
  `IphoneFiyatRadari/0.1 (portfolio project feasibility check; contact pending)` by a temporary fetcher that read only
  the `User-agent: *` groups of robots.txt.
- **Stated plainly:** that fetcher counted its 2.0–3.5 s gap from the *start* of the previous request. Responses took
  5.8–9.2 s, so requests went out back to back: one at a time, roughly 7–10 per minute in continuous runs. That broke
  the project's own rule. The production client now starts the gap after the previous response has been received
  (commit `4d79131`).
- No challenge page, 403 or 429 appeared, but only under that load and with the temporary identity.

## Sources tested

A first access check sent a few requests per site, with an honest bot identity and with a desktop browser identity,
without trying to get past any protection.

| Site | Type | Result | Decision |
|---|---|---|---|
| Sahibinden | Peer-to-peer classifieds | Cloudflare JavaScript challenge with both identities | Dropped |
| letgo | Peer-to-peer classifieds | robots.txt allows crawling, but an Akamai bot challenge appears with both identities | Dropped |
| Dolap | Peer-to-peer classifieds | Blocked for the bot identity; opens for a browser but has no iPhones | Dropped |
| n11, Hepsiburada | Marketplaces | Blocked with both identities | Dropped |
| Getmobil | Refurbished marketplace, many sellers | Each variant page lists every seller with price and stock | **GO** |
| Easycep | Refurbished retailer, single seller | One listing gives price, quantity, condition and battery range per SKU | **GO** |
| Trendyol | Marketplace | Reachable; product pages show the winning seller and other sellers (4 in the sample), but whether that list is complete is unknown and there is no battery health field | **Conditional**; stays in Phase 5 (confirmed by the owner) |
| TCMB | Central bank exchange rates | Daily and archive XML files | Used for USD/TRY |

**Why the classifieds were dropped (K4):** their bot protection ignores the User-Agent and checks whether the client
runs JavaScript like a real browser. Getting past it would need browser automation, stealth tooling and proxies, which
is fragile and would probably still fail from GitHub Actions. The project also treats a block of its honest identity as
a "no". Refurbished prices are a cleaner signal anyway: with no haggling, the listed price is close to the selling
price.

## What Getmobil and Easycep expose

**Getmobil**

- **Structure:** brand page → model listings (12 records per page, in-stock variants only) → one page per variant
  (model × storage × colour × condition, and SIM type on some models). The data comes from the Next.js React Server
  Components (RSC) payload embedded in each page; JSON-LD lists only the 10 cheapest alternative sellers.
- **Sellers and volume:** every seller with price and stock, 0–33 per variant, median 2 (counted on 25 pages). 704
  in-stock variants in 38 models, 689 of them in the 33 in-scope models (counted); about 1,500 seller offers
  (estimate; 90% range ~1,220–2,360).
- **Identifiers:** `variant_id` (URL suffix), the numeric seller id `vendorId` and a 13-digit SKU; offer key
  `getmobil:{variant_id}:{vendor_id}`, unique on 25 of 25 pages.

**Easycep**

- **Structure:** one refurbished-Apple listing (`/kategori/apple-2`), 30 records per page, 6 pages; each record has
  price, quantity, cosmetic grade, storage, colour and battery range.
- **Volume:** 169 in-stock SKUs and 285 units at 12:46 UTC (counted), all iPhone 11 or newer. The sitemap's 14,383
  iPhone URLs are a SKU catalogue with only 1.18% in stock, so it is not used for discovery.
- **Identifiers:** Easycep is the only seller. The numeric `id` is a SKU with 1–2 units of one configuration (offer key
  `easycep:{id}`); the SKU code and EAN appear only on the unit page.

## Daily request budget and run time

| Source | Requests per day | Run time |
|---|---|---|
| Getmobil | ~805: 1 brand page, 74 listing pages, 1 sitemap, 689 variant pages, ~40 re-checks of ids that left the listing (assumption) | ~2.3 h (range ~1.9–2.7 h) |
| Easycep | 6 listing pages + 0–30 unit pages for new ids | ~1–6 min |
| Easycep, once | 169 unit pages to read the flags of the current SKUs | ~25–30 min |
| TCMB | 1 | — |

- About 810–840 requests a day. Getmobil gets a full pass every day and, since the sources run in parallel, sets the
  collection time.
- Getmobil responses took 5.8–9.2 s (n=6). With the 2–3.5 s gap after each response, a request cycle is ~8.6–12.0 s,
  3.1–4.4 times the plan's 2.75 s. Getmobil uses ~54% of the 1,500-request per-source budget and fits the 6-hour
  GitHub Actions job limit, but the whole budget would take ~4.2 h (range ~3.6–5.0 h).
- These times were measured locally with the temporary fetcher (a new TLS connection per request, no compression) and
  must be measured again with the new client and from GitHub Actions.

## Storage estimate

| Input | Plan assumption | Phase 0 |
|---|---|---|
| Active offers per day | ~3,000 | ~1,700 (estimate; ~1,390–2,530) |
| Payload per offer | ~2 KB | ~0.4–0.5 KB (measured) |
| Database after one year | ~200–250 MB | ~80–120 MB; ~235–265 MB in the upper scenario |

The upper scenario stores every payload every day at the upper offer count, which applies if the daily change rate
(not measured yet) is high. Both stay under Neon's last known free storage of 0.5 GB. Whole pages are not stored:
Getmobil's RSC payload alone is 196–282 KB per page.

## Main data caveats

1. **No per-offer battery health.** Getmobil has no battery field per offer, Easycep shows one value (`%85-%100`) on
   every record and Trendyol only capacity, so battery health cannot be part of the offer key, the price segment or
   the model features.
2. **Stale listing index (Getmobil).** On 23 in-stock pages the listing disagreed with the variant page on stock 5
   times, on price once and on the buybox seller 3 times. The listing is used only for discovery; prices, stock and
   sellers come from the variant pages.
3. **Out-of-stock pages still show prices.** A sold-out Getmobil variant returns HTTP 200 with `Stokta Yok` ("out of
   stock") and its last price; a delisted Easycep SKU keeps its page and price with `quantity=0`. Offers come from the
   stock fields only.
4. **Hidden SKU flags (Easycep).** SKU codes on unit pages can carry flags (PU, EU, KU, their combinations, 2ESIM) with
   no published meaning; 3 of 23 in-stock unit pages had a flag or an unexplained code. In two pairs (weak evidence),
   EU-flagged SKUs were 7.3% and 9.2% cheaper than their unflagged twins, more than the 1.5–4.6% median gaps between
   cosmetic grades. The listing lacks the flags, so the unit page of each new id is read once.
5. **An offer is not a device.** One Getmobil seller held 91 units of one variant, and on Easycep a sale, a reservation
   and a withdrawal look the same, so the project speaks of delisting, not sales.

## Crawling and privacy rules

What site owners need is in the README section [For site owners](../README.md#for-site-owners). In short:

- **Identity (K5):** `IphonePriceRadar/0.1 (+https://github.com/pnidatunca/iphone-price-radar#for-site-owners)`, with
  no e-mail address; site owners reach the project through GitHub issues.
- **robots.txt:** read on every run, including groups for the `IphonePriceRadar` product token (RFC 9309).
- **Load:** one request at a time per site, a random 2–3.5 s gap after each response, one run a day, at most 1,500
  requests per source. Public pages only; no internal APIs, no browser automation.
- **Blocks and objections (K17):** no permission e-mails are sent. A bot challenge or an access-denied response stops
  the source for the run, and the identity is never changed to get around a block. A source is dropped if its company
  objects or blocks the crawler.
- **Catalogue (K12):** only aggregates and model outputs are published, with links back to the original pages. Test
  fixtures are synthetic, and the raw Phase 0 pages will be deleted.
- **Sellers (K22):** display names, vendor slugs, store links and legal fields are never stored or shown, because slugs
  and store links often contain a store owner's personal name; only the numeric seller id is kept. The deals table
  shows the platform, the price and, for Getmobil, a link to the variant page with `?merchantId=<seller_id>`, where the
  platform itself shows the seller. Public charts label sellers anonymously (Seller A, Seller B, ...). Known
  limitation: if that offer has ended, the link can show another seller, so the table shows the collection time and
  ended offers drop out on the next run.

## Scope (K21)

iPhone X and newer are collected, including iPhone SE (2020) and SE (2022). iPhone 8 / 8 Plus and older, including the
first SE, are not (`scope.excluded_models` in [`config/settings.yaml`](../config/settings.yaml)). On Getmobil this
leaves out 5 models (iPhone 6, 7, 7 Plus, 8 and 8 Plus) with 15 variants and 5 listing pages, 20 requests a day. All of
Easycep's stock is in scope.

## What is still open

- **Phase 0 code** (both adapters, the RSC decoder, a command-line runner and a manual GitHub Actions access test) is
  written and was checked against the 196 archived Getmobil and Easycep pages without sending any request. Neither the
  trial crawl nor the access test has run against the live sites yet.
- **Validation run:** the first run with the permanent identity checks robots.txt for `IphonePriceRadar` groups,
  Getmobil's `isBot` flag, the page structure, challenges, response times and compression.
- **3-day trial crawl:** offer count, field fill rates, daily change rate and new or delisted ids, with raw data in a
  temporary Neon table (K19). Phase 0 ends after 3 consecutive error-free days from at least 2 sources, with daily
  collection running, the request budget confirmed and the run method (GitHub Actions or a local scheduled task)
  chosen.
- **Neon:** project in AWS eu-central-1 (Frankfurt); the connection string is kept in a local `.env` file and a GitHub
  Actions secret. The connection test succeeded (PostgreSQL 18.6); the free-plan limits are not recorded yet.
- **Other:** a DagsHub account; a Power BI Desktop → Neon test (Power BI Desktop is not installed yet); Easycep's terms
  of use, which load only in a browser and are still unread; the meaning of Easycep's SKU flags; Getmobil's offer
  lifecycle (no variant page was fetched twice). Redirect targets are now checked against robots.txt before they are
  requested, each hop as a request of its own.
