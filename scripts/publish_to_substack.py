#!/usr/bin/env python3
"""
Substack Publisher - Publish markdown posts to Substack as drafts or articles.

Converts markdown to Substack's ProseMirror JSON format and publishes via API.

Requires environment variables:
    SUBSTACK_SID       - substack.sid session cookie value
    SUBSTACK_SUBDOMAIN - e.g., adrianpetcu
    SUBSTACK_USER_ID   - e.g., 106993810

Usage:
    python publish_to_substack.py post.md --dry-run
    python publish_to_substack.py post.md --title "My Post"
    python publish_to_substack.py post.md --publish
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error


# Metadata sections to exclude from the body
EXCLUDED_SECTIONS = {
    "status", "hashtags", "notes", "verdict", "linkedin assessment",
}


def get_config():
    """Read and validate Substack configuration from environment variables."""
    sid = os.environ.get("SUBSTACK_SID")
    if not sid:
        print("Error: SUBSTACK_SID environment variable not set", file=sys.stderr)
        print("Set it to your substack.sid cookie value", file=sys.stderr)
        sys.exit(1)

    subdomain = os.environ.get("SUBSTACK_SUBDOMAIN")
    if not subdomain:
        print("Error: SUBSTACK_SUBDOMAIN environment variable not set", file=sys.stderr)
        print("Example: export SUBSTACK_SUBDOMAIN=adrianpetcu", file=sys.stderr)
        sys.exit(1)

    user_id = os.environ.get("SUBSTACK_USER_ID")
    if not user_id:
        print("Error: SUBSTACK_USER_ID environment variable not set", file=sys.stderr)
        print("Find your user ID in Substack network requests", file=sys.stderr)
        sys.exit(1)

    try:
        user_id = int(user_id)
    except ValueError:
        print(f"Error: SUBSTACK_USER_ID must be a number, got: {user_id}", file=sys.stderr)
        sys.exit(1)

    return {"sid": sid, "subdomain": subdomain, "user_id": user_id}


def parse_markdown(filepath):
    """
    Parse a post markdown file.

    Returns: (title, subtitle, body_lines)
    - title: from first # heading
    - subtitle: from ## Hook section content
    - body_lines: remaining content lines (metadata sections excluded)
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    title = None
    subtitle_lines = []
    body_lines = []
    current_section = None
    in_hook = False

    for line in lines:
        stripped = line.rstrip("\n")

        # Extract title from first H1
        if title is None and stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
            continue

        # Track which H2 section we're in
        if stripped.startswith("## "):
            section_name = stripped[3:].strip().lower()
            current_section = section_name
            in_hook = section_name == "hook"

            # Skip excluded sections entirely
            if section_name in EXCLUDED_SECTIONS:
                continue

            # Don't emit the ## Hook heading itself; its content becomes subtitle
            if in_hook:
                continue

            # Include other section headings in body
            body_lines.append(stripped)
            continue

        # Skip lines in excluded sections
        if current_section in EXCLUDED_SECTIONS:
            continue

        # Collect hook content as subtitle
        if in_hook:
            subtitle_lines.append(stripped)
            continue

        # Everything else goes to body
        body_lines.append(stripped)

    # Clean up subtitle
    subtitle = "\n".join(subtitle_lines).strip()

    # Clean up body - remove leading/trailing blank lines
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()

    return title or "Untitled", subtitle, "\n".join(body_lines)


def parse_inline(text):
    """
    Parse inline markdown to ProseMirror text nodes with marks.

    Handles: **bold**, *italic*, `code`, [links](url)
    """
    if not text:
        return []

    nodes = []
    # Combined pattern for inline elements
    # Order matters: bold before italic, links need special handling
    pattern = re.compile(
        r'(\*\*(.+?)\*\*)'      # bold
        r'|(\*(.+?)\*)'          # italic
        r'|(`(.+?)`)'            # inline code
        r'|(\[([^\]]+)\]\(([^)]+)\))'  # link
    )

    pos = 0
    for m in pattern.finditer(text):
        # Add plain text before this match
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                nodes.append({"type": "text", "text": plain})

        if m.group(2) is not None:  # bold
            nodes.append({
                "type": "text",
                "text": m.group(2),
                "marks": [{"type": "strong"}],
            })
        elif m.group(4) is not None:  # italic
            nodes.append({
                "type": "text",
                "text": m.group(4),
                "marks": [{"type": "em"}],
            })
        elif m.group(6) is not None:  # code
            nodes.append({
                "type": "text",
                "text": m.group(6),
                "marks": [{"type": "code"}],
            })
        elif m.group(8) is not None:  # link
            link_text = m.group(8)
            link_url = m.group(9)
            nodes.append({
                "type": "text",
                "text": link_text,
                "marks": [{"type": "link", "attrs": {"href": link_url}}],
            })

        pos = m.end()

    # Add remaining text
    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            nodes.append({"type": "text", "text": remaining})

    # If no matches were found, return the whole text as one node
    if not nodes and text:
        nodes.append({"type": "text", "text": text})

    return nodes


