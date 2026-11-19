#!/usr/bin/env python3
"""Add comments to an existing Instagram collector JSON file with Playwright.

Run instagram_brand_image_collector.py with --skip-comments first, then pass the
resulting posts JSON to this script. A visible Firefox window opens so you can
log in manually. The script never enters your password and does not bypass
CAPTCHAs, checkpoints, rate limits, or other access controls.

Install (preferably in a virtual environment):
    python -m pip install playwright
    python -m playwright install firefox

Example:
    python instagram_comment_scraper.py \
      instagram_brand_data/posts/aesop_posts.json --max-comments 100

Comments are saved back to the same JSON after every post. Existing comments
are kept unless --overwrite is supplied. A separate persistent browser profile
is stored in .instagram_firefox_profile so later runs can reuse that login.
Use --browser chromium if you prefer Chromium instead.
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


LOCAL_BROWSER_DIR = Path(__file__).resolve().parent / ".playwright-browsers"
if LOCAL_BROWSER_DIR.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(LOCAL_BROWSER_DIR))

SECURITY_URL_PARTS = ("/challenge/", "/checkpoint/", "/auth_platform/", "/accounts/login")
SECURITY_TEXT = (
    "help us confirm it's you",
    "help us confirm it’s you",
    "suspicious login attempt",
    "captcha",
    "checkpoint required",
)
EXTRACT_COMMENTS_JS = r"""
() => {
  const records = [];
  const seen = new Set();

  for (const heading of document.querySelectorAll('h3')) {
    const username = (heading.innerText || '').trim().split(/\s+/)[0];
    if (!/^[A-Za-z0-9._]+$/.test(username)) continue;

    // A comment heading and its timestamp share a small container. Walking up
    // avoids Instagram's frequently changing generated CSS class names.
    let container = heading.parentElement;
    let timeElement = null;
    for (let depth = 0; container && depth < 7; depth += 1) {
      timeElement = container.querySelector('time[datetime]');
      if (timeElement) break;
      container = container.parentElement;
    }
    if (!container || !timeElement) continue;

    // Comment text is normally a span beside the h3. Prefer a direct child,
    // then use the longest suitable span inside the same comment container.
    const spans = Array.from(container.querySelectorAll('span')).filter(span => {
      const text = (span.innerText || '').trim();
      return text && !heading.contains(span) && !span.closest('button')
        && !span.closest('time') && !span.querySelector('h3');
    });
    const directSpans = spans.filter(span => span.parentElement === container);
    const candidates = directSpans.length ? directSpans : spans;
    candidates.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
    const text = candidates.length ? (candidates[0].innerText || '').trim() : '';
    if (!text || text === username) continue;

    const timestamp = timeElement.getAttribute('datetime') || '';
    const key = JSON.stringify([username, text, timestamp]);
    if (seen.has(key)) continue;
    seen.add(key);

    const containerText = container.innerText || '';
    const likeMatch = containerText.match(/(\d[\d,]*)\s+likes?/i);
    records.push({
      username,
      text,
      timestamp_utc: timestamp,
      likes: likeMatch ? Number(likeMatch[1].replace(/,/g, '')) : null,
      replies: []
    });
  }
  return records;
}
"""


LOAD_MORE_JS = r"""
() => {
  const pattern = /^(load more comments|view more comments|view all \d+ comments|view( all)?( \d+)? replies?|show more comments)$/i;
  const elements = Array.from(document.querySelectorAll('button, [role="button"], [aria-label]'));
  for (const element of elements) {
    const labels = [element.getAttribute('aria-label') || '', element.innerText || ''];
    if (!labels.some(label => pattern.test(label.trim().replace(/\s+/g, ' ')))) continue;
    const target = element.closest('button, [role="button"]') || element;
    target.scrollIntoView({block: 'center'});
    target.click();
    return true;
  }
  return false;
}
"""


SCROLL_COMMENTS_JS = r"""
() => {
  const replies = Array.from(document.querySelectorAll('button, [role="button"]'))
    .filter(element => (element.innerText || '').trim() === 'Reply');
  for (const reply of replies) {
    let container = reply.parentElement;
    while (container) {
      if (container.scrollHeight > container.clientHeight + 40) {
        const before = container.scrollTop;
        container.scrollTop = container.scrollHeight;
        return container.scrollTop !== before;
      }
      container = container.parentElement;
    }
  }
  return false;
}
"""


class AccessBlocked(RuntimeError):
    """Instagram presented a login, checkpoint, CAPTCHA, or similar stop page."""


class ApplicationJSONParser(HTMLParser):
    """Collect application/json script bodies from an Instagram page."""

    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/json":
            self.capture = True
            self.parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            self.scripts.append("".join(self.parts))
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def comment_from_node(node):
    """Convert an XIGComment-shaped dictionary to the collector's schema."""
    user = node.get("user") or node.get("owner") or {}
    username = user.get("username")
    text = node.get("text")
    created = node.get("created_at", node.get("created_at_utc"))
    if not username or text is None or created is None:
        return None
    try:
        timestamp = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        timestamp = str(created)

    likes = node.get("comment_like_count")
    if likes is None and isinstance(node.get("edge_liked_by"), dict):
        likes = node["edge_liked_by"].get("count")
    comment_id = node.get("id") or node.get("pk")
    parent_id = node.get("parent_comment_id")
    return {
        "comment_id": str(comment_id) if comment_id is not None else None,
        "parent_comment_id": str(parent_id) if parent_id is not None else None,
        "username": username,
        "text": text,
        "timestamp_utc": timestamp,
        "likes": likes,
        "reply_count": node.get("child_comment_count", 0),
        "replies": [],
    }


