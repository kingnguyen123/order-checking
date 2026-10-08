# Rewrite Guide: Account Finder

Read `rewrite-00-overview.md` first for shared conventions. Same idea as Profile Finder (`rewrite-03-profile-finder.md`), applied to plain-text `email:password` account lists instead of column-based CSV — read that doc alongside this one, since this tool is a close sibling of it.

## What it does

You give it a full account list, one `email:password` per line, and a list of emails. It splits the list into two new text files: matching lines (**matches**) and everything else (**rest**) — lines copied through **byte-for-byte**, never re-parsed or reformatted, so unusual password characters (`$`, `!`, `%`, extra colons, etc.) can never be corrupted.

## File: `backend/account_finder/filter.py`

```python
def line_email(line):
    if ":" not in line:
        return None
    return line.split(":", 1)[0].strip() or None
```
The email is everything before the **first** colon. A line with no colon at all can never match anything (returns `None`) — it falls through to "rest" untouched, rather than being force-fit or dropped.

```python
def parse_wanted_emails(text):
    wanted = {}
    for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        item = chunk.strip()
        if not item:
            continue
        email = item.split(":", 1)[0].strip() if ":" in item else item
        if not email:
            continue
        key = email.lower()
        if key not in wanted:
            wanted[key] = email
    return wanted
```
Same splitting rules as Profile Finder's version (newline/comma/semicolon, dedupe by lowercase key, first-seen spelling kept), **plus** one extra affordance: each entry in the "wanted" list may itself be a full `email:password` line, and only the part before its first colon is kept. This lets you paste a subset of lines copied directly out of the same account-list format as the "emails to pull out" list, without having to strip passwords off yourself first — a small but deliberate UX difference from Profile Finder (which only ever expects bare emails in its wanted-list input).

```python
def filter_lines(accounts_text, wanted):
    matched_lines, rest_lines, seen_counts = [], [], {}
    for raw_line in (accounts_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue                              # blank lines: dropped entirely, not "rest"
        email = line_email(line)
        key = (email or "").lower()
        if email and key in wanted:
            matched_lines.append(line)
            seen_counts[key] = seen_counts.get(key, 0) + 1
        else:
            rest_lines.append(line)
    return matched_lines, rest_lines, seen_counts
```
**One important difference from Profile Finder:** a fully-blank line is dropped entirely here (it isn't an "account" at all, so there's nothing to preserve), whereas Profile Finder keeps a blank-email CSV *row* in `rest` (because a CSV row can carry other column data even with a blank email). Every non-blank line still goes to exactly one of matched/rest, same guarantee as Profile Finder. The line text used everywhere is `line` (stripped of leading/trailing whitespace only) — **never re-split, never reassembled** — so whatever's between and after the first colon is preserved exactly.

```python
def lines_to_text(lines):
    return "\n".join(lines) + ("\n" if lines else "")
```

## CLI

```
python backend/account_finder/filter.py --accounts accounts.txt --emails emails.txt
python backend/account_finder/filter.py --accounts accounts.txt --emails emails.txt --out matches.txt --out-rest rest.txt
```
Same shape as Profile Finder's CLI: `--accounts`/`--emails` required, `--out`/`--out-rest` default to `<accounts-stem>-success.txt` / `<accounts-stem>-unsuccess.txt` next to the input, same summary print format (emails listed/found/not-found, "matched more than one line" report, not-found list).

## API: `POST /api/account-finder/filter`

Request:
```json
{"accountsText": "<raw text, one email:password per line>", "emails": "<raw textarea text>"}
```
Response:
```json
{
  "matchedCount": 3, "restCount": 4, "emailCount": 4, "foundCount": 3,
  "notFound": ["notfound@x.com"], "duplicated": [],
  "matchedText": "a@x.com:pass1\nb@x.com:pass2\n", "restText": "..."
}
```
**Difference from Profile Finder's response:** there is no `rows`-style array of individually-parsed matches. The frontend currently re-splits `matchedText` on `"\n"` itself to build the on-screen preview, and masks the password portion of each line by default (a "Show passwords" checkbox reveals them) — this data is live credentials, arguably more sensitive than the CSV data the other tools handle, so nothing here shows a password on screen without an explicit opt-in. If the rewrite wants server-computed preview rows instead of client-side re-splitting, add something like `"matchedLines": [{"email": "...", "line": "..."}]` — left out originally to avoid sending the same data twice over the wire, but a real array may be worth the redundancy for a cleaner React component.

Same statelessness guarantee as Profile Finder — arguably even more important here given the payload is live login credentials, not just PII. Never written to disk server-side, never touches the SQLite DB.

## Things worth reconsidering in the rewrite

- **Add the `matchedLines` array** to the response (see above) instead of making the frontend re-derive it by splitting text — cleaner contract for a React component, small cost.
- **Consider surfacing malformed lines** (ones with no colon at all) as their own category in the response, rather than silently lumping them into "rest" — right now there's no way to tell "this account didn't match" apart from "this line wasn't a valid account entry at all" without inspecting the rest output by hand.
- The password-masking-by-default behavior lives entirely in the frontend today. If you want it enforced more strongly (e.g. so a future frontend can't forget to mask it), consider whether the API should offer a masked preview field itself (e.g. `matchedPreview: [{"email": "...", "passwordMasked": "••••••••"}]`) alongside the full `matchedText` needed for export — trades a slightly fatter response for a guarantee that stays correct even if someone builds a different frontend against this API later.
