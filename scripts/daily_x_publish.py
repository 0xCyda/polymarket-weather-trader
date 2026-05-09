#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path


DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8414/"
DEFAULT_IMAGE_OUTPUT = "/tmp/polymarket-daily-x-metrics.png"
DEFAULT_UPLOAD_DIR = Path.home() / "Downloads"
DEFAULT_CAPTURE_WIDTH = 1872
DEFAULT_CAPTURE_HEIGHT = 760


def run_bh(code: str) -> str:
    prelude = """
try:
    info = page_info()
    if isinstance(info, dict) and info.get('dialog'):
        cdp('Page.handleJavaScriptDialog', accept=True)
        wait(0.5)
except Exception:
    pass
"""
    result = subprocess.run(
        ["browser-harness"],
        input=textwrap.dedent(prelude) + "\n" + code,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"browser-harness failed with exit {result.returncode}")
    return result.stdout.strip()


def capture_dashboard_metrics(output_path: str, dashboard_url: str) -> dict:
    snippet = f"""
import base64, json
from pathlib import Path

output_path = {json.dumps(output_path)}
dashboard_url = {json.dumps(dashboard_url)}
width = {DEFAULT_CAPTURE_WIDTH}
height = {DEFAULT_CAPTURE_HEIGHT}

new_tab(dashboard_url)
cdp('Emulation.setDeviceMetricsOverride', width=width, height=height, deviceScaleFactor=1, mobile=False)
wait_for_load(20)

ready = False
for _ in range(40):
    probe = js(r'''(() => JSON.stringify({{
      cardsText: document.querySelector('#summary-cards')?.innerText || '',
      childCount: document.querySelector('#summary-cards')?.children.length || 0,
      layoutReady: !!document.querySelector('.layout-main .card h2'),
      updated: document.querySelector('#last-updated')?.innerText || ''
    }}))()''')
    probe = json.loads(probe)
    if probe.get('childCount', 0) >= 6 and 'BALANCE' in probe.get('cardsText', '') and probe.get('layoutReady'):
        ready = True
        break
    wait(0.5)
if not ready:
    raise RuntimeError('Dashboard overview never became ready for screenshot capture')

wait(0.5)
shot = cdp('Page.captureScreenshot', format='png', clip={{
    'x': 0,
    'y': 0,
    'width': width,
    'height': height,
    'scale': 1,
}})['data']
meta = json.loads(js(r'''(() => JSON.stringify({{
  viewport: {{w: window.innerWidth, h: window.innerHeight}},
  header: (() => {{ const e = document.querySelector('.header'); if (!e) return null; const r = e.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})(),
  cards: (() => {{ const e = document.querySelector('#summary-cards'); if (!e) return null; const r = e.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})(),
  layoutMain: (() => {{ const e = document.querySelector('.layout-main'); if (!e) return null; const r = e.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})(),
  scrollHeight: document.body.scrollHeight
}}))()'''))
Path(output_path).parent.mkdir(parents=True, exist_ok=True)
Path(output_path).write_bytes(base64.b64decode(shot))
print(json.dumps({{'output_path': output_path, 'capture': {{'x': 0, 'y': 0, 'width': width, 'height': height}}, 'meta': meta, 'page': page_info()}}, indent=2))
"""
    raw = run_bh(textwrap.dedent(snippet))
    return json.loads(raw)


