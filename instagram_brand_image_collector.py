#!/usr/bin/env python3
"""Collect public Instagram images, likes, captions, timestamps and comments.

Pipeline: last two years of public posts -> download images -> save like count,
caption, UTC timestamp and optionally comments to one JSON file per target.

Install:
    python -m pip install instaloader

Targets: an Instagram username, a profile URL, or a hashtag written with #.

Anonymous collection (recommended), followed by browser comment collection:
    python instagram_brand_image_collector.py aesop --skip-comments
    python instagram_comment_scraper.py instagram_brand_data/posts/aesop_posts.json

Hashtag example:
    python instagram_brand_image_collector.py "#maisonmargiela" --skip-comments

Only public posts are accessed. Stories, private content and access-control
bypass are not used. For anonymous collection, always use --skip-comments.
The separate Playwright script supports manual browser login for comments.
Direct Instaloader login remains available through --login or --browser firefox.
"""

import argparse
import getpass
import glob
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlparse

import instaloader


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def username_from_url(account_url):
    parsed = urlparse(account_url)
    if (parsed.hostname or "").lower() not in {"instagram.com", "www.instagram.com"}:
        raise ValueError(f"Not an Instagram URL: {account_url}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9._]+", parts[0]):
        raise ValueError(f"Not a profile URL: {account_url}")
    if parts[0].lower() in {"p", "reel", "reels", "explore", "stories"}:
        raise ValueError(f"Not a profile URL: {account_url}")
    return parts[0]


def hashtag_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Invalid hashtag: #{value}")
    return value


def parse_target(value):
    """Return ("profile", username) or ("hashtag", name) for one CLI argument."""
    value = value.strip()
    if value.startswith("#"):
        return "hashtag", hashtag_name(value[1:])
    if "instagram.com/explore/tags/" in value.lower():
        return "hashtag", hashtag_name(value.rstrip("/").split("/")[-1])
    if "instagram.com" in value.lower():
        return "profile", username_from_url(value if "://" in value else f"https://{value}")
    username = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise ValueError(f"Invalid Instagram username: {value}")
    return "profile", username


def parse_date(value):
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def make_loader(output_dir, delay):
    return instaloader.Instaloader(
        dirname_pattern=str(output_dir / "images" / "{target}"),
        filename_pattern="{date_utc:%Y%m%dT%H%M%SZ}_{shortcode}",
        download_pictures=True,
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        download_geotags=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        request_timeout=30,
        max_connection_attempts=2,
        quiet=False,
    )


def login_from_browser(loader, browser):
    """Copy Instagram cookies from the browser into the Instaloader session."""
    if browser != "firefox":
        raise ValueError(f"Unsupported browser: {browser}")

    # Firefox locks its cookie database, so read a temporary copy instead.
    profiles = glob.glob(os.path.expanduser("~/Library/Application Support/Firefox/Profiles/*"))
    for profile in profiles:
        cookie_db = os.path.join(profile, "cookies.sqlite")
        if not os.path.exists(cookie_db):
            continue
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as temp:
            with open(cookie_db, "rb") as source:
                temp.write(source.read())
            temp.flush()
            connection = sqlite3.connect(temp.name)
            try:
                rows = connection.execute(
                    "SELECT name, value FROM moz_cookies WHERE host LIKE '%instagram.com'"
                ).fetchall()
            finally:
                connection.close()
        cookies = dict(rows)
        if "sessionid" not in cookies:
            continue
        loader.context.load_session(cookies.get("ds_user_id", ""), cookies)
        try:
            # test_login() uses a deprecated endpoint; check a profile instead.
            instaloader.Profile.from_username(loader.context, "instagram")
        except Exception:
            continue
        loader.context.log(f"Loaded Instagram session from Firefox ({profile})")
        return
    raise RuntimeError(
        "No logged-in Instagram session found in Firefox. Log in at "
        "https://www.instagram.com in Firefox, then run the script again."
    )


def post_image_files(image_dir, shortcode):
    """Return image files saved for one post (carousel slides get _1, _2, ...)."""
    if not image_dir.exists():
        return []
    pattern = re.compile(rf"_{re.escape(shortcode)}(_\d+)?$")
    return sorted(
        path for path in image_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and pattern.search(path.stem)
    )


def comment_record(comment):
    return {
        "username": comment.owner.username,
        "text": comment.text,
        "timestamp_utc": as_utc(comment.created_at_utc).isoformat(),
        "likes": comment.likes_count,
    }


def extract_comments(post, max_comments):
    comments = []
    for comment in islice(post.get_comments(), max_comments):
        record = comment_record(comment)
        record["replies"] = [comment_record(reply) for reply in comment.answers]
        comments.append(record)
    return comments


