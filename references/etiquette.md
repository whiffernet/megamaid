# Etiquette

> _"Evil will always triumph because good is dumb."_ — Dark Helmet.
>
> Prove him wrong. Scrape politely.

Scraping is not magic. You are making HTTP requests against someone
else's server, and if you are impolite about it you will:

1. Degrade service for real users.
2. Get your IP blocked (which solves your immediate problem but also
   burns the bridge for anyone else scraping politely later).
3. Occasionally expose yourself or the user to legal risk.

None of this is hard to avoid. A few rules of the road.

## Respect `robots.txt`

The CLI checks `robots.txt` before every `suck` run and refuses if the
target path is disallowed for the megamaid User-Agent.

If the user has **written permission** from the site owner, they can
pass `--ignore-robots`. You should tell them clearly when that flag is
set that they're overriding the check.

Check `Crawl-delay: N` in `robots.txt` too. If the site specifies 10s,
set `rate_limit_seconds = 10`.

## Rate limit

Default is 2.0 seconds between requests. That's the right starting
point for an unfamiliar site.

- **Large commercial sites with obvious bot tolerance** (Shopify
  stores, big news sites): 1.0s is usually fine.
- **Small/independent/personal sites**: 3–5s. You're not in a hurry.
- **Government or academic sites**: 2s, and run during off-hours if
  you're pulling a lot.
- **Anything showing signs of strain** (slow responses, 5xx errors):
  back off more.

Never drop below 1s without a specific reason the user gave you.

## User-Agent

The default User-Agent identifies megamaid and a URL. Don't change it
to impersonate a real browser unless the user specifically asks and
has a good reason. Transparent User-Agents are a gift to the site
operator: they can block you cleanly, or they can whitelist you.

## Concurrency

Default is **one request at a time**, per target. Don't parallelize
against the same host. If you absolutely need to parallelize across
multiple targets, use separate processes or sessions — but one host,
one connection, one request in flight.

## Off-hours

If you're going to pull thousands of pages, run overnight in the
site's local timezone. The same 10,000-request scrape is polite at
3am and obnoxious at 3pm.

## Caching

Every scrape writes the raw response to `staging/<slug>/<run>/raw/`.
That's your cache. Re-parsing is free; re-fetching is not. Don't
re-run just because your selectors changed — re-parse from `raw/`.

## Legal

Not legal advice. General guidance only:

- **Public, static content is generally fair game to read.** Scraping
  the HTML a server sends to any visitor is, in most jurisdictions,
  not illegal on its own.
- **ToS matters.** If a site's Terms of Service explicitly forbid
  automated access, scraping can become a breach-of-contract issue
  even if the content is technically public.
- **Auth matters a lot more.** Scraping behind a login — even with
  valid credentials — can implicate the CFAA in the US and similar
  laws elsewhere if the access exceeds what the account is intended
  for. See `patterns/auth_wall.md`.
- **Copyright matters.** You can fetch a page; you can't always
  republish its content. "Local documents for personal use" is
  generally safer than "I'm going to redistribute this corpus".
- **Personal data matters.** If you're scraping pages that contain
  personal information about identifiable people, you may be subject
  to GDPR, CCPA, or similar. This skill is not designed for that, and
  you should talk to a lawyer before going there.

When in doubt: email the site owner and ask. Many will say yes if
you're polite and explain what you're doing.

## The short version

If a site operator looked at their logs and saw what you were doing,
would they be annoyed? If yes, slow down. If no, carry on.
