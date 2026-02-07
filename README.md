# Substack Skill

A Cursor skill that lets the AI publish markdown posts to your Substack newsletter. It converts markdown to Substack’s ProseMirror format and can create drafts or publish directly.

**Capabilities:**

- **Publish or draft** – Create Substack drafts for review, or publish articles immediately
- **Markdown → Substack** – Converts markdown (headings, lists, code blocks, blockquotes, images, bold, italic, links) into Substack’s editor format
- **Flexible options** – Override title and subtitle, set audience to everyone or paid subscribers, and use `--dry-run` to preview conversion without hitting the API
- **Template-aware** – Works with a post template that includes Hook, Key Points, Body Outline, Call to Action, and similar sections; skips metadata sections like Status, Hashtags, Notes

After installation, configure `SUBSTACK_SID`, `SUBSTACK_SUBDOMAIN`, and `SUBSTACK_USER_ID` (see `.env.example` and `SKILL.md` for details).

## Installation

To install this skill, run:

```bash
npx skills add https://github.com/apetcu/substack-skill --skill substack-publisher
```