def comments_from_data(data):
    """Find comment nodes anywhere in an embedded or GraphQL JSON response."""
    records = []

    def walk(value):
        if isinstance(value, dict):
            is_comment = value.get("__typename") == "XIGComment" or (
                "comment_like_count" in value
                and "parent_comment_id" in value
                and "created_at" in value
                and "text" in value
            )
            if is_comment:
                record = comment_from_node(value)
                if record:
                    records.append(record)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return records


def comments_from_html(html):
    """Extract comments embedded in Instagram's application/json script tags."""
    parser = ApplicationJSONParser()
    parser.feed(html)
    records = []
    for script in parser.scripts:
        if "XIGComment" not in script and "comments_connection" not in script:
            continue
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        records = merge_comments(records, comments_from_data(data))
    return records


def load_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        raise ValueError("Expected a collector JSON object containing a 'posts' list")
    return data


def save_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def shortcode_from_url(url):
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"p", "reel"}:
        return parts[1]
    return None


def comment_key(item):
    if item.get("comment_id"):
        return "id", item["comment_id"]
    return "content", (
        item.get("username"), item.get("text"), item.get("timestamp_utc")
    )


def merge_comments(existing, new):
    """Deduplicate comments and combine replies collected at different times."""
    merged = [dict(item) for item in existing]
    indices = {comment_key(item): index for index, item in enumerate(merged)}
    for item in new:
        key = comment_key(item)
        if key not in indices:
            indices[key] = len(merged)
            merged.append(dict(item))
            continue
        current = merged[indices[key]]
        current["replies"] = merge_comments(
            current.get("replies", []), item.get("replies", [])
        )
        for field in ("parent_comment_id", "timestamp_utc", "likes", "reply_count"):
            if current.get(field) is None and item.get(field) is not None:
                current[field] = item[field]
    return merged


def nest_comments(records):
    """Turn flat XIGComment records into top-level comments with nested replies."""
    nodes = [dict(record, replies=list(record.get("replies", []))) for record in records]
    by_id = {node["comment_id"]: node for node in nodes if node.get("comment_id")}
    roots = []
    for node in nodes:
        parent_id = node.get("parent_comment_id")
        if parent_id and parent_id in by_id and parent_id != node.get("comment_id"):
            by_id[parent_id]["replies"].append(node)
        else:
            roots.append(node)
    return roots


def check_access(page):
    url = page.url.lower()
    if any(part in url for part in SECURITY_URL_PARTS):
        raise AccessBlocked(f"Instagram requires attention at {page.url}")
    body = page.locator("body").inner_text(timeout=5000)[:30000].lower()
    if any(text in body for text in SECURITY_TEXT):
        raise AccessBlocked("Instagram presented a checkpoint or CAPTCHA; stopping")


def extract_comments(page, max_comments, network_comments):
    comments = []
    stalled = 0

    def collect_available(current):
        current = merge_comments(current, comments_from_html(page.content()))
        current = merge_comments(current, network_comments)
        return merge_comments(current, page.evaluate(EXTRACT_COMMENTS_JS))

    # The cap is only a safeguard against an infinite loop when all comments
    # were requested. Normal runs stop at max_comments or after stalled loading.
    for _ in range(500):
        comments = collect_available(comments)
        if max_comments and len(comments) >= max_comments:
            break
        activated = page.evaluate(LOAD_MORE_JS)
        if not activated:
            activated = page.evaluate(SCROLL_COMMENTS_JS)
        if not activated:
            break
        page.wait_for_timeout(1500)
        updated = collect_available(comments)
        stalled = stalled + 1 if len(updated) == len(comments) else 0
        comments = updated
        if stalled >= 3:
            break
    return comments[:max_comments] if max_comments else comments


def save_diagnostics(page, output_dir, post_url):
    """Save UTF-8 HTML named after the shortcode in the post URL; no screenshot."""
    shortcode = shortcode_from_url(post_url)
    if not shortcode:
        raise ValueError(f"Cannot derive a diagnostic filename from {post_url}")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{shortcode}.html").write_text(page.content(), encoding="utf-8")


