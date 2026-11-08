# Accessibility snapshot fixtures

Real `browser_snapshot` outputs of Playwright MCP, captured from public test sites
(`books.toscrape.com`, `quotes.toscrape.com`, `httpbin.org/forms/post`) and trimmed. They pin the
text format `src/browser/playback.py` reads: `- Page URL:`, `[ref=f3e42]` markers with a frame
prefix even in the main frame, `[cursor=pointer]`, inline text after `:`.

`books_product_reloaded.txt` is derived from `books_product.txt`: the frame prefix changes
(`f4` -> `f9`, as between page loads) and the numbers change (price, stock). Playback must
still rebind every element and keep the checkpoint similar.
