"""Render a built brief.html in Chromium and fail on any console error or page error.

  python tests/shoot_brief.py out/preview/brief.html out/preview/shots

Takes full-page and Track-record-section screenshots at desktop 1200px and mobile 390px, each in light and dark mode,
and checks the page for horizontal overflow. Needs: pip install playwright && python -m playwright install chromium.
"""
import pathlib, sys

from playwright.sync_api import sync_playwright

VIEWPORTS = {'desktop1200': {'width': 1200, 'height': 900}, 'mobile390': {'width': 390, 'height': 844}}


def main(html, outdir):
    html = pathlib.Path(html).resolve()
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    problems, shots = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for vp_name, vp in VIEWPORTS.items():
            for scheme in ('light', 'dark'):
                ctx = browser.new_context(viewport=vp, color_scheme=scheme, device_scale_factor=1)
                page = ctx.new_page()
                msgs = []
                page.on('console', lambda m, msgs=msgs: msgs.append((m.type, m.text)) if m.type in ('error', 'warning') else None)
                page.on('pageerror', lambda e, msgs=msgs: msgs.append(('pageerror', str(e))))
                page.goto(html.as_uri())
                page.wait_for_selector('section.section')
                tag = f'{vp_name}-{scheme}'
                bad = [m for m in msgs if m[0] in ('error', 'pageerror')]
                problems += [f'{tag}: {m[0]}: {m[1]}' for m in bad]
                overflow = page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
                if overflow > 0:
                    problems.append(f'{tag}: horizontal page overflow of {overflow}px')
                if page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgba(0, 0, 0, 0)':
                    problems.append(f'{tag}: body has no background')
                height = page.evaluate('document.documentElement.scrollHeight')
                # Chromium cannot capture past ~16k px in one image: cap the full-page shot, the section/card shots carry the detail
                page.screenshot(path=str(out / f'{tag}-full.png'), full_page=True,
                                clip={'x': 0, 'y': 0, 'width': vp['width'], 'height': min(height, 15000)})
                shots.append(f'{tag}-full.png')
                sec = page.locator('section.section:has(h2:has-text("Track record"))')
                if sec.count():
                    sec.first.screenshot(path=str(out / f'{tag}-track.png'))
                    shots.append(f'{tag}-track.png')
                else:
                    problems.append(f'{tag}: no Track record section')
                # a pick card with its tracker chip
                card = page.locator('article.pick.tech').first
                if card.count():
                    card.screenshot(path=str(out / f'{tag}-card.png'))
                    shots.append(f'{tag}-card.png')
                ctx.close()
        browser.close()
    print('screenshots:', ', '.join(shots))
    if problems:
        print('PROBLEMS:')
        for p in problems:
            print(' -', p)
        return 1
    print('no console errors, no page errors, no horizontal overflow, in 4 viewport/theme combinations')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
