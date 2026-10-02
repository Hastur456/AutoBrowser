---
kind: site
scope: ozon.ru
status: user
source: user
description: Ozon — direct search URL and the price-filter fallback
---
- Direct search navigation is an acceptable fallback:
  https://www.ozon.ru/search/?text=<url-encoded query>. Use it immediately after one
  failed attempt to expose/use the homepage search input.
- If one UI filter attempt leaves the result list unchanged, use a direct URL with
  price parameters instead of repeating the same filter interaction.