def collect(args):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Run: python -m pip install playwright && "
            "python -m playwright install firefox"
        ) from exc

    posts_path = args.posts_file.expanduser().resolve()
    data = load_json(posts_path)
    profile_dir = args.profile_dir or Path(f".instagram_{args.browser}_profile")
    profile_dir = profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_dir = posts_path.parent / "comment_diagnostics"
    processed = 0

    with sync_playwright() as playwright:
        browser_type = getattr(playwright, args.browser)
        context = browser_type.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        network_comments = []

        def capture_comments(response):
            try:
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                url = response.url.lower()
                if "graphql" not in url and "comment" not in url:
                    return
                network_comments.extend(comments_from_data(response.json()))
            except Exception:
                # Non-JSON responses and requests aborted during navigation are
                # irrelevant; embedded JSON and later responses still remain.
                pass

        page.on("response", capture_comments)
        try:
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            input(
                "\nUse the visible browser to log in normally if needed. "
                "Complete any Instagram security check manually, then press Enter here "
                "to start collecting comments (or Ctrl+C to cancel)..."
            )
            check_access(page)

            for post in data["posts"]:
                if args.max_posts is not None and processed >= args.max_posts:
                    break
                shortcode = post.get("shortcode") or shortcode_from_url(post.get("post_url", ""))
                if not shortcode or shortcode_from_url(post.get("post_url", "")) != shortcode:
                    print(f"Skipping a record with an invalid or mismatched post URL: {shortcode}")
                    continue
                if (post.get("comments")
                        and post.get("comments_status") in (None, "collected")
                        and not args.overwrite):
                    continue
                if post.get("comment_count") == 0:
                    post["comments"] = []
                    post["comments_status"] = "no_comments"
                    save_json(posts_path, data)
                    continue

                processed += 1
                print(f"{shortcode}: opening post ({processed})", flush=True)
                try:
                    network_comments.clear()
                    page.goto(post["post_url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3000)
                    check_access(page)
                    # Confirm the browser is still on the requested post before
                    # attaching comments to this JSON record.
                    if shortcode_from_url(page.url) != shortcode:
                        raise AccessBlocked(
                            f"Expected post {shortcode}, but Instagram opened {page.url}"
                        )
                    comments = extract_comments(page, args.max_comments, network_comments)
                    if not comments and post.get("comment_count", 0) > 0:
                        if post.get("comments"):
                            post["comments_status"] = "partial"
                            print(f"{shortcode}: no additional comments found")
                        else:
                            post["comments_status"] = "not_found"
                            save_diagnostics(page, diagnostics_dir, post["post_url"])
                            print(
                                f"{shortcode}: no comments found in embedded JSON or the page. "
                                f"Diagnostics saved to {diagnostics_dir}"
                            )
                    else:
                        post["comments"] = merge_comments(
                            [] if args.overwrite else post.get("comments", []),
                            nest_comments(comments),
                        )
                        enough = len(comments) >= post.get("comment_count", 0) or (
                            args.max_comments and len(comments) >= args.max_comments
                        )
                        post["comments_status"] = "collected" if enough else "partial"
                        print(f"{shortcode}: saved {len(comments)} comments", flush=True)
                    post["comments_source"] = "playwright"
                    post["comments_collected_at"] = datetime.now(timezone.utc).isoformat()
                    save_json(posts_path, data)
                    time.sleep(args.delay)
                except AccessBlocked as exc:
                    post["comments_status"] = "blocked"
                    save_json(posts_path, data)
                    print(f"Stopping: {exc}")
                    break
                except (PlaywrightError, PlaywrightTimeoutError) as exc:
                    post["comments_status"] = "error"
                    save_json(posts_path, data)
                    print(f"{shortcode}: browser error: {exc}")
        finally:
            context.close()

    print(f"Updated JSON: {posts_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("posts_file", type=Path,
                        help="Posts JSON produced by instagram_brand_image_collector.py")
    parser.add_argument("--browser", choices=["firefox", "chromium"], default="firefox",
                        help="Browser used for manual login and comments (default: firefox)")
    parser.add_argument("--profile-dir", type=Path, default=None,
                        help="Optional custom persistent browser profile directory")
    parser.add_argument("--max-posts", type=int, default=None,
                        help="Maximum posts to visit this run (default: all remaining)")
    parser.add_argument("--max-comments", type=int, default=100,
                        help="Maximum comments per post; 0 means no comment limit (default: 100)")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="Delay between posts in seconds (default: 5)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Recollect posts that already have comments")
    args = parser.parse_args()

    if args.max_posts is not None and args.max_posts < 1:
        parser.error("--max-posts must be positive")
    if args.max_comments < 0:
        parser.error("--max-comments cannot be negative")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    collect(args)


if __name__ == "__main__":
    main()
