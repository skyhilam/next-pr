"""CLI adapters. Never choose a model or silently enable metered credentials."""
import json
import os
from pathlib import Path
import re
import subprocess

from .common import Blocked, command, now

CREDENTIAL_VARIABLES = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                        'XAI_API_KEY', 'GROK_API_KEY', 'CURSOR_API_KEY',
                        'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL', 'CURSOR_API_ENDPOINT',
                        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                        'CLAUDE_CODE_USE_FOUNDRY', 'CODEX_API_KEY')


def safe_environment():
    present = [key for key in CREDENTIAL_VARIABLES if os.environ.get(key)]
    if present:
        raise Blocked('API credentials/provider overrides present: ' + ', '.join(present))
    return dict(os.environ)


def verify_auth(name, item):
    env = safe_environment()
    binary = item['binary']
    if name == 'codex':
        result = subprocess.run([binary, 'login', 'status'], env=env, text=True, capture_output=True)
        if result.returncode or 'Logged in using ChatGPT' not in result.stdout + result.stderr:
            raise Blocked('Codex ChatGPT subscription login is not verified')
    elif name == 'claude':
        status = json.loads(command([binary, 'auth', 'status'], env=env))
        if not status.get('loggedIn') or status.get('authMethod') not in {'oauth', 'claude.ai'}:
            raise Blocked('Claude subscription OAuth login is not verified')
        if status.get('apiProvider') not in {None, 'firstParty'}:
            raise Blocked('Claude is configured for a third-party API provider')
    else:
        # These CLIs do not yet have a stable documented subscription-entitlement schema.
        # An operator-supplied non-model probe must assert both facts explicitly.
        probe = item.get('subscription_probe')
        if not isinstance(probe, list) or not probe or not all(isinstance(a, str) for a in probe):
            raise Blocked(f'{name} requires a verified subscription_probe argv; login alone is insufficient')
        status = json.loads(command(probe, env=env))
        if status.get('subscription') is not True or status.get('api_credentials') is not False:
            raise Blocked(f'{name} subscription authentication is not verified')
    return {'verified_at': now(), 'method': 'subscription', 'quota': 'unknown'}


def gate(name, cfg, paused):
    item = cfg['providers'].get(name)
    if not item or not item.get('enabled') or not item.get('billing_confirmed'):
        raise Blocked(f'{name}: disabled or subscription billing unconfirmed')
    if paused.get(name):
        raise Blocked(f'{name}: provider paused after rate limit; explicitly resume provider')
    # Config files can contain API keys/helpers: operator attestation is mandatory too.
    if not item.get('config_audited'):
        raise Blocked(f'{name}: CLI configuration billing audit is unconfirmed')
    verify_auth(name, item)
    return item


def argv(name, item, role, worktree, prompt_path):
    binary = item['binary']
    prompt = Path(prompt_path).read_text()
    if name == 'codex':
        return [binary, 'exec', '--cd', worktree, '--json', '--sandbox',
                'read-only' if role == 'review' else 'workspace-write',
                '-c', 'forced_login_method="chatgpt"', prompt]
    if name == 'claude':
        args = [binary, '-p', prompt, '--output-format', 'json', '--permission-prompts', 'none']
        if role == 'review':
            args += ['--tools', 'Read,Glob,Grep', '--strict-mcp-config',
                     '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands']
        return args
    if name == 'grok':
        if role == 'review':
            raise Blocked('Grok read-only tool boundary is not verified; reviewer adapter disabled')
        return [binary, '--cwd', worktree, '--prompt-file', str(prompt_path),
                '--output-format', 'json', '--no-subagents']
    if name == 'cursor':
        args = [binary, '-p', prompt, '--output-format', 'json', '--workspace', worktree]
        if role == 'review':
            args += ['--mode', 'ask']
        return args
    raise Blocked('unsupported provider (OpenCode is disabled)')


def parse_output(path):
    """Extract only known CLI result envelopes, not tool output or arbitrary log JSON."""
    content = Path(path).read_text(errors='replace')
    session, usage, final, permission_denials = None, None, None, []
    try:
        events = [json.loads(content)]
    except ValueError:
        events = []
        for line in content.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    native_rate_limit = False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get('type') in {'error', 'turn.failed', 'rate_limit_event'} or event.get('is_error'):
            details = json.dumps(event).lower()
            native_rate_limit = native_rate_limit or any(marker in details for marker in
                ('rate_limit_exceeded', 'usage_limit_reached', 'hit your usage limit', 'hit your limit', 'rate limit exceeded'))
        if event.get('type') == 'thread.started':
            session = event.get('thread_id')
        if event.get('session_id'):
            session = event['session_id']
        if event.get('type') in {'result', 'turn.completed'}:
            usage = event.get('usage', usage)
        permission_denials.extend(event.get('permission_denials') or [])
        if event.get('type') == 'result' or ('result' in event and 'session_id' in event):
            final = event.get('result')
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            final = item.get('text')
    if native_rate_limit:
        return dict(status='rate_limited', summary='CLI reported rate/usage limit', tests=[],
                    blockers=[], session_id=session, usage=usage)
    if permission_denials:
        return dict(status='blocked', summary='CLI permission denied', tests=[], blockers=[],
                    session_id=session, usage=usage)
    if isinstance(final, str):
        text = final.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
        try:
            final = json.loads(text)
        except ValueError:
            final = None
    if not isinstance(final, dict) or final.get('status') not in {
            'completed', 'blocked', 'rate_limited', 'no_blockers', 'blockers'}:
        raise Blocked('worker did not emit the required final JSON contract; inspect log')
    if not isinstance(final.get('summary'), str) or not isinstance(final.get('tests', []), list):
        raise Blocked('invalid worker summary/test evidence')
    if not isinstance(final.get('blockers', []), list) or not all(
            isinstance(item, str) for item in final.get('blockers', [])):
        raise Blocked('invalid worker blocker list')
    return {key: final.get(key) for key in ('status', 'summary', 'tests', 'blockers', 'head_sha')} | {
        'session_id': session, 'usage': usage}