def make_paragraph(text=""):
    """Create a ProseMirror paragraph node."""
    if not text.strip():
        return {"type": "paragraph"}
    content = parse_inline(text)
    if content:
        return {"type": "paragraph", "content": content}
    return {"type": "paragraph"}


def make_heading(text, level):
    """Create a ProseMirror heading node."""
    content = parse_inline(text)
    node = {"type": "heading", "attrs": {"level": level}}
    if content:
        node["content"] = content
    return node


def md_to_prosemirror(markdown):
    """
    Convert markdown body text to ProseMirror JSON document.

    Two-pass approach:
    - Pass 1: Block-level state machine (paragraphs, headings, lists, code blocks,
      blockquotes, horizontal rules, images)
    - Pass 2: Inline mark parsing via regex (bold, italic, code, links)
    """
    lines = markdown.split("\n")
    content = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Empty line -> skip (paragraphs handle their own grouping)
        if not stripped:
            i += 1
            continue

        # Code block (fenced)
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip().startswith("```"):
                    i += 1
                    break
                code_lines.append(lines[i].rstrip("\n"))
                i += 1
            else:
                i += 1  # unterminated code block

            code_text = "\n".join(code_lines)
            node = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            if code_text:
                node["content"] = [{"type": "text", "text": code_text}]
            content.append(node)
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            content.append({"type": "horizontal_rule"})
            i += 1
            continue

        # Headings (## and ###)
        if stripped.startswith("### "):
            content.append(make_heading(stripped[4:].strip(), 3))
            i += 1
            continue
        if stripped.startswith("## "):
            content.append(make_heading(stripped[3:].strip(), 2))
            i += 1
            continue
        if stripped.startswith("# "):
            content.append(make_heading(stripped[2:].strip(), 1))
            i += 1
            continue

        # Image: ![alt](src)
        img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)', stripped)
        if img_match:
            alt = img_match.group(1)
            src = img_match.group(2)
            if src.startswith(("http://", "https://")):
                node = {
                    "type": "captionedImage",
                    "attrs": {"src": src, "alt": alt},
                }
                if alt:
                    node["content"] = [make_paragraph(alt)]
                content.append(node)
            else:
                print(f"Warning: Skipping local image: {src}", file=sys.stderr)
            i += 1
            continue

        # Blockquote
        if stripped.startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            quote_text = " ".join(quote_lines)
            content.append({
                "type": "blockquote",
                "content": [make_paragraph(quote_text)],
            })
            continue

        # Ordered list
        if re.match(r'^\d+\.\s', stripped):
            items = []
            while i < len(lines):
                item_match = re.match(r'^\d+\.\s+(.*)', lines[i].strip())
                if not item_match:
                    break
                items.append(item_match.group(1))
                i += 1
            content.append({
                "type": "ordered_list",
                "attrs": {"order": 1},
                "content": [
                    {"type": "list_item", "content": [make_paragraph(item)]}
                    for item in items
                ],
            })
            continue

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            items = []
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("- "):
                    items.append(s[2:])
                elif s.startswith("* "):
                    items.append(s[2:])
                else:
                    break
                i += 1
            content.append({
                "type": "bullet_list",
                "content": [
                    {"type": "list_item", "content": [make_paragraph(item)]}
                    for item in items
                ],
            })
            continue

        # Bold label on standalone line -> H3 heading
        # e.g., "**Paragraph 1 - The Problem:**"
        bold_label = re.match(r'^\*\*(.+?):\*\*\s*$', stripped)
        if bold_label:
            content.append(make_heading(bold_label.group(1), 3))
            i += 1
            continue

        # Regular paragraph (collect consecutive non-empty, non-special lines)
        para_lines = []
        while i < len(lines):
            s = lines[i].strip()
            if not s:
                i += 1
                break
            # Stop if next line is a block-level element
            if (s.startswith("#") or s.startswith("```") or s.startswith("> ")
                    or s.startswith("- ") or s.startswith("* ")
                    or re.match(r'^\d+\.\s', s)
                    or re.match(r'^!\[', s)
                    or s in ("---", "***", "___")
                    or re.match(r'^\*\*(.+?):\*\*\s*$', s)):
                break
            para_lines.append(s)
            i += 1

        if para_lines:
            text = " ".join(para_lines)
            content.append(make_paragraph(text))

    return {"type": "doc", "content": content if content else [{"type": "paragraph"}]}