def collect_posts(label, source_url, posts, args, loader):
    since = parse_date(args.since) or (datetime.now(timezone.utc) - timedelta(days=730))
    until_end = parse_date(args.until)
    if until_end:
        until_end += timedelta(days=1)

    # Posts are returned newest first, so scanning can stop once older than `since`.
    eligible_posts = []
    for scanned, post in enumerate(islice(posts, args.scan_posts), start=1):
        if scanned % 25 == 0:
            print(f"{label}: scanned {scanned} posts...", flush=True)
        post_date = as_utc(post.date_utc)
        if post_date < since:
            break
        if until_end and post_date >= until_end:
            continue
        if post.is_video:
            continue
        eligible_posts.append(post)

    if not eligible_posts:
        raise RuntimeError(f"{label}: no eligible public image posts found")

    print(f"{label}: {len(eligible_posts)} eligible image posts found")
    image_dir = args.output / "images" / label
    records = []
    attempted = 0

    for post in eligible_posts:
        attempted += 1
        print(f"{label}: processing {post.shortcode}")
        loader.download_post(post, target=label)
        images = post_image_files(image_dir, post.shortcode)
        if not images:
            print(f"{label}: no image saved for {post.shortcode}; skipping")
            continue

        records.append({
            "shortcode": post.shortcode,
            "post_url": f"https://www.instagram.com/p/{post.shortcode}/",
            "owner_username": post.owner_username,
            "post_type": post.typename,
            "timestamp_utc": as_utc(post.date_utc).isoformat(),
            "caption": post.caption or "",
            "like_count": post.likes,
            "comment_count": post.comments,
            "image_files": [str(path) for path in images],
            "comments": [] if args.skip_comments else extract_comments(post, args.max_comments),
        })
        time.sleep(args.delay)

    if not records:
        raise RuntimeError(f"{label}: no images downloaded from {attempted} posts")

    posts_dir = args.output / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    posts_path = posts_dir / f"{label}_posts.json"
    posts_path.write_text(
        json.dumps(
            {"target": label, "source_url": source_url, "posts": records},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return {
        "target": label,
        "source_url": source_url,
        "eligible_posts": len(eligible_posts),
        "posts_saved": len(records),
        "images_saved": sum(len(record["image_files"]) for record in records),
        "comments_saved": sum(
            len(record["comments"]) + sum(len(c["replies"]) for c in record["comments"])
            for record in records
        ),
        "posts_file": str(posts_path),
    }


def collect_target(kind, name, args, loader):
    if kind == "hashtag":
        source_url = f"https://www.instagram.com/explore/tags/{name}/"
        hashtag = instaloader.Hashtag.from_name(loader.context, name)
        return collect_posts(f"hashtag_{name}", source_url,
                             hashtag.get_posts_resumable(), args, loader)

    source_url = f"https://www.instagram.com/{name}/"
    profile = instaloader.Profile.from_username(loader.context, name)
    if profile.is_private:
        raise RuntimeError(f"{name}: private profiles are not supported")
    return collect_posts(name, source_url, profile.get_posts(), args, loader)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+",
                        help="Usernames, profile URLs, or hashtags such as \"#maisonmargiela\"")
    parser.add_argument("--output", type=Path, default=Path("instagram_brand_data"))
    parser.add_argument("--scan-posts", type=int, default=600,
                        help="Maximum recent posts to scan per target (default: 600)")
    parser.add_argument("--max-comments", type=int, default=None,
                        help="Maximum top-level comments per post (default: all)")
    parser.add_argument("--skip-comments", action="store_true",
                        help="Do not fetch comments; images, captions and timestamps need no login")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="Delay between processed posts in seconds (default: 5)")
    parser.add_argument("--login", metavar="INSTAGRAM_USERNAME",
                        help="Log in with your Instagram account (needed for comments)")
    parser.add_argument("--browser", choices=["firefox"], default=None,
                        help="Reuse your Instagram login from this browser (alternative to --login)")
    parser.add_argument("--since", help="Inclusive UTC date (default: two years ago)")
    parser.add_argument("--until", help="Inclusive UTC date, e.g. 2026-11-07")
    args = parser.parse_args()

    if args.scan_posts < 1:
        parser.error("--scan-posts must be positive")
    if args.max_comments is not None and args.max_comments < 0:
        parser.error("--max-comments cannot be negative")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    try:
        targets = [parse_target(value) for value in args.targets]
    except ValueError as exc:
        parser.error(str(exc))

    args.output = args.output.expanduser().resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    loader = make_loader(args.output, args.delay)
    if args.browser and args.login:
        parser.error("Use --login or --browser, not both")
    if args.browser:
        try:
            login_from_browser(loader, args.browser)
        except Exception as exc:
            raise SystemExit(f"Could not use {args.browser} login: {exc}")
    elif args.login:
        try:
            loader.load_session_from_file(args.login)
            print(f"Using saved session for {args.login}.", flush=True)
        except FileNotFoundError:
            password = os.getenv("INSTAGRAM_PASSWORD") or getpass.getpass(
                f"Instagram password for {args.login}: "
            )
            try:
                loader.login(args.login, password)
                loader.save_session_to_file()
                print(f"Logged in as {args.login}; session saved for future runs.", flush=True)
            except Exception as exc:
                raise SystemExit(f"Instagram login failed: {exc}")
    report = {"completed": [], "errors": []}

    for kind, name in targets:
        label = f"#{name}" if kind == "hashtag" else name
        try:
            report["completed"].append(collect_target(kind, name, args, loader))
        except (instaloader.exceptions.LoginRequiredException,
                instaloader.exceptions.ConnectionException) as exc:
            report["errors"].append({"target": label, "error": str(exc)})
            print(f"Stopping collection: Instagram access failed for {label}: {exc}")
            break  # Do not repeatedly retry after a platform/network block.
        except Exception as exc:
            report["errors"].append({"target": label, "error": str(exc)})
            print(f"{label}: {exc}")

    report_path = args.output / "collection_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Collection report: {report_path}")


if __name__ == "__main__":
    main()
