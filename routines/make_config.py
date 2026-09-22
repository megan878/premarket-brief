"""Print the RemoteTrigger `create` body for each routine. This only WRITES JSON FILES; it never calls the API.

    python routines/make_config.py [--repo URL] [--brief-url URL] [--open-url URL] [--connector-url URL]

Anything not supplied stays as a <PLACEHOLDER> so it is obvious what still needs a decision.
Both routines are generated with enabled=false: create disabled, run once by hand, inspect, then enable.
"""
import argparse, json, pathlib, uuid

here = pathlib.Path(__file__).parent
ap = argparse.ArgumentParser()
ap.add_argument('--repo', default='<GITHUB_REPO_URL>')
ap.add_argument('--brief-url', default='<BRIEF_ARTIFACT_URL>')
ap.add_argument('--open-url', default='<OPEN_ARTIFACT_URL>')
ap.add_argument('--connector-url', default='<FMP_CONNECTOR_URL>')
ap.add_argument('--env', default='env_011GEddWgLHigXXVsZaD6hRV')       # the account's default cloud environment
ap.add_argument('--model', default='claude-sonnet-5')
a = ap.parse_args()

FMP = {'connector_uuid': '01a07ad1-c7b9-4104-a24b-5718bb62e41a', 'name': 'FMP', 'url': a.connector_url}
TOOLS = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebFetch', 'WebSearch', 'Artifact']


def body(name, cron, prompt_file):
    prompt = (here / prompt_file).read_text(encoding='utf-8').replace('{{BRIEF_URL}}', a.brief_url).replace('{{OPEN_URL}}', a.open_url)
    return {
        'name': name,
        'cron_expression': cron,
        'enabled': False,
        'mcp_connections': [FMP],
        'job_config': {'ccr': {
            'environment_id': a.env,
            'session_context': {'model': a.model, 'sources': [{'git_repository': {'url': a.repo}}], 'allowed_tools': TOOLS},
            'events': [{'data': {'uuid': str(uuid.uuid4()), 'session_id': '', 'type': 'user', 'parent_tool_use_id': None,
                                 'message': {'content': prompt, 'role': 'user'}}}]}},
    }


for name, cron, pf, out in [
    ('Pre-Market Brief', '0 12 * * 1-5', 'pre-market-brief.prompt.md', 'pre-market-brief.config.json'),
    ('Market Open Update', '25 13,14 * * 1-5', 'market-open-update.prompt.md', 'market-open-update.config.json'),
]:
    (here / out).write_text(json.dumps(body(name, cron, pf), indent=1, ensure_ascii=False), encoding='utf-8')
    print('wrote', out)