def _make_headers(config):
    """Build HTTP headers that pass Cloudflare checks."""
    base_url = f"https://{config['subdomain']}.substack.com"
    return {
        "Content-Type": "application/json",
        "Cookie": f"substack.sid={config['sid']}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": base_url,
        "Referer": f"{base_url}/publish/post?type=newsletter",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def create_draft(config, title, subtitle, body_json, audience="everyone"):
    """
    Create a draft on Substack.

    Returns: draft data dict (includes 'id' for the draft)
    """
    url = f"https://{config['subdomain']}.substack.com/api/v1/drafts"

    payload = {
        "draft_title": title,
        "draft_subtitle": subtitle,
        "draft_podcast_url": None,
        "draft_podcast_duration": None,
        "draft_body": json.dumps(body_json),
        "section_chosen": False,
        "draft_section_id": None,
        "draft_bylines": [{"id": config["user_id"], "is_guest": False}],
        "audience": audience,
        "type": "newsletter",
    }

    headers = _make_headers(config)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        if e.code in (401, 403):
            print("Error: Authentication failed. Your SUBSTACK_SID may be expired.", file=sys.stderr)
            print(f"Detail: {error_body}", file=sys.stderr)
            print("Get a fresh cookie from your browser's DevTools.", file=sys.stderr)
        else:
            print(f"API Error ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Network Error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    return result


def publish_draft(config, draft_id):
    """Publish an existing draft on Substack."""
    url = f"https://{config['subdomain']}.substack.com/api/v1/drafts/{draft_id}/publish"

    payload = {"send": True}

    headers = _make_headers(config)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"Publish Error ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Network Error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Publish markdown posts to Substack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s post.md --dry-run
  %(prog)s post.md --title "Custom Title"
  %(prog)s post.md --publish --audience everyone
        """,
    )
    parser.add_argument("file", help="Markdown file to publish")
    parser.add_argument("--title", help="Override post title (default: from # heading)")
    parser.add_argument("--subtitle", help="Override subtitle (default: from ## Hook)")
    parser.add_argument("--publish", action="store_true", help="Publish immediately (default: draft only)")
    parser.add_argument(
        "--audience",
        choices=["everyone", "paid"],
        default="everyone",
        help="Post audience. Default: everyone",
    )
    parser.add_argument("--dry-run", action="store_true", help="Output ProseMirror JSON without API calls")

    args = parser.parse_args()

    # Parse markdown
    print(f"Parsing: {args.file}", file=sys.stderr)
    title, subtitle, body = parse_markdown(args.file)

    if args.title:
        title = args.title
    if args.subtitle:
        subtitle = args.subtitle

    print(f"Title: {title}", file=sys.stderr)
    if subtitle:
        print(f"Subtitle: {subtitle[:80]}{'...' if len(subtitle) > 80 else ''}", file=sys.stderr)

    # Convert to ProseMirror
    body_json = md_to_prosemirror(body)

    # Dry run - just output JSON
    if args.dry_run:
        print(json.dumps(body_json, indent=2))
        return

    # Create draft
    config = get_config()
    print(f"Creating draft on {config['subdomain']}.substack.com...", file=sys.stderr)

    draft = create_draft(config, title, subtitle, body_json, args.audience)
    draft_id = draft.get("id")
    draft_url = f"https://{config['subdomain']}.substack.com/publish/post/{draft_id}"

    print(f"Draft created: {draft_url}", file=sys.stderr)

    # Publish if requested
    if args.publish:
        print("Publishing...", file=sys.stderr)
        result = publish_draft(config, draft_id)
        slug = result.get("slug", "")
        post_url = f"https://{config['subdomain']}.substack.com/p/{slug}"
        print(f"Published: {post_url}", file=sys.stderr)
        print(post_url)
    else:
        print(draft_url)


if __name__ == "__main__":
    main()
