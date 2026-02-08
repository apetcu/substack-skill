#!/usr/bin/env python3
"""
Substack Publisher - Publish markdown posts to Substack as drafts or articles.

Converts markdown to Substack's ProseMirror JSON format and publishes via API.
Supports uploading local images.

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
import base64
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path


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

    # Split hook: first paragraph -> subtitle, rest -> prepend to body
    hook_text = "\n".join(subtitle_lines).strip()
    if hook_text:
        # Split on first blank line or --- divider to get first paragraph
        parts = re.split(r'\n\s*\n', hook_text, maxsplit=1)
        subtitle = parts[0].strip()
        if len(parts) > 1:
            # Remove leading --- divider if present
            remainder = parts[1].strip()
            remainder = re.sub(r'^---\s*\n?', '', remainder).strip()
            if remainder:
                body_lines = remainder.split("\n") + [""] + body_lines
    else:
        subtitle = ""

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
    pattern = re.compile(
        r'(\*\*(.+?)\*\*)'      # bold
        r'|(\*(.+?)\*)'          # italic
        r'|(`(.+?)`)'            # inline code
        r'|(\[([^\]]+)\]\(([^)]+)\))'  # link
    )

    pos = 0
    for m in pattern.finditer(text):
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
            nodes.append({
                "type": "text",
                "text": m.group(8),
                "marks": [{"type": "link", "attrs": {"href": m.group(9)}}],
            })

        pos = m.end()

    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            nodes.append({"type": "text", "text": remaining})

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


def md_to_prosemirror(markdown, base_dir="."):
    """
    Convert markdown body text to ProseMirror JSON document.

    Returns: (doc, local_images)
    - doc: ProseMirror JSON document
    - local_images: list of {"index": int, "path": str, "alt": str} for local images
    """
    lines = markdown.split("\n")
    content = []
    local_images = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Empty line
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
                i += 1

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

        # Headings
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
                # Local image - resolve path and track for upload
                image_path = os.path.join(base_dir, src)
                if os.path.isfile(image_path):
                    placeholder = {
                        "type": "captionedImage",
                        "attrs": {"_local_path": image_path, "_alt": alt},
                    }
                    local_images.append({
                        "index": len(content),
                        "path": image_path,
                        "alt": alt,
                    })
                    content.append(placeholder)
                    print(f"Found local image: {src}", file=sys.stderr)
                else:
                    print(f"Warning: Local image not found: {image_path}", file=sys.stderr)
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
        bold_label = re.match(r'^\*\*(.+?):\*\*\s*$', stripped)
        if bold_label:
            content.append(make_heading(bold_label.group(1), 3))
            i += 1
            continue

        # Regular paragraph
        para_lines = []
        while i < len(lines):
            s = lines[i].strip()
            if not s:
                i += 1
                break
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

    doc = {"type": "doc", "content": content if content else [{"type": "paragraph"}]}
    return doc, local_images


# --- Image handling ---

def detect_mime_type(image_bytes):
    """Detect image MIME type from magic bytes."""
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        return "image/jpeg"
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    return "image/png"


def get_image_dimensions(image_bytes):
    """Get (width, height) from image bytes. Returns (0, 0) if unknown."""
    # PNG: width/height in IHDR chunk at bytes 16-23
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        try:
            width = struct.unpack('>I', image_bytes[16:20])[0]
            height = struct.unpack('>I', image_bytes[20:24])[0]
            return width, height
        except struct.error:
            return 0, 0

    # JPEG: scan for SOF marker
    if image_bytes[:2] == b'\xff\xd8':
        try:
            i = 2
            while i < len(image_bytes) - 1:
                if image_bytes[i] != 0xFF:
                    break
                marker = image_bytes[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    height = struct.unpack('>H', image_bytes[i+5:i+7])[0]
                    width = struct.unpack('>H', image_bytes[i+7:i+9])[0]
                    return width, height
                elif marker == 0xD9:
                    break
                elif marker in range(0xD0, 0xD9) or marker == 0x01:
                    i += 2
                else:
                    length = struct.unpack('>H', image_bytes[i+2:i+4])[0]
                    i += 2 + length
        except (struct.error, IndexError):
            return 0, 0

    return 0, 0


def upload_image(config, image_path, post_id):
    """
    Upload a local image to Substack.

    Returns: dict with image metadata, or None on failure.
    """
    image_bytes = Path(image_path).read_bytes()
    mime_type = detect_mime_type(image_bytes)
    b64_data = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{mime_type};base64,{b64_data}"

    url = f"https://{config['subdomain']}.substack.com/api/v1/image"
    payload = {
        "image": data_uri,
        "postId": post_id,
    }

    headers = _make_headers(config)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"Image upload error ({e.code}): {error_body}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"Image upload network error: {e.reason}", file=sys.stderr)
        return None

    width, height = get_image_dimensions(image_bytes)

    return {
        "src": result.get("url", result.get("src", "")),
        "width": result.get("width", width),
        "height": result.get("height", height),
        "bytes": len(image_bytes),
        "type": mime_type,
        "response": result,
    }


def build_image_node(config, image_info, post_id, alt=None):
    """Build a captionedImage node with image2 child from upload response."""
    src = image_info["src"]
    internal_redirect = (
        f"https://{config['subdomain']}.substack.com/i/{post_id}"
        f"?img={urllib.parse.quote(src, safe='')}"
    )

    image2_node = {
        "type": "image2",
        "attrs": {
            "src": src,
            "srcNoWatermark": None,
            "fullscreen": None,
            "imageSize": None,
            "height": image_info.get("height"),
            "width": image_info.get("width"),
            "resizeWidth": None,
            "bytes": image_info.get("bytes"),
            "alt": alt,
            "title": None,
            "type": image_info.get("type", "image/png"),
            "href": None,
            "belowTheFold": False,
            "topImage": False,
            "internalRedirect": internal_redirect,
            "isProcessing": False,
            "align": None,
            "offset": False,
        },
    }

    return {
        "type": "captionedImage",
        "content": [image2_node],
    }


def resolve_local_images(config, doc, local_images, post_id):
    """Upload local images and replace placeholder nodes in the doc."""
    if not local_images:
        return doc

    for img_info in local_images:
        idx = img_info["index"]
        path = img_info["path"]
        alt = img_info["alt"]

        print(f"Uploading image: {os.path.basename(path)}...", file=sys.stderr)
        upload_result = upload_image(config, path, post_id)

        if upload_result and upload_result["src"]:
            node = build_image_node(config, upload_result, post_id, alt=alt or None)
            doc["content"][idx] = node
            print(f"  -> {upload_result['src']}", file=sys.stderr)
        else:
            doc["content"][idx] = {"type": "paragraph"}
            print(f"  Failed to upload: {path}", file=sys.stderr)

    return doc


# --- API helpers ---

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
    """Create a draft on Substack. Returns: draft data dict."""
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


def update_draft(config, draft_id, title, subtitle, body_json, audience="everyone"):
    """Update an existing draft on Substack via PUT."""
    url = f"https://{config['subdomain']}.substack.com/api/v1/drafts/{draft_id}"

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
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"Update draft error ({e.code}): {error_body}", file=sys.stderr)
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
  %(prog)s post.md --update 187272495
        """,
    )
    parser.add_argument("file", help="Markdown file to publish")
    parser.add_argument("--title", help="Override post title (default: from # heading)")
    parser.add_argument("--subtitle", help="Override subtitle (default: from ## Hook)")
    parser.add_argument("--publish", action="store_true", help="Publish immediately (default: draft only)")
    parser.add_argument("--update", metavar="POST_ID", type=int, help="Update an existing draft/post by ID instead of creating a new one")
    parser.add_argument(
        "--audience",
        choices=["everyone", "paid"],
        default="everyone",
        help="Post audience. Default: everyone",
    )
    parser.add_argument("--dry-run", action="store_true", help="Output ProseMirror JSON without API calls")

    args = parser.parse_args()

    # Parse markdown
    filepath = Path(args.file)
    print(f"Parsing: {args.file}", file=sys.stderr)
    title, subtitle, body = parse_markdown(args.file)

    if args.title:
        title = args.title
    if args.subtitle:
        subtitle = args.subtitle

    print(f"Title: {title}", file=sys.stderr)
    if subtitle:
        print(f"Subtitle: {subtitle[:80]}{'...' if len(subtitle) > 80 else ''}", file=sys.stderr)

    # Convert to ProseMirror (resolve image paths relative to the markdown file)
    base_dir = str(filepath.parent) if filepath.parent != Path() else "."
    body_json, local_images = md_to_prosemirror(body, base_dir=base_dir)

    if local_images:
        print(f"Found {len(local_images)} local image(s) to upload", file=sys.stderr)

    # Dry run - just output JSON
    if args.dry_run:
        print(json.dumps(body_json, indent=2))
        return

    config = get_config()

    # Update existing post
    if args.update:
        draft_id = args.update
        draft_url = f"https://{config['subdomain']}.substack.com/publish/post/{draft_id}"
        print(f"Updating post {draft_id} on {config['subdomain']}.substack.com...", file=sys.stderr)

        # Upload local images first
        if local_images:
            body_json = resolve_local_images(config, body_json, local_images, draft_id)

        update_draft(config, draft_id, title, subtitle, body_json, args.audience)
        print(f"Draft body updated.", file=sys.stderr)

        # Re-publish to push changes live
        print("Re-publishing to apply changes...", file=sys.stderr)
        result = publish_draft(config, draft_id)
        slug = result.get("slug", "")
        post_url = f"https://{config['subdomain']}.substack.com/p/{slug}"
        print(f"Published: {post_url}", file=sys.stderr)
        print(post_url)
        return

    # Create new draft
    print(f"Creating draft on {config['subdomain']}.substack.com...", file=sys.stderr)

    draft = create_draft(config, title, subtitle, body_json, args.audience)
    draft_id = draft.get("id")
    draft_url = f"https://{config['subdomain']}.substack.com/publish/post/{draft_id}"

    print(f"Draft created: {draft_url}", file=sys.stderr)

    # Upload local images and update draft
    if local_images:
        body_json = resolve_local_images(config, body_json, local_images, draft_id)
        print("Updating draft with images...", file=sys.stderr)
        update_draft(config, draft_id, title, subtitle, body_json, args.audience)
        print("Draft updated with images.", file=sys.stderr)

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