def stage_upload_image(image_path: str) -> str:
    src = Path(image_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Upload image not found: {src}")
    DEFAULT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    staged = DEFAULT_UPLOAD_DIR / src.name
    if src != staged:
        shutil.copy2(src, staged)
    return str(staged)


def post_to_x(
    text: str,
    image_path: str,
    dry_run: bool = False,
    quote_url: str | None = None,
    quote_latest: bool = False,
) -> dict:
    normalized_snippet = " ".join(text.split())[:80]
    lead_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    upload_path = stage_upload_image(image_path)
    snippet = f"""
import json, re, time

post_text = {json.dumps(text)}
image_path = {json.dumps(upload_path)}
needle = {json.dumps(normalized_snippet.lower())}
lead_line = {json.dumps(lead_line.lower())}
dry_run = {repr(dry_run)}
quote_url = {repr(quote_url)}
quote_latest = {repr(quote_latest)}


def canonical_status_href(href):
    if not href:
        return None
    href = href.split('?')[0].split('/analytics')[0]
    m = re.search(r'/status/(\\d+)', href)
    if not m:
        return href
    return href[:m.end()]


def authored_text_only(text, quote_url):
    text = (text or '').strip()
    if not quote_url or not text:
        return text
    lines = [line.rstrip() for line in text.splitlines()]
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('x.com/') or stripped.startswith('https://x.com/') or stripped.startswith('http://x.com/'):
            break
        out.append(line)
    return '\\n'.join(out).strip()


def tweet_matches_authored_text(tweet_text, lead_line, needle, quote_url):
    authored = authored_text_only(tweet_text, quote_url)
    normalized = ' '.join(authored.split()).lower()
    lowered = authored.lower()
    if needle and needle in normalized:
        return True
    if lead_line and lead_line in lowered:
        return True
    return False


x_open_home()
profile_href = js("(() => {{ const a = document.querySelector('a[data-testid=\\\"AppTabBar_Profile_Link\\\"]'); return a ? a.getAttribute('href') : null; }})()")
if not profile_href:
    raise RuntimeError('Could not determine X profile handle')

goto('https://x.com' + profile_href)
wait_for_load(20)
wait(2)
latest_before_raw = js(r'''(() => JSON.stringify({{
  href: document.querySelector('article[data-testid="tweet"] a[href*="/status/"]')?.getAttribute('href') || null
}}))()''')
latest_before = canonical_status_href(json.loads(latest_before_raw or '{{}}').get('href'))

if quote_latest and not quote_url:
    if not latest_before:
        raise RuntimeError('Could not find latest tweet URL to quote')
    quote_url = 'https://x.com' + latest_before

if quote_url:
    goto(quote_url)
    wait_for_load(20)
    wait(3)
    js(r'''(() => {{
      const btn = document.querySelector('button[data-testid="retweet"]');
      if (!btn) throw new Error('Retweet button missing on quote target');
      btn.click();
    }})()''')
    wait(1)
    js(r'''(() => {{
      const item = [...document.querySelectorAll('[role="menuitem"], [role="menuitemradio"]')]
        .find(n => /quote/i.test((n.innerText || '').trim()));
      if (!item) throw new Error('Quote menu item missing');
      item.click();
    }})()''')
else:
    new_tab('https://x.com/compose/post')

wait_for_load(20)
wait(3)
state = js(r'''(() => JSON.stringify({{
  url: location.href,
  title: document.title,
  loginInput: !!document.querySelector('input[name="text"], input[autocomplete="username"]'),
  composer: !!document.querySelector('[data-testid="tweetTextarea_0"], div[role="textbox"]'),
  postButton: !!document.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'),
}}))()''')
state = json.loads(state or '{{}}')
if state.get('loginInput'):
    raise RuntimeError('X is not logged in')
if not state.get('composer'):
    raise RuntimeError('X composer did not load')

js(r'''(() => {{
  const box = document.querySelector('[data-testid="tweetTextarea_0"], div[role="textbox"]');
  if (box) box.focus();
}})()''')
wait(0.4)
type_text(post_text)
wait(0.8)
upload_file('input[data-testid="fileInput"]', image_path)

button_ready = False
for _ in range(40):
    status = js(r'''(() => JSON.stringify({{
      mediaFailed: document.body.innerText.includes('Some of your media failed to load.'),
      buttonDisabled: (() => {{
        const btn = document.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
        if (!btn) return null;
        return btn.getAttribute('aria-disabled') || btn.disabled || false;
      }})(),
      composerText: (() => {{
        const box = document.querySelector('[data-testid="tweetTextarea_0"], div[role="textbox"]');
        return box ? box.innerText : '';
      }})(),
      attachmentImgs: document.querySelectorAll('[data-testid="attachments"] img').length,
      blobImgs: document.querySelectorAll('img[src^="blob:"]').length,
      hasTagPeople: document.body.innerText.includes('Tag people'),
      hasAddDescription: document.body.innerText.includes('Add description'),
    }}))()''')
    status = json.loads(status or '{{}}')
    enough_images = status.get('blobImgs', 0) >= 1
    if quote_url:
        enough_images = enough_images and status.get('attachmentImgs', 0) >= 3
    if not status.get('mediaFailed') and str(status.get('buttonDisabled')).lower() not in ('true', '1') and enough_images:
        button_ready = True
        break
    wait(0.5)
if not button_ready:
    raise RuntimeError('X composer never became ready to post with media attached')

if dry_run:
    preview_raw = js(r'''(() => JSON.stringify({{
      profileHref: document.querySelector('a[data-testid="AppTabBar_Profile_Link"]')?.getAttribute('href') || null,
      composerText: document.querySelector('[data-testid="tweetTextarea_0"], div[role="textbox"]')?.innerText || '',
      mediaFailed: document.body.innerText.includes('Some of your media failed to load.'),
      buttonDisabled: (() => {{
        const btn = document.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
        if (!btn) return null;
        return btn.getAttribute('aria-disabled') || btn.disabled || false;
      }})(),
      attachmentImgs: document.querySelectorAll('[data-testid="attachments"] img').length,
      blobImgs: document.querySelectorAll('img[src^="blob:"]').length,
      quoteUrl: quote_url,
      latestBefore: latest_before,
    }}))()''')
    preview = json.loads(preview_raw or '{{}}')
    print(json.dumps({{'dry_run': True, 'ready': True, 'profile_href': profile_href, 'preview': preview}}, indent=2))
else:
    js(r'''(() => {{
      const btn = document.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
      if (!btn) throw new Error('Post button missing');
      btn.click();
    }})()''')
    wait(6)
    found = None
    for _ in range(30):
        goto('https://x.com' + profile_href)
        wait_for_load(20)
        wait(2)
        data = js(r'''(() => JSON.stringify({{
          tweets: [...document.querySelectorAll('article[data-testid="tweet"]')].slice(0, 8).map(a => ({{
            text: [...a.querySelectorAll('[data-testid="tweetText"]')].map(n => n.innerText).join('\n').trim(),
            href: a.querySelector('a[href*="/status/"]')?.getAttribute('href') || null,
          }}))
        }}))()''')
        tweets = json.loads(data or '{{}}').get('tweets', [])
        for tweet in tweets:
            href = canonical_status_href(tweet.get('href'))
            if latest_before and href == latest_before:
                continue
            if not tweet_matches_authored_text(tweet.get('text') or '', lead_line, needle, quote_url):
                continue
            found = dict(
                text=tweet.get('text') or '',
                authored_text=authored_text_only(tweet.get('text') or '', quote_url),
                url=('https://x.com' + href) if href else None,
            )
            break
        if found:
            break
        wait(1)
    if not found:
        raise RuntimeError('Post click finished, but could not verify the tweet on profile')
    print(json.dumps({{'dry_run': False, 'posted': True, 'profile_href': profile_href, 'tweet': found, 'quote_url': quote_url, 'latest_before': latest_before}}, indent=2))
"""
    raw = run_bh(textwrap.dedent(snippet))
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture dashboard metrics and post a daily X update via browser-harness.")
    parser.add_argument("--text-file", required=True, help="Path to file containing the final X post text.")
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--image-output", default=DEFAULT_IMAGE_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Prepare the post and attach the image, but do not click Post.")
    parser.add_argument("--quote-url", help="Quote this exact tweet URL instead of making a standalone post.")
    parser.add_argument("--quote-latest", action="store_true", help="Quote the latest post from the logged-in profile.")
    args = parser.parse_args()

    text_path = Path(args.text_file)
    if not text_path.exists():
        raise SystemExit(f"Text file not found: {text_path}")
    text = text_path.read_text().strip()
    if not text:
        raise SystemExit("Text file is empty")

    image_info = capture_dashboard_metrics(args.image_output, args.dashboard_url)
    post_info = post_to_x(
        text,
        args.image_output,
        dry_run=args.dry_run,
        quote_url=args.quote_url,
        quote_latest=args.quote_latest,
    )
    print(json.dumps({
        "text_file": str(text_path.resolve()),
        "image": image_info,
        "post": post_info,
        "timestamp": int(time.time()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
